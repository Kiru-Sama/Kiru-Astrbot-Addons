# -*- coding: utf-8 -*-
"""ComfyUI 传输层（纯 stdlib，不依赖 AstrBot）。

为什么要单独一层（2026-11-02）：
  原来 HTTP 协议、进程管理、远程拉起、AstrBot 日志全焊在 ComfyClient 一个类里，
  导致「换个 ComfyUI 地址」和「在没有显卡的机器上测」都做不到。
  这一层只做一件事：按 ComfyUI 官方 HTTP API 收发，并把失败分类成可处理的异常。

接口（对应官方 API）：
  submit(graph)                    -> prompt_id          POST /prompt
  wait(prompt_id, deadline)        -> outputs            GET  /history/<id>（内含探活）
  cancel(prompt_id)                -> None               POST /interrupt + POST /queue{"delete":[...]}
  health()                         -> dict | None        GET  /system_stats
  object_info(node, field)         -> list[str]          GET  /object_info/<node>
  view(filename, subfolder, type)  -> bytes             GET  /view
  queue()                          -> {running, pending} GET /queue

错误分类（上层按类型给不同提示，不再靠字符串匹配）：
  TransportUnreachable  连不上 / 连接超时 / 5xx
  TransportTimeout      等待结果超时（deadline）
  ExecutionError        节点执行报错（status_str == "error"）
  ProtocolError         返回格式不对 / 4xx
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 10.0


class TransportError(Exception):
    """传输层异常基类。"""


class TransportUnreachable(TransportError):
    """连不上 ComfyUI（网络/端口/服务挂了）。"""


class TransportTimeout(TransportError):
    """提交成功但等待结果超过 deadline。"""


class ExecutionError(TransportError):
    """ComfyUI 执行报错（节点异常）。"""


class ProtocolError(TransportError):
    """返回格式不符合预期 / 请求被拒（4xx）。"""


class Transport:
    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        client_id: str = "ncd-comfy",
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.client_id = client_id

    # ── 底层 ──────────────────────────────────────────────────────
    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: float | None = None,
    ):
        url = self.base_url + path
        data = None
        headers = {"User-Agent": f"Mozilla/5.0 ({self.client_id})"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise TransportUnreachable(f"HTTP {exc.code} @ {path}") from None
            raise ProtocolError(f"HTTP {exc.code} @ {path}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise TransportUnreachable(f"连不上 {self.base_url}（{type(exc).__name__}）") from None

        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ProtocolError(f"返回不是 JSON @ {path}") from None

    # ── 官方 API ──────────────────────────────────────────────────
    def submit(self, graph: dict, client_id: str | None = None) -> str:
        if not isinstance(graph, dict) or not graph:
            raise ProtocolError("工作流为空")
        data = self._request(
            "POST",
            "/prompt",
            {"prompt": graph, "client_id": client_id or self.client_id},
        )
        if not isinstance(data, dict):
            raise ProtocolError(f"/prompt 返回了奇怪的东西：{str(data)[:200]}")
        node_errors = data.get("node_errors") or {}
        if node_errors:
            raise ProtocolError(f"工作流被拒绝：{_node_errors_text(node_errors)}")
        pid = str(data.get("prompt_id") or "")
        if not pid:
            raise ProtocolError(f"提交后没有 prompt_id：{str(data)[:200]}")
        return pid

    def wait(
        self,
        prompt_id: str,
        deadline: float,
        poll_interval: float = 0.6,
        probe_every: float = 15.0,
        on_progress=None,
    ) -> dict:
        """轮询 /history 直到完成；期间定期探活，连续 3 次失败判为断线。"""
        last_probe = 0.0
        fails = 0
        while time.time() < deadline:
            if time.time() - last_probe >= probe_every:
                last_probe = time.time()
                if self.health() is None:
                    fails += 1
                    if fails >= 3:
                        raise TransportUnreachable("连续 3 次探活失败，电脑可能断线了")
                else:
                    fails = 0
            try:
                hist = self._request(
                    "GET", f"/history/{urllib.parse.quote(prompt_id)}", timeout=10.0
                )
            except TransportUnreachable:
                hist = {}          # 单次抖动继续等，由探活计数决定是否中止
            entry = (hist or {}).get(prompt_id) if isinstance(hist, dict) else None
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise ExecutionError(_status_text(status))
                if status.get("completed") or entry.get("outputs"):
                    return entry.get("outputs") or {}
                if on_progress and isinstance(status.get("messages"), list):
                    try:
                        on_progress(status["messages"])
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(max(0.1, poll_interval))
        raise TransportTimeout(f"等 {prompt_id[:8]} 超过 {max(0.0, deadline - time.time() + 0.0):.0f}s")

    def cancel(self, prompt_id: str) -> None:
        """先 interrupt，再从队列删掉；两步都失败只抛最后一类错误。"""
        errors = []
        try:
            self._request("POST", "/interrupt", {}, timeout=8.0)
        except TransportError as exc:
            errors.append(exc)
        if prompt_id:
            try:
                self._request(
                    "POST", "/queue", {"delete": [prompt_id]}, timeout=8.0
                )
            except TransportError as exc:
                errors.append(exc)
        if len(errors) == 2:
            raise errors[-1]

    def health(self, timeout: float = 3.0) -> dict | None:
        try:
            data = self._request("GET", "/system_stats", timeout=timeout)
        except TransportError:
            return None
        return data if isinstance(data, dict) else None

    def object_info(self, node: str, field: str) -> list[str]:
        data = self._request(
            "GET", f"/object_info/{urllib.parse.quote(node)}", timeout=15.0
        )
        try:
            spec = data[node]["input"]["required"][field]
        except (KeyError, TypeError):
            raise ProtocolError(f"{node}.{field} 不在 object_info 里") from None
        if isinstance(spec[0], list):
            return [str(x) for x in spec[0]]
        options = (spec[1] or {}).get("options") if len(spec) > 1 else None
        return [str(x) for x in (options or [])]

    def view(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        q = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": type_}
        )
        url = self.base_url + "/view?" + q
        try:
            with urllib.request.urlopen(url, timeout=30.0) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise ProtocolError(f"取图 HTTP {exc.code}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise TransportUnreachable(f"取图失败（{type(exc).__name__}）") from None

    def queue(self) -> dict:
        data = self._request("GET", "/queue", timeout=8.0) or {}
        try:
            running = len(data.get("queue_running") or [])
            pending = len(data.get("queue_pending") or [])
        except Exception:  # noqa: BLE001
            raise ProtocolError("queue 返回格式不对") from None
        return {"running": running, "pending": pending}


def _node_errors_text(node_errors: dict) -> str:
    """把 /prompt 返回的 node_errors 压成一句可读的话。"""
    parts = []
    for node_id, info in (node_errors or {}).items():
        if not isinstance(info, dict):
            continue
        for err in info.get("errors") or []:
            if isinstance(err, dict):
                parts.append(
                    f"{info.get('class_type') or node_id}: "
                    f"{str(err.get('message') or '')[:160]}"
                )
    return "；".join(parts) if parts else "节点错误（详情看 ComfyUI 控制台）"


def _status_text(status: dict) -> str:
    """把 /history 的 error 状态压成一句可读的话。"""
    parts = []
    for msg in status.get("messages") or []:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        kind, payload = msg[0], msg[1]
        if kind == "execution_error" and isinstance(payload, dict):
            parts.append(
                f"{payload.get('node_type') or '?'}"
                f"({payload.get('node_id') or '?'}): "
                f"{str(payload.get('exception_message') or '')[:200]}"
            )
    return "执行报错：" + ("；".join(parts) if parts else "未知原因")
