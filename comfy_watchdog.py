# -*- coding: utf-8 -*-
"""ComfyUI 常驻看护（Windows 侧，不属于任何 AstrBot 插件）。

职责只有一件事：让 8199 上的 ComfyUI 一直活着。
  · 每 10 秒探一次 /system_stats
  · 不在跑 → 启动（--listen 0.0.0.0 --port 8199 --disable-auto-launch）
  · 进程退出 → 指数退避重启（5s → 300s 封顶）
  · **不主动关它**（不做空闲卸载/关机，那是它的反面）

★ 2026-11-18：外挂模型库（F 盘 USB）启动就绪防抖
  ComfyUI 如果在 F 盘没挂稳时启动，folder_paths 会缓存空列表，
  之后即使 F 盘恢复也不会重扫 → 出图报 "Value not in list"（假掉盘）。
  现在：
    1. 启动前等 extra_model_paths.yaml 里的外挂目录可枚举（最多 180 秒）；
    2. 启动后抽样检查 UnetLoaderGGUF 列表，空就重启一次（最多 2 次）。
"""

from __future__ import annotations

import json
import re
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


def _model_lib_roots() -> list[Path]:
    """从 extra_model_paths.yaml 里取 base_path（避免引入 yaml 依赖）。"""
    roots: list[Path] = []
    cfg = COMFY_DIR / "ComfyUI" / "extra_model_paths.yaml"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return roots
    for line in text.splitlines():
        m = re.search(r"^\s*base_path\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        raw = m.group(1).strip().strip('"').strip("'")
        if raw:
            roots.append(Path(raw))
    return roots


def _model_lib_ready() -> bool:
    """外挂模型库关键目录可枚举且非空（ComfyUI 启动扫描的前提）。"""
    roots = _model_lib_roots()
    if not roots:
        return True  # 没配外挂路径：不阻塞
    for root in roots:
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        for sub in ("diffusion_models", "text_encoders"):
            d = root / sub
            try:
                if not d.is_dir():
                    break
                if not any(d.iterdir()):
                    break
            except OSError:
                break
        else:
            return True
    return False


def _wait_model_lib(max_wait: float = 180.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < max_wait:
        if _model_lib_ready():
            return True
        log("外挂模型库还没就绪（F 盘未挂稳？），等待中…")
        time.sleep(5)
    return False


def _gguf_ok() -> bool | None:
    """启动后抽检：True=清单正常 / False=清单为空（半残实例）/ None=判断不了。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/object_info/UnetLoaderGGUF", timeout=10
        ) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        spec = (
            (d.get("UnetLoaderGGUF") or {})
            .get("input", {})
            .get("required", {})
            .get("unet_name")
        )
        if not spec:
            return None  # 节点不在：判断不了，不折腾
        if isinstance(spec[0], list):
            vals = spec[0]
        elif len(spec) > 1 and isinstance(spec[1], dict):
            vals = spec[1].get("options") or []
        else:
            vals = []
        return bool(vals)
    except Exception:
        return None  # ★ 2026-11-19：接口异常时不再当成功，交回上层判断


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
    retry_bad = 0
    # ★ 2026-11-19 修 M9：抽检拉长到 5 分钟一次（原来每 10 秒请求 object_info）
    last_gguf_check = 0.0
    GGUF_CHECK_INTERVAL = 300.0
    while True:
        try:
            if healthy():
                if proc is not None:
                    log("ComfyUI 已就绪")
                    proc = None
                # 就绪后按间隔抽检模型清单（防止"能连但模型没扫到"的半残实例）
                now_check = time.time()
                if now_check - last_gguf_check >= GGUF_CHECK_INTERVAL:
                    last_gguf_check = now_check
                    gguf = _gguf_ok()
                    if gguf is False:
                        retry_bad += 1
                        if retry_bad <= 2:
                            log(f"模型清单为空（第 {retry_bad} 次），重启 ComfyUI 重新扫描")
                            _kill_comfy()
                            time.sleep(8)
                            backoff = 5.0
                            continue
                        log("模型清单仍为空（已重试 2 次），先保持现状，等外挂盘恢复（5 分钟后再抽检）")
                    elif gguf is None:
                        log("模型清单抽检失败（接口异常，判断不了），下一轮再试")
                    else:
                        retry_bad = 0
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

            # ★ 先等外挂模型库就绪，再启动 ComfyUI（防止缓存空列表）
            if not _wait_model_lib():
                log("外挂模型库 180 秒仍未就绪，先不启动（避免半残实例），30 秒后重试")
                time.sleep(30)
                continue

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


def _kill_comfy() -> None:
    """杀掉 8199 上的 ComfyUI 进程（自愈重启用）。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
        for line in out.splitlines():
            if f":{PORT}" in line and "LISTENING" in line:
                pid = line.split()[-1].strip()
                if pid.isdigit() and int(pid) > 4:
                    subprocess.run(
                        ["taskkill", "/PID", pid, "/F"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    log(f"已结束旧 ComfyUI pid={pid}")
                    return
    except Exception as exc:  # noqa: BLE001
        log(f"结束 ComfyUI 失败：{exc}")


if __name__ == "__main__":
    sys.exit(main())
