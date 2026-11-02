# -*- coding: utf-8 -*-
"""ComfyClient 级行为测试（假 ComfyUI，不需要显卡/手机）。

填的是子代理指出的「假绿」：Transport 自己有 10 条测试，但 ComfyClient 委托后
没有任何测试——deadline 传错、fetch_image 参数顺序写反，那 10 条仍全绿。

这里 stub 掉 astrbot.api.logger 后直接 import comfy_backend，跑：
  generate 全链路（ensure_server → submit → wait → 解析 images）
  fetch_image 的参数名/顺序（假服务器记录 query）
  queue_status / _health_sync
  超时归到 ComfyTimeout、连不上归到 ComfyUnreachable（② 的分类型）
  cancel 打 /interrupt 与 /queue
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

COMFY_DIR = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy"
sys.path.insert(0, COMFY_DIR)

# ── stub astrbot.api.logger（comfy_backend 唯一的框架依赖）──────────────
if "astrbot" not in sys.modules:
    fake_astrbot = types.ModuleType("astrbot")
    fake_api = types.ModuleType("astrbot.api")

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    fake_api.logger = _Logger()
    fake_astrbot.api = fake_api
    sys.modules["astrbot"] = fake_astrbot
    sys.modules["astrbot.api"] = fake_api

import comfy_backend as cb  # noqa: E402
import comfy_settings as cs  # noqa: E402

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
    calls: list[str] = []
    mode = "ok"          # ok / never_done / error
    polls = 0

    def log_message(self, *a):  # noqa: D401
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        FakeComfy.calls.append("POST " + self.path)
        if self.path == "/prompt":
            self._json({"prompt_id": "p1"})
        elif self.path == "/interrupt":
            self._json({})
        elif self.path == "/queue":
            self._json({})
        else:
            self._json({}, 404)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        FakeComfy.calls.append("GET " + self.path)
        if u.path == "/system_stats":
            self._json({"system": {"comfyui_version": "0.0.0-fake"}, "devices": [{"name": "FakeGPU"}]})
        elif u.path.startswith("/history/"):
            FakeComfy.polls += 1
            if FakeComfy.mode == "never_done":
                self._json({})
                return
            if FakeComfy.mode == "error":
                self._json({"p1": {"status": {"status_str": "error", "messages": [
                    ["execution_error", {"node_type": "KSampler", "node_id": "7", "exception_message": "CUDA OOM"}]]}}})
                return
            self._json({"p1": {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"9": {"images": [
                    {"filename": "a.png", "subfolder": "s", "type": "output"}]}},
            }})
        elif u.path == "/view":
            q = parse_qs(u.query)
            FakeComfy.calls.append("VIEWQ " + json.dumps({k: v[0] for k, v in q.items()}))
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
        elif u.path == "/queue":
            self._json({"queue_running": [[1]], "queue_pending": [[2], [3]]})
        else:
            self._json({}, 404)


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def make_client(port: int, gen_timeout: float = 5.0) -> "cb.ComfyClient":
    settings = cs.ComfySettings(host="127.0.0.1", port=port, gen_timeout=gen_timeout)
    return cb.ComfyClient(settings=settings, log_path=None)


def reset(mode="ok"):
    FakeComfy.calls = []
    FakeComfy.polls = 0
    FakeComfy.mode = mode


def main() -> int:
    srv, port = start()
    print("=" * 70)
    print(f"ComfyClient 行为测试（假 ComfyUI @ 127.0.0.1:{port}）")
    print("=" * 70)

    def t_generate():
        reset()
        client = make_client(port)
        out = asyncio.run(client.generate({"1": {"class_type": "X", "inputs": {}}}))
        imgs = out.get("images") or []
        assert imgs, f"没有 images: {out}"
        first = imgs[0]
        assert first.get("filename") == "a.png" and first.get("subfolder") == "s", first
        assert "POST /prompt" in FakeComfy.calls, "没提交工作流"
        return f"images={len(imgs)} filename={first['filename']}"

    check("generate 全链路（ensure→submit→wait→解析）", t_generate)

    def t_fetch():
        reset()
        client = make_client(port)
        raw = asyncio.run(client.fetch_image({"filename": "a.png", "subfolder": "s", "type": "output"}))
        assert raw == PNG, "取图字节不一致"
        q = [c for c in FakeComfy.calls if c.startswith("VIEWQ")]
        assert q, "没请求 /view"
        parsed = json.loads(q[0][len("VIEWQ "):])
        assert parsed.get("filename") == "a.png", parsed
        assert parsed.get("subfolder") == "s", parsed
        assert parsed.get("type") == "output", parsed
        return "filename/subfolder/type 三个参数名与取值都对"

    check("fetch_image 参数顺序与命名", t_fetch)

    def t_queue():
        reset()
        client = make_client(port)
        q = asyncio.run(client.queue_status())
        assert q == {"running": 1, "pending": 2}, q
        return f"running={q['running']} pending={q['pending']}"

    check("queue_status 解析", t_queue)

    def t_health():
        reset()
        client = make_client(port)
        h = client._health_sync(2.0)
        assert isinstance(h, dict) and h.get("system"), h
        return "system_stats 正常返回"

    check("_health_sync 探活", t_health)

    def t_timeout_class():
        reset("never_done")
        client = make_client(port, gen_timeout=1.0)
        t0 = time.time()
        try:
            asyncio.run(client.generate({"1": {"class_type": "X", "inputs": {}}}))
        except cb.ComfyTimeout as exc:
            return f"{time.time()-t0:.1f}s 内抛 ComfyTimeout（子类正确）"
        except cb.ComfyError as exc:
            raise AssertionError(f"抛的是基类 {type(exc).__name__}: {exc}") from None
        raise AssertionError("没有抛异常")

    check("超时归到 ComfyTimeout（② 分类）", t_timeout_class)

    def t_exec_error():
        reset("error")
        client = make_client(port)
        try:
            asyncio.run(client.generate({"1": {"class_type": "X", "inputs": {}}}))
        except cb.ComfyExecutionError as exc:
            assert "CUDA OOM" in str(exc), exc
            return "ComfyExecutionError 带节点原因"
        except cb.ComfyError as exc:
            raise AssertionError(f"抛的是基类 {type(exc).__name__}: {exc}") from None
        raise AssertionError("没有抛异常")

    check("执行报错归到 ComfyExecutionError", t_exec_error)

    def t_unreachable():
        reset()
        # 用 TEST-NET 地址（必然不可达）走远程分支；127.0.0.1 会走本地 check_paths，
        # 报的是「comfy_dir 配错」而不是「连不上」，那是另一种错误，不该混测。
        settings = cs.ComfySettings(host="192.0.2.1", port=8199, gen_timeout=2.0)
        client = cb.ComfyClient(settings=settings, log_path=None)
        try:
            asyncio.run(client.generate({"1": {"class_type": "X", "inputs": {}}}))
        except cb.ComfyUnreachable:
            return "连不上抛 ComfyUnreachable（子类正确）"
        except cb.ComfyError as exc:
            raise AssertionError(f"抛的是基类 {type(exc).__name__}: {exc}") from None
        raise AssertionError("没有抛异常")

    check("连不上归到 ComfyUnreachable", t_unreachable)

    def t_cancel():
        reset()
        client = make_client(port)
        client.last_prompt_id = "p1"
        asyncio.run(client.cancel())
        joined = " ".join(FakeComfy.calls)
        assert "POST /interrupt" in joined, joined
        assert "POST /queue" in joined, joined
        return "调了 /interrupt 与 /queue"

    check("cancel 清远端队列", t_cancel)

    srv.shutdown()
    ok = sum(1 for _, p, _ in results if p)
    bad = [r for r in results if not r[1]]
    print()
    print(f"汇总：{ok}/{len(results)} 通过" + ("" if not bad else f"，{len(bad)} 项失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
