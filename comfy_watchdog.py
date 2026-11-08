# -*- coding: utf-8 -*-
"""ComfyUI 常驻看护（Windows 侧，不属于任何 AstrBot 插件）。

职责只有一件事：让 8199 上的 ComfyUI 一直活着。
  · 每 10 秒探一次 /system_stats
  · 不在跑 → 启动（--listen 0.0.0.0 --port 8199 --disable-auto-launch）
  · 进程退出 → 指数退避重启（5s → 300s 封顶）
  · **不主动关它**（不做空闲卸载/关机，那是它的反面）

为什么单独写：
  之前由手机上的 AstrBot 插件 POST /start 远程拉起（comfy_launcher 8200），
  代价是一次冷启动 60~90 秒、空闲 30 分钟被关、还要处理端口绑定重试。
  进程生命周期属于操作系统，放回 Windows 层后，插件只管 HTTP。
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ★ 2026-11-07：极简版从 F 盘（USB 外接，报 Ntfs 错误）挪到 E 盘（NVMe）
#   旧路径 F:\ComfyUI_min 已废弃；可用环境变量 NCD_COMFY_DIR 覆盖
import os as _os
COMFY_DIR = Path(_os.environ.get("NCD_COMFY_DIR") or r"E:\comfyuiMINI")
PYTHON = COMFY_DIR / "python_embeded" / "python.exe"
MAIN = COMFY_DIR / "ComfyUI" / "main.py"
PORT = 8199
LOG = Path(__file__).with_name("comfy_watchdog.log")

ARGS = [
    str(PYTHON),
    "-s",
    str(MAIN),
    "--port",
    str(PORT),
    "--listen",
    "0.0.0.0",
    "--disable-auto-launch",
    "--preview-method",
    "none",
]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def healthy(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/system_stats", timeout=timeout
        ) as r:
            return int(getattr(r, "status", 0) or 0) == 200
    except Exception:
        return False


_LOCK_PORT = 8202
_lock_sock = None


def _single_instance() -> bool:
    """用本地端口当单实例锁：抢不到就说明已经有看护在跑。"""
    global _lock_sock
    import socket

    s = socket.socket()
    try:
        s.bind(("127.0.0.1", _LOCK_PORT))
        s.listen(1)
    except OSError:
        return False
    _lock_sock = s
    return True


def main() -> int:
    if not _single_instance():
        print("已有看护实例在跑，本进程退出")
        return 0
    log(f"看护启动：{COMFY_DIR}  端口 {PORT}")
    proc: subprocess.Popen | None = None
    backoff = 5.0
    while True:
        try:
            if healthy():
                if proc is not None:
                    log("ComfyUI 已就绪")
                    proc = None
                backoff = 5.0
                time.sleep(10)
                continue

            # 没在跑：如果自己起的子进程还活着，就再等等（模型加载中）
            if proc is not None and proc.poll() is None:
                time.sleep(10)
                continue

            if proc is not None:
                log(f"ComfyUI 进程退出（code={proc.returncode}），{backoff:.0f} 秒后重启")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300.0)

            log("启动 ComfyUI …")
            proc = subprocess.Popen(  # noqa: S603
                ARGS,
                cwd=str(COMFY_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            log(f"已拉起 pid={proc.pid}")
            # 给它 90 秒就绪窗口
            for _ in range(18):
                time.sleep(5)
                if healthy():
                    log("ComfyUI 就绪")
                    proc = None
                    backoff = 5.0
                    break
        except Exception as exc:  # noqa: BLE001
            log(f"看护异常：{type(exc).__name__}: {exc}")
            time.sleep(15)


if __name__ == "__main__":
    sys.exit(main())
