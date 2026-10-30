# -*- coding: utf-8 -*-
"""ComfyUI 远程拉起器 —— 给手机端的 AstrBot 用。

背景：bot 住手机、ComfyUI 住电脑。手机没法在电脑上开进程，所以需要电脑侧
有个小服务：手机探活 8199 失败时 POST 过来，由它在本地把 ComfyUI 拉起来。

接口（只监听局域网）：
    GET  /status            -> {"ready": bool, "pid": int|null}
    POST /start             -> 启动 ComfyUI 并等到就绪（或超时），返回 JSON
    POST /stop              -> 关掉由本服务启动的 ComfyUI（可选）

安全：
    · 默认只接受 192.168.137.0/24、10.x、172.16-31.x、127.0.0.1 的请求
    · 可设 TOKEN，请求需带 X-Token 头（留空=不校验，仅限可信局域网）

用法：
    python comfy_launcher.py
    python comfy_launcher.py --port 8200 --token 你的口令
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ★ 2026-10-30：pythonw.exe 下 sys.stdout / sys.stderr 是 None，
#   任何 print() 都会抛 AttributeError —— 进程绑好端口后立刻死，
#   外部表现为「8200 能连上，但 HTTP 请求被直接掐断」。
#   这里兜住，保证没有控制台也能正常跑。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = sys.stdout

# ── 默认配置（按你的实际路径改，或用命令行参数覆盖）──────────────────
DEFAULT_COMFY_DIR = r"F:\ComfyUI_min"
DEFAULT_COMFY_PORT = 8199
DEFAULT_LISTEN = "0.0.0.0"
DEFAULT_LAUNCH_PORT = 8200
DEFAULT_TOKEN = ""            # 留空 = 不校验（只在可信局域网用）
STARTUP_TIMEOUT = 300         # 等 ComfyUI 就绪最长秒数
DEFAULT_IDLE_TIMEOUT = 1800   # 空闲多久自动关掉（秒）；0 = 永不自动关

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_last_request = time.time()   # 最后一次收到请求的时间（看门狗用）


def comfy_busy(port: int, timeout: float = 3.0) -> bool:
    """ComfyUI 有没有正在跑/排队的任务。有任务时绝不能关它。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/queue", timeout=timeout
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(data, dict):
        return False
    running = data.get("queue_running") or []
    pending = data.get("queue_pending") or []
    return bool(running) or bool(pending)


# ────────────────────────────────────────────────────────────────
#  ComfyUI 探活 / 启动
# ────────────────────────────────────────────────────────────────
def comfy_ready(port: int, timeout: float = 3.0) -> bool:
    """端口上是不是真的 ComfyUI（/system_stats 返回带 system 的 JSON）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/system_stats", timeout=timeout
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
        return isinstance(data, dict) and isinstance(data.get("system"), dict)
    except Exception:  # noqa: BLE001
        return False


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _python_exe(comfy_dir: Path) -> str:
    cand = comfy_dir / "python_embeded" / "python.exe"
    if cand.exists():
        return str(cand)
    return sys.executable


def _main_py(comfy_dir: Path) -> Path:
    for cand in (comfy_dir / "ComfyUI" / "main.py", comfy_dir / "main.py"):
        if cand.exists():
            return cand
    raise FileNotFoundError(f"在 {comfy_dir} 下找不到 ComfyUI/main.py")


def start_comfy(comfy_dir: Path, port: int) -> dict:
    """启动 ComfyUI 并等到就绪。已就绪则直接返回。"""
    global _proc

    if comfy_ready(port):
        return {"ready": True, "started": False, "reason": "已经在跑"}

    if port_busy(port):
        return {
            "ready": False,
            "started": False,
            "error": f"端口 {port} 被别的程序占着（不是 ComfyUI）",
        }

    main_py = _main_py(comfy_dir)
    py = _python_exe(comfy_dir)
    cmd = [
        py, "-s", str(main_py),
        "--port", str(port),
        "--listen", "0.0.0.0",
        "--disable-auto-launch",
        "--preview-method", "none",
    ]
    log_path = comfy_dir / "launcher_comfy.log"
    out = open(log_path, "ab")  # noqa: SIM115 —— 交给子进程持有
    try:
        _proc = subprocess.Popen(  # noqa: S603
            cmd, cwd=str(comfy_dir), stdout=out, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    finally:
        out.close()

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if comfy_ready(port):
            return {"ready": True, "started": True, "pid": _proc.pid}
        if _proc.poll() is not None:
            return {
                "ready": False, "started": True, "pid": _proc.pid,
                "error": f"ComfyUI 启动后立即退出（返回码 {_proc.returncode}），日志见 {log_path}",
            }
        time.sleep(2)
    return {"ready": False, "started": True, "error": f"等待 {STARTUP_TIMEOUT}s 仍未就绪"}


def stop_comfy(port: int) -> dict:
    global _proc
    if _proc is None or _proc.poll() is not None:
        return {"stopped": False, "reason": "没有由本服务启动的进程"}
    _proc.terminate()
    try:
        _proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _proc.kill()
    _proc = None
    return {"stopped": True}


def idle_watchdog(cfg: dict) -> None:
    """空闲到点就把自己启动的 ComfyUI 关掉，把显存还回去。

    ★ 2026-10-30：机主自己还有一套完整版 ComfyUI 常驻，两张嘴抢一张卡。
      min 版平时不该一直占着显存 —— 反正 bot 下次出图会再把它拉起来。
    安全线：只有「距上次请求超过 idle_timeout」且「队列为空」才关；
            用户手动启动的 ComfyUI（_proc 为 None）永远不碰。
    """
    idle = int(cfg.get("idle_timeout") or 0)
    if idle <= 0:
        return
    while True:
        time.sleep(60)
        try:
            if _proc is None or _proc.poll() is not None:
                continue
            waited = time.time() - _last_request
            if waited < idle:
                continue
            if comfy_busy(cfg["comfy_port"]):
                print(f"[launcher] 空闲 {waited/60:.0f} 分钟，但队列有任务，先不关", flush=True)
                continue
            print(f"[launcher] 空闲 {waited/60:.0f} 分钟且队列为空，关闭 ComfyUI 释放显存", flush=True)
            res = stop_comfy(cfg["comfy_port"])
            print(f"[launcher] 结果: {res}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[launcher] 看门狗异常: {exc}", flush=True)


# ────────────────────────────────────────────────────────────────
#  HTTP
# ────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    cfg: dict = {}

    def _allowed(self) -> bool:
        ip = self.client_address[0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.is_loopback:
            return True
        return (
            addr in ipaddress.ip_network("192.168.137.0/24")
            or addr in ipaddress.ip_network("192.168.0.0/16")
            or addr in ipaddress.ip_network("10.0.0.0/8")
            or addr in ipaddress.ip_network("172.16.0.0/12")
        )

    def _reply(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check(self) -> bool:
        global _last_request
        _last_request = time.time()      # 任何一次请求都算「有人在用」
        if not self._allowed():
            self._reply(403, {"error": "来源不在允许的局域网段内"})
            return False
        token = self.cfg.get("token") or ""
        if token and self.headers.get("X-Token") != token:
            self._reply(401, {"error": "token 不匹配"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._check():
            return
        port = self.cfg["comfy_port"]
        pid = _proc.pid if (_proc is not None and _proc.poll() is None) else None
        self._reply(200, {"ready": comfy_ready(port), "pid": pid, "port": port})

    def do_POST(self) -> None:
        if not self._check():
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        port = self.cfg["comfy_port"]
        if path == "/start":
            with _lock:
                res = start_comfy(Path(self.cfg["comfy_dir"]), port)
            self._reply(200 if res.get("ready") else 503, res)
        elif path == "/stop":
            with _lock:
                res = stop_comfy(port)
            self._reply(200, res)
        else:
            self._reply(404, {"error": f"未知路径 {path}"})

    def log_message(self, fmt, *args):  # noqa: ANN001
        sys.stderr.write(f"[launcher] {self.address_string()} {fmt % args}\n")


def _log_path() -> Path:
    return Path(__file__).resolve().parent / "comfy_launcher.log"


def log(msg: str) -> None:
    """同时写控制台和日志文件。

    ★ 2026-10-30：自启用 pythonw 启动，没有控制台，异常会静默消失，
      只剩「端口能连但不回话」这种怪现象。落一份日志，出问题能查。
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        # 用 GBK 写：Windows 的记事本 / type / PowerShell 默认按本地代码页读，
        # 写 UTF-8 无 BOM 会显示成乱码。中文内容 GBK 都装得下。
        with open(_log_path(), "a", encoding="gbk", errors="replace") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="ComfyUI 远程拉起器")
    ap.add_argument("--comfy-dir", default=DEFAULT_COMFY_DIR)
    ap.add_argument("--comfy-port", type=int, default=DEFAULT_COMFY_PORT)
    ap.add_argument("--listen", default=DEFAULT_LISTEN)
    ap.add_argument("--port", type=int, default=DEFAULT_LAUNCH_PORT)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    ap.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT,
                    help="空闲多少秒后自动关掉自己启动的 ComfyUI（0=永不）")
    args = ap.parse_args()

    Handler.cfg = {
        "comfy_dir": args.comfy_dir,
        "comfy_port": args.comfy_port,
        "token": args.token,
        "idle_timeout": args.idle_timeout,
    }

    # ★ 绑定重试：开机自启时可能和上一次退出的 socket 撞车（TIME_WAIT / 端口没放干净），
    #   一次失败就静默退出会让人以为服务在跑。重试 6 次，每次退避 2 秒。
    srv = None
    last_err: Exception | None = None
    for i in range(6):
        try:
            srv = ThreadingHTTPServer((args.listen, args.port), Handler)
            break
        except OSError as exc:
            last_err = exc
            log(f"[launcher] 绑定 {args.listen}:{args.port} 失败（第 {i + 1} 次）: {exc}")
            time.sleep(2)
    if srv is None:
        log(f"[launcher] 放弃：端口 {args.port} 一直被占用 / 不可用（{last_err}）")
        return

    log(f"[launcher] 监听 {args.listen}:{args.port}")
    log(f"[launcher] ComfyUI 目录 {args.comfy_dir}  端口 {args.comfy_port}")
    log(f"[launcher] token {'已启用' if args.token else '未启用（仅限可信局域网）'}")
    if args.idle_timeout > 0:
        log(f"[launcher] 空闲 {args.idle_timeout // 60} 分钟后自动关掉 ComfyUI（队列有任务时不关）")
    else:
        log("[launcher] 空闲自动关闭：已禁用")
    threading.Thread(target=idle_watchdog, args=(Handler.cfg,), daemon=True).start()
    log(f"[launcher] 探活: GET http://<本机IP>:{args.port}/status")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("[launcher] 退出")


if __name__ == "__main__":
    main()
