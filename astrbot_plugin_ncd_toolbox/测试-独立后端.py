# -*- coding: utf-8 -*-
"""独立后端（不依赖 ComfyUI）的离线回归测试。

两块：
  A. sd_worker 的 HTTP 协议 —— 用假 torch + 假 pipeline，不需要真模型
  B. sd_local.StandaloneClient 的拉起/复用/报错 —— 用桩 worker（只用标准库）

为什么值得测：
  · worker 是"按需拉起的子进程"，最容易静默出问题的地方是
    【端口上其实是别人】却照样把请求发过去、或者【子进程一启动就死】
    而客户端还在傻等。这两条都只有真跑起来才知道。
  · 协议（/health 的字段、/txt2img 的 X-Seed 头）如果和客户端对不上，
    表现是"画图失败"但没有具体线索。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-独立后端.py
"""

import asyncio
import pathlib
import sys
import threading
import time
import types
import urllib.request
from http.server import ThreadingHTTPServer

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


# =====================================================================
#  假 torch —— sd_worker._render 里只用到 torch.Generator(...).manual_seed()
#  ★ 必须在 import sd_worker 之前塞进 sys.modules：它是函数里 import 的，
#    但模块级也会被反复解析，提前塞好最稳。
# =====================================================================
_fake_torch = types.ModuleType("torch")


class _FakeGenerator:
    def __init__(self, device=None):
        self.device = device
        self.seed = None

    def manual_seed(self, seed):
        self.seed = seed
        return self


_fake_torch.Generator = _FakeGenerator
sys.modules.setdefault("torch", _fake_torch)

import sd_worker  # noqa: E402
from sd_local import SDBackendError, StandaloneClient  # noqa: E402


# =====================================================================
#  用例 A：worker 的 HTTP 协议
# =====================================================================
print("===== 用例 A：worker 协议（假 pipeline）=====")


class FakeImage:
    size = (64, 64)

    def save(self, buf, format="PNG"):  # noqa: A002
        buf.write(b"\x89PNG\r\n\x1a\n" + b"FAKE" * 8)


class FakePipe:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        # ★ 真 diffusers 会每步调一次 callback_on_step_end；这里照做，
        #   用来验证 sd_worker 确实把进度回调接上了（不然前端进度条永远是 0）
        cb = kwargs.get("callback_on_step_end")
        total = int(kwargs.get("num_inference_steps") or 0)
        if cb is not None:
            for s in range(total):
                cb(self, s, None, {})
        return types.SimpleNamespace(images=[FakeImage()])

    def set_progress_bar_config(self, **kwargs):
        pass


fake_pipe = FakePipe()
sd_worker._state["pipe"] = fake_pipe
sd_worker._state["checkpoint"] = "fake.safetensors"
sd_worker._state["idle_timeout"] = 300.0

httpd = ThreadingHTTPServer(("127.0.0.1", 0), sd_worker.Handler)
port_a = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base_a = f"http://127.0.0.1:{port_a}"
print(f"  测试端口 {port_a}")

# --- /health ---
with urllib.request.urlopen(f"{base_a}/health", timeout=5) as r:
    import json as _json

    health = _json.loads(r.read().decode())
print(f"  /health → {health}")
check("A1", health.get("ok") is True, "health 应返回 ok=true")
check("A1", health.get("checkpoint") == "fake.safetensors", "health 应带 checkpoint")
check("A1", "idle_timeout" in health, "health 应带 idle_timeout")

# --- /progress（还没生成过）---
with urllib.request.urlopen(f"{base_a}/progress", timeout=5) as r:
    prog0 = _json.loads(r.read().decode())
print(f"  /progress（空闲）→ {prog0}")
check("A7", prog0.get("ok") is True, "progress 应返回 ok")
check("A7", prog0.get("busy") is False, "空闲时 busy 应为 false")
check("A7", "step" in prog0 and "total" in prog0, "progress 应带 step/total")

# --- /txt2img 正常路径 ---
import json  # noqa: E402

body = json.dumps(
    {"prompt": "a cat", "width": 512, "height": 512, "steps": 20, "cfg": 7.5, "seed": 123}
).encode()
req = urllib.request.Request(
    f"{base_a}/txt2img", data=body, headers={"Content-Type": "application/json"}, method="POST"
)
with urllib.request.urlopen(req, timeout=30) as r:
    png = r.read()
    seed_hdr = r.headers.get("X-Seed")
    ctype = r.headers.get("Content-Type")
print(f"  /txt2img → {len(png)} 字节  X-Seed={seed_hdr}  Content-Type={ctype}")
check("A2", png.startswith(b"\x89PNG"), "应返回 PNG 字节")
check("A2", ctype == "image/png", f"Content-Type 应是 image/png，实际 {ctype!r}")
check("A2", seed_hdr == "123", f"X-Seed 应回显传入的 123，实际 {seed_hdr!r}")
check("A2", len(fake_pipe.calls) == 1, "pipeline 应被调用一次")
call = fake_pipe.calls[0]
print(f"  传给 pipeline: prompt={call['prompt']!r} {call['width']}x{call['height']} "
      f"steps={call['num_inference_steps']} cfg={call['guidance_scale']}")
check("A3", call["num_inference_steps"] == 20, "steps 没透传")
check("A3", call["guidance_scale"] == 7.5, "cfg 没透传")
check("A3", call["prompt"] == "a cat", "prompt 没透传")
check("A3", call["negative_prompt"], "negative_prompt 应有默认值，不能是空")
check("A3", call.get("callback_on_step_end") is not None,
      "★ 没有把进度回调传给 pipeline —— 前端进度条会永远是 0")

# 生成完之后：step 应等于总步数、busy 应回到 false
with urllib.request.urlopen(f"{base_a}/progress", timeout=5) as r:
    prog1 = _json.loads(r.read().decode())
print(f"  生成后 /progress → {prog1}")
check("A8", prog1.get("busy") is False, "生成完后 busy 应回到 false")
check("A8", prog1.get("step") == 20,
      f"★step 应等于步数 20（说明每步回调都进了状态），实际 {prog1.get('step')}")
check("A8", prog1.get("total") == 20, f"total 应为 20，实际 {prog1.get('total')}")

# --- /txt2img 不传 seed 要随机 ---
body2 = json.dumps({"prompt": "a dog"}).encode()
req2 = urllib.request.Request(
    f"{base_a}/txt2img", data=body2, headers={"Content-Type": "application/json"}, method="POST"
)
with urllib.request.urlopen(req2, timeout=30) as r:
    seed_hdr2 = r.headers.get("X-Seed")
print(f"  不传 seed → X-Seed={seed_hdr2}")
check("A4", seed_hdr2 and seed_hdr2 != "0", "不传 seed 时应随机生成一个并回显")

# --- /txt2img 空 prompt 要 500 且带原因 ---
req3 = urllib.request.Request(
    f"{base_a}/txt2img",
    data=json.dumps({"prompt": "  "}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req3, timeout=10)
    check("A5", False, "空 prompt 竟然成功了")
except urllib.error.HTTPError as exc:
    payload = json.loads(exc.read().decode())
    print(f"  空 prompt → HTTP {exc.code} {payload}")
    check("A5", exc.code == 500, f"应 500，实际 {exc.code}")
    check("A5", "prompt" in str(payload.get("error", "")), "错误信息应说明是 prompt 的问题")

# --- 未知路径 404 ---
try:
    urllib.request.urlopen(f"{base_a}/nope", timeout=5)
    check("A6", False, "未知路径竟然 200")
except urllib.error.HTTPError as exc:
    check("A6", exc.code == 404, f"未知路径应 404，实际 {exc.code}")

httpd.shutdown()
httpd.server_close()

# =====================================================================
#  用例 B：StandaloneClient —— 端口被别人占着要明确报错
# =====================================================================
print("\n===== 用例 B：端口被占 / 不是我们的 worker =====")


class ForeignHandler(sd_worker.Handler):
    def do_GET(self):  # noqa: N802
        self._json(200, {"hello": "i am some other service"})


foreign = ThreadingHTTPServer(("127.0.0.1", 0), ForeignHandler)
port_b = foreign.server_address[1]
threading.Thread(target=foreign.serve_forever, daemon=True).start()

client_b = StandaloneClient(
    python=sys.executable, checkpoint=str(PLUGIN / "sd_worker.py"), port=port_b
)
try:
    asyncio.run(client_b.ensure_worker())
    check("B1", False, "端口被别的服务占着，竟然没报错")
except SDBackendError as exc:
    print(f"  报错：{str(exc)[:100]}")
    check("B1", "端口" in str(exc) or "认得" in str(exc) or "别的服务" in str(exc),
          f"错误信息应指向端口冲突，实际 {exc}")
foreign.shutdown()
foreign.server_close()

# =====================================================================
#  用例 C：缺配置要明确报错（不要抛一个看不懂的 traceback）
# =====================================================================
print("\n===== 用例 C：配置不全的报错 =====")


def expect_sd_error(label: str, client: StandaloneClient) -> None:
    try:
        asyncio.run(client.ensure_worker())
    except SDBackendError as exc:
        print(f"  {label} → {str(exc)[:80]}")
        check("C", True, "")
    except Exception as exc:  # noqa: BLE001
        check("C", False, f"{label} 抛的是 {type(exc).__name__}，应是 SDBackendError")
    else:
        check("C", False, f"{label} 竟然没报错")


expect_sd_error("没配 python", StandaloneClient(python="", checkpoint="x"))
expect_sd_error(
    "python 路径不存在",
    StandaloneClient(python=r"F:\肯定不存在\python.exe", checkpoint="x"),
)
expect_sd_error(
    "没配 checkpoint",
    StandaloneClient(python=sys.executable, checkpoint=""),
)
expect_sd_error(
    "checkpoint 不存在",
    StandaloneClient(python=sys.executable, checkpoint=r"F:\肯定不存在\a.safetensors"),
)

# =====================================================================
#  用例 D：桩 worker 走完整链路（拉起 → 探活 → 出图 → 复用同一进程）
# =====================================================================
print("\n===== 用例 D：拉起桩 worker 并复用 =====")

STUB = PLUGIN / "_stub_sd_worker.py"
STUB.write_text(
    '''# -*- coding: utf-8 -*-
"""测试用的桩 worker：只用标准库，不碰 torch。"""
import argparse, json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--checkpoint", default="")
parser.add_argument("--port", type=int, default=8199)
parser.add_argument("--idle-timeout", type=float, default=300)
# ★ 桩 worker 必须认得真 worker 的【全部】参数：只要客户端多传一个，
#   argparse 就会 exit(2)，表现成"worker 启动就退出了"，
#   而真因藏在子进程的 stderr 里（这里踩过一次：加了 --device 没同步更新桩）。
parser.add_argument("--device", default="cuda")
parser.add_argument("--lora", default="")
parser.add_argument("--lora-scale", type=float, default=0.8)
# ★ 加了新参数就要同步到这里，否则 argparse 直接 exit(2)，
#   表现为"worker 启动就退出了"，真因藏在子进程 stderr 里（这坑踩过两次）。
parser.add_argument("--arch", default="auto")
parser.add_argument("--offload", default="auto")
parser.add_argument("--sampler", default="euler")
parser.add_argument("--max-side", type=int, default=1920)
args = parser.parse_args()

hits = {"n": 0}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): return
    def do_GET(self):
        body = json.dumps({"ok": True, "checkpoint": args.checkpoint,
                           "idle_timeout": args.idle_timeout, "device": "stub"}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n).decode() or "{}")
        hits["n"] += 1
        png = b"\\x89PNG\\r\\n\\x1a\\n" + (b"STUB" * 16)
        self.send_response(200); self.send_header("Content-Type","image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("X-Seed", str(payload.get("seed") or 777)); self.end_headers()
        self.wfile.write(png)

print("[stub] ready", flush=True)
ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
''',
    encoding="utf-8",
)

# 找一个空闲端口
probe = ThreadingHTTPServer(("127.0.0.1", 0), ForeignHandler)
free_port = probe.server_address[1]
probe.server_close()

client_d = StandaloneClient(
    python=sys.executable,
    checkpoint=str(PLUGIN / "sd_worker.py"),   # 桩不读模型，只要文件存在（客户端会校验）
    port=free_port,
    idle_timeout=300,
    log_path=PLUGIN / "_stub_sd_worker.log",
    worker_script=STUB,                        # ★ 用桩 worker，不真加载模型
)
stub_log = PLUGIN / "_stub_sd_worker.log"
if stub_log.exists():
    stub_log.unlink()

try:
    t0 = time.time()
    asyncio.run(client_d.ensure_worker())
    print(f"  拉起并就绪，耗时 {time.time() - t0:.1f} 秒")
    check("D1", client_d._proc is not None and client_d._proc.poll() is None,
          "应有一个活着的子进程")

    pid1 = client_d._proc.pid
    png1, seed1 = asyncio.run(client_d.txt2img({"prompt": "a cat", "seed": 5}))
    png2, seed2 = asyncio.run(client_d.txt2img({"prompt": "a dog", "seed": 6}))
    pid2 = client_d._proc.pid
    print(f"  两次出图: {len(png1)}/{len(png2)} 字节  seed={seed1}/{seed2}  pid={pid1}/{pid2}")
    check("D2", png1.startswith(b"\x89PNG") and png2.startswith(b"\x89PNG"), "两次都应拿到 PNG")
    check("D3", (seed1, seed2) == (5, 6), f"X-Seed 应回显 5/6，实际 {seed1}/{seed2}")
    check("D4", pid1 == pid2, "★第二次应复用同一个 worker 进程，不该重拉")
finally:
    client_d.stop()
    time.sleep(0.5)
    if STUB.exists():
        STUB.unlink()
    if stub_log.exists():
        stub_log.unlink()

check("D5", client_d._proc is None, "stop() 之后应该不再持有进程")
proc_after = client_d._proc
print(f"  stop() 之后 _proc = {proc_after}")

# =====================================================================
#  判定
# =====================================================================
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: worker 协议（health / txt2img / X-Seed / 空 prompt 报错 / 404）/ "
      "端口被占明确报错 / 配置不全明确报错 / 拉起桩 worker 并复用同一进程 全部正确")

