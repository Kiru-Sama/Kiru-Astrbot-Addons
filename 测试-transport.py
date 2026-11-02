# -*- coding: utf-8 -*-
"""ComfyUI 传输层的行为测试（不需要真显卡、不需要手机）。

做法：起一个进程内的假 ComfyUI（http.server），只实现官方 API 的几条：
  POST /prompt            -> {"prompt_id": "p1"}
  GET  /history/<id>      -> 第一次 {}，第二次 completed + outputs
  POST /interrupt         -> {}
  POST /queue             -> {}
  GET  /system_stats      -> 假显卡信息
  GET  /object_info/<n>   -> 一个下拉字段
  GET  /view              -> PNG 字节

然后断言：提交 / 等待 / 超时 / 取消 / 探活 / 取清单 / 取图 / 连不上。
这套测试能进 CI —— 这是「传输层可测」的证明。
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COMFY_DIR = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy"
sys.path.insert(0, COMFY_DIR)

import comfy_transport as ct  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
results: list[tuple[str, bool, str]] = []


def check(name, fn):
    try:
        detail = fn() or ""
        results.append((name, True, detail))
        print(f"  PASS  {name}  — {detail}")
    except AssertionError as exc:
        results.append((name, False, str(exc)))
        print(f"  FAIL  {name}\n        └─ {exc}")
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"  ERROR {name}\n        └─ {type(exc).__name__}: {exc}")


class FakeComfy(BaseHTTPRequestHandler):
    polls = 0
    completed_at = 2          # 第几次轮询给结果
    error_mode = False
    calls: list[str] = []
    mode = "ok"               # ok / error / slow

    def log_message(self, *a):  # noqa: D401
        pass

    def _json(self, obj, code=200):
        body = __import__("json").dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        FakeComfy.calls.append(self.path)
        if self.path == "/prompt":
            self._json({"prompt_id": "p1"})
        elif self.path == "/interrupt":
            self._json({})
        elif self.path == "/queue":
            self._json({})
        else:
            self._json({}, 404)

    def do_GET(self):  # noqa: N802
        FakeComfy.calls.append(self.path)
        if self.path == "/system_stats":
            if FakeComfy.mode == "down":
                self._json({}, 500)
            else:
                self._json({"system": {"comfyui_version": "0.0.0-fake"}, "devices": [{"name": "FakeGPU"}]})
        elif self.path.startswith("/history/"):
            FakeComfy.polls += 1
            if FakeComfy.error_mode:
                self._json({"p1": {"status": {"status_str": "error", "messages": [["execution_error", {"node_type": "KSampler", "node_id": "7", "exception_message": "CUDA OOM"}]]}}})
            elif FakeComfy.polls >= FakeComfy.completed_at:
                self._json({"p1": {"status": {"status_str": "success", "completed": True}, "outputs": {"9": {"images": [{"filename": "a.png"}]}}}})
            else:
                self._json({})
        elif self.path.startswith("/object_info/"):
            self._json({"UNETLoader": {"input": {"required": {"unet_name": [["a.safetensors", "b.safetensors"]]}}}})
        elif self.path.startswith("/view"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
        elif self.path == "/queue":
            self._json({"queue_running": [[1]], "queue_pending": []})
        else:
            self._json({}, 404)


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def reset(completed_at=2, mode="ok", error_mode=False):
    FakeComfy.polls = 0
    FakeComfy.completed_at = completed_at
    FakeComfy.mode = mode
    FakeComfy.error_mode = error_mode
    FakeComfy.calls = []


def main() -> int:
    srv, base = start_server()
    print("=" * 70)
    print(f"传输层行为测试（假 ComfyUI @ {base}）")
    print("=" * 70)
    t = ct.Transport(base, timeout=5.0)

    def t_submit():
        reset()
        pid = t.submit({"1": {"class_type": "X", "inputs": {}}})
        assert pid == "p1", f"prompt_id 错: {pid!r}"
        assert "/prompt" in FakeComfy.calls, "没真的 POST /prompt"
        return "提交返回 p1，请求到达 /prompt"

    check("submit 提交工作流", t_submit)

    def t_wait_ok():
        reset(completed_at=2)
        out = t.wait("p1", deadline=time.time() + 10, poll_interval=0.05, probe_every=60)
        assert "9" in out, f"outputs 不对: {out}"
        assert FakeComfy.polls >= 2, "没有真的轮询两次"
        return f"轮询 {FakeComfy.polls} 次拿到 outputs"

    check("wait 轮询直到完成", t_wait_ok)

    def t_wait_timeout():
        reset(completed_at=999)
        t0 = time.time()
        try:
            t.wait("p1", deadline=time.time() + 1.0, poll_interval=0.05, probe_every=60)
        except ct.TransportTimeout:
            return f"1 秒内正确超时（实耗 {time.time()-t0:.1f}s）"
        raise AssertionError("没有抛 TransportTimeout")

    check("wait 超时抛 TransportTimeout", t_wait_timeout)

    def t_error():
        reset(error_mode=True)
        try:
            t.wait("p1", deadline=time.time() + 5, poll_interval=0.05, probe_every=60)
        except ct.ExecutionError as exc:
            assert "CUDA OOM" in str(exc), f"错误信息丢了节点报错: {exc}"
            return f"ExecutionError 带节点原因: {str(exc)[:40]}"
        raise AssertionError("没有抛 ExecutionError")

    check("执行报错抛 ExecutionError（带节点原因）", t_error)

    def t_cancel():
        reset()
        t.cancel("p1")
        assert "/interrupt" in FakeComfy.calls, "没调 /interrupt"
        assert "/queue" in FakeComfy.calls, "没调 /queue delete"
        return "调了 /interrupt 与 /queue"

    check("cancel 同时清 interrupt 与队列", t_cancel)

    def t_health():
        reset()
        h = t.health()
        assert h and h.get("devices"), f"health 返回异常: {h}"
        reset(mode="down")
        assert t.health() is None, "5xx 时应返回 None"
        return "正常返回信息，5xx 返回 None"

    check("health 探活", t_health)

    def t_object_info():
        reset()
        names = t.object_info("UNETLoader", "unet_name")
        assert names == ["a.safetensors", "b.safetensors"], f"解析错: {names}"
        return "解析下拉字段 " + ",".join(names)

    check("object_info 解析下拉字段", t_object_info)

    def t_view():
        reset()
        raw = t.view("a.png")
        assert raw == PNG, "取图字节不一致"
        return f"取图 {len(raw)} 字节"

    check("view 取图", t_view)

    def t_queue():
        reset()
        q = t.queue()
        assert q == {"running": 1, "pending": 0}, f"queue 解析错: {q}"
        return "running=1 pending=0"

    check("queue 解析运行中/排队", t_queue)

    def t_unreachable():
        dead = ct.Transport("http://127.0.0.1:1", timeout=1.0)
        try:
            dead.submit({"1": {}})
        except ct.TransportUnreachable:
            return "端口不通抛 TransportUnreachable"
        raise AssertionError("没有抛 TransportUnreachable")

    check("连不上抛 TransportUnreachable", t_unreachable)

    def t_probe_hook():
        """本机模式：probe_hook 报「进程退了」要立刻中止，不能等 45 秒。"""
        reset(completed_at=999)
        t = ct.Transport(base, timeout=5.0)
        calls = {"n": 0}

        def hook():
            calls["n"] += 1
            return "出图过程中 ComfyUI 退出了（exit=1）" if calls["n"] >= 2 else ""

        try:
            t.wait(
                "p1", deadline=time.time() + 10,
                poll_interval=0.05, probe_every=0.05, probe_hook=hook,
            )
        except ct.TransportUnreachable as exc:
            assert "exit=1" in str(exc), exc
            return f"第 {calls['n']} 次探活中止（带进程退出原因）"
        raise AssertionError("没有中止")

    check("probe_hook 能中止等待（本机进程退出）", t_probe_hook)

    def t_no_abort():
        """abort_after_fails=0：探活失败也不中止，只按 deadline 超时（本机长任务）。"""
        reset(completed_at=999, mode="down")
        t = ct.Transport(base, timeout=2.0)
        try:
            t.wait(
                "p1", deadline=time.time() + 1.0,
                poll_interval=0.05, probe_every=0.05, abort_after_fails=0,
            )
        except ct.TransportTimeout:
            return "探活失败但不中止，1 秒后按 deadline 超时"
        raise AssertionError("没有按 deadline 超时")

    check("abort_after_fails=0 时不因探活中止", t_no_abort)

    srv.shutdown()
    ok = sum(1 for _, p, _ in results if p)
    bad = [r for r in results if not r[1]]
    print()
    print(f"汇总：{ok}/{len(results)} 通过" + ("" if not bad else f"，{len(bad)} 项失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
