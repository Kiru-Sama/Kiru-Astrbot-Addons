# -*- coding: utf-8 -*-
"""linkd —— 电脑侧链路探针（给手机上的 bot 用）。

为什么要有它（2026-11-01）：
  手机上 AstrBot 判断「电脑连没连上」原本靠两个 HTTP 探测拼出来：
    · ComfyUI 8188 通 → 认为电脑在
    · 不通就试拉起器 8200 → 通了也算在
  但这两种都通不了时，它分不清是「热点断了」还是「ComfyUI 没起」，
  于是对用户统一说「comfy未连接电脑（host:port 不可达）」，用户没法判断该干嘛。

linkd 的做法：在电脑上跑一个极小的 HTTP 服务（0.0.0.0:8201），
  手机只要访问 http://<电脑IP>:8201/link/state，就同时拿到：
    pc_reachable   能连到本服务 = 电脑在线、网络可达（这是唯一确定的“链路通”信号）
    comfy          8188 是否就绪
    launcher       8200 拉起器是否在
    hotspot_ip     本机是否有 192.168.137.1（热点还开着）
  并记录每次状态变化的历史，供面板/排查用。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess

# ★ 2026-11-02：所有子进程都必须隐藏窗口，否则面板轮询会一直弹命令框
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8201
COMFY = "http://127.0.0.1:8199"
# ★ 2026-11-02（P1-2）：8200 拉起器已退役，进程生命周期归 Windows 计划任务。
# ★ 2026-11-07：日志从 F 盘（USB 外接盘，今天报过 Ntfs Delayed Write）挪到系统盘，
#   避免「写日志失败」拖住探活循环
_LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(Path(__file__).parent)) / "ncd_linkd"
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    _LOG_DIR = Path(__file__).parent
LOG = _LOG_DIR / "linkd.log"
HISTORY_MAX = 200

_lock = threading.Lock()
_state = {"pc_reachable": True, "comfy": None, "launcher": None, "hotspot_ip": None, "ts": 0.0, "detail": ""}
_history: list[dict] = []


def _probe(url: str, path: str, timeout: float = 2.0) -> dict:
    t0 = time.time()
    try:
        with urllib.request.urlopen(url + path, timeout=timeout) as r:
            body = r.read(2048)
        return {"ok": True, "status": int(getattr(r, "status", 200)), "ms": int((time.time() - t0) * 1000), "body": body[:120].decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}", "ms": int((time.time() - t0) * 1000)}


def _local_ips() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        out = subprocess.run(
        ["ipconfig"], capture_output=True, timeout=2, creationflags=_NO_WINDOW
    ).stdout.decode("gbk", "replace")
        for line in out.splitlines():
            if "IPv4" in line:
                part = line.split(":")[-1].strip()
                if part and part[0].isdigit():
                    ips.add(part)
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


_CACHE_TTL = 2.0
_addrs: list[str] | None = None
_addrs_at = 0.0


def _cached_ips() -> list[str]:
    """本机地址 60 秒缓存 —— ipconfig 要几百毫秒，不能每次请求都跑。"""
    global _addrs, _addrs_at
    now = time.time()
    if _addrs is None or now - _addrs_at > 60.0:
        _addrs = _local_ips()
        _addrs_at = now
    return _addrs


def _refresh() -> dict:
    # ★ 2026-11-02：结果缓存 2 秒。插件只等 2.5 秒，而三项探测串行 ≈2.6 秒
    #   → 手机侧永远超时、linkd 形同虚设。这里做两件事：并发探测 + 短缓存。
    now0 = time.time()
    with _lock:
        cached = _state.get("snapshot")
        if cached and now0 - float(cached.get("ts") or 0.0) < _CACHE_TTL:
            return cached

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_mini = pool.submit(_probe, "http://127.0.0.1:8199", "/system_stats", 1.2)
        f_full = pool.submit(_probe, "http://127.0.0.1:8188", "/system_stats", 1.2)
        mini, full = f_mini.result(), f_full.result()
    ips = _cached_ips()
    hotspot = "192.168.137.1" in ips
    comfy_ok = bool(mini["ok"] or full["ok"])
    if mini["ok"]:
        detail = "电脑在线，ComfyUI(8199) 就绪"
    elif full["ok"]:
        detail = "电脑在线，ComfyUI(8188) 就绪（8199 未开）"
    else:
        detail = "电脑在线，但 ComfyUI 没响应（常驻看护会在十几秒内拉起）"
    snapshot = {
        "pc_reachable": True,
        "comfy": {"ok": comfy_ok, "mini": mini, "full": full},
        "hotspot_ip": hotspot,
        "ips": ips,
        "detail": detail,
        "ts": time.time(),
    }
    log_line = ""
    with _lock:
        prev = _state.get("detail")
        _state.update(snapshot)
        _state["snapshot"] = snapshot
        if prev and prev != detail:
            _history.append({"at": snapshot["ts"], "from": prev, "to": detail})
            del _history[:-HISTORY_MAX]
            log_line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {prev}  ->  {detail}\n"
    # ★ 2026-11-07：日志写盘放在锁外——F 盘/USB 卡住时不能把探活线程一起拖死
    if log_line:
        try:
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(log_line)
        except Exception:
            pass
    return snapshot



# ── 电脑资源（面板顶部那条）────────────────────────────────────────
_gpu_cache: dict = {"at": 0.0, "data": None}
_cpu_prev: dict = {"idle": None, "total": None}


def _cpu_percent() -> float:
    """用 GetSystemTimes 两次差值算 CPU 占用（不依赖 psutil）。"""
    import ctypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    k32 = ctypes.windll.kernel32
    if not k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return -1.0

    def ft_100ns(ft) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    i, k, u = ft_100ns(idle), ft_100ns(kernel), ft_100ns(user)
    total = k + u                    # kernel 里已含 idle
    prev = dict(_cpu_prev)          # 先拷快照，再更新，否则首帧判断永远失效
    _cpu_prev.update({"idle": i, "total": total})
    if prev.get("idle") is None or prev.get("total") is None:
        # 首帧没有上一次采样：用「开机以来的平均值」，别返回 -1 让面板显示 CPU -1%
        return round(max(0.0, min(100.0, (1.0 - i / total) * 100.0)), 1) if total else 0.0
    d_total = total - prev["total"]
    d_idle = i - prev["idle"]
    if d_total <= 0:
        return -1.0
    return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))


def _mem() -> dict:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    total = st.ullTotalPhys / 1024**3
    used = (st.ullTotalPhys - st.ullAvailPhys) / 1024**3
    return {
        "ram_used_gb": round(used, 2),
        "ram_total_gb": round(total, 2),
        "ram_pct": round(st.dwMemoryLoad, 1),
    }


def _gpu() -> dict | None:
    """nvidia-smi 查询（5 秒缓存）；没有显卡就返回 None。"""
    now = time.time()
    if now - float(_gpu_cache["at"]) < 5.0:
        return _gpu_cache["data"]
    data = None
    try:
        import subprocess

        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
                "temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=_NO_WINDOW,
        ).stdout.strip().splitlines()
        if out:
            parts = [x.strip() for x in out[0].split(",")]
            if len(parts) >= 4:
                data = {
                    "name": parts[0],
                    "util": float(parts[1]),
                    "mem_used_mb": float(parts[2]),
                    "mem_total_mb": float(parts[3]),
                }
                if len(parts) >= 5 and parts[4] not in ("N/A", "[N/A]", ""):
                    try:
                        data["temp"] = float(parts[4])
                    except ValueError:
                        pass
                if len(parts) >= 6 and parts[5] not in ("N/A", "[N/A]", ""):
                    data["power"] = parts[5]
    except Exception:
        data = None
    _gpu_cache.update({"at": now, "data": data})
    return data


_cpu_temp_cache: dict = {"at": 0.0, "data": None}


def _cpu_temp() -> tuple[float, str] | None:
    """CPU 温度：返回 (摄氏温度, 来源)。

    Windows 上 CPU 温度没有统一用户态接口：ACPI 热区很多笔记本不暴露。
    AIDA64 / 游戏加加 之所以能读，是因为它们加载了 ring0 驱动直接读 MSR/SuperIO。
    这里按现实可行的顺序找：
      ① psutil（Linux / 部分主板）
      ② WMI MSAcpi 热区（部分机器有）
      ③ LibreHardwareMonitor / OpenHardwareMonitor 的 WMI（装了它们就有了）
    都没有就返回 None —— 不是我们算错，是这台机器没提供。
    """
    now = time.time()
    if now - float(_cpu_temp_cache["at"]) < 10.0:
        return _cpu_temp_cache["data"]
    res: tuple[float, str] | None = None
    # ① psutil
    try:
        import psutil

        temps = psutil.sensors_temperatures() or {}
        vals: list[float] = []
        for entries in temps.values():
            for e in entries:
                try:
                    cur = float(getattr(e, "current", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if 0 < cur < 150:
                    vals.append(cur)
        if vals:
            res = (round(max(vals), 1), "psutil")
    except Exception:
        pass
    # ② WMI 热区（值单位是 0.1K）
    if res is None:
        try:
            import subprocess

            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance -Namespace root/wmi -ClassName "
                    "MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue | "
                    "Select-Object -First 1).CurrentTemperature",
                ],
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=_NO_WINDOW,
            ).stdout.strip()
            if out.isdigit():
                c = int(out) / 10.0 - 273.15
                if 0 < c < 150:
                    res = (round(c, 1), "acpi")
        except Exception:
            pass
    # ③ LibreHardwareMonitor / OpenHardwareMonitor 的 WMI
    if res is None:
        for ns in ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor"):
            try:
                import subprocess

                ps = (
                    f"$s = Get-CimInstance -Namespace '{ns}' -ClassName Sensor "
                    "-ErrorAction SilentlyContinue | Where-Object { $_.SensorType -eq 'Temperature' "
                    "-and ($_.Name -match 'CPU|Core|Package') } | "
                    "Measure-Object -Property Value -Maximum; "
                    "if ($s.Maximum) { [math]::Round($s.Maximum, 1) }"
                )
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=_NO_WINDOW,
                ).stdout.strip()
                if out:
                    c = float(out)
                    if 0 < c < 150:
                        res = (round(c, 1), "lhm")
                        break
            except Exception:
                continue
    _cpu_temp_cache.update({"at": now, "data": res})
    return res


def _sysinfo() -> dict:
    info = {"cpu": _cpu_percent(), **_mem(), "ts": time.time()}
    gpu = _gpu()
    if gpu:
        info["gpu"] = gpu
        info["gpu_temp_src"] = "nvidia-smi"
    ct = _cpu_temp()
    if ct:
        info["cpu_temp"] = ct[0]
        info["cpu_temp_src"] = ct[1]
    return info



class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path in ("/link/state", "/link/state/"):
                self._send(200, json.dumps(_refresh(), ensure_ascii=False).encode())
            elif path == "/sysinfo":
                self._send(200, json.dumps(_sysinfo(), ensure_ascii=False).encode())
            elif path == "/link/history":
                with _lock:
                    self._send(200, json.dumps(_history[-50:], ensure_ascii=False).encode())
            elif path in ("/", "/link"):
                html = "<meta charset=utf-8><h3>linkd 链路探针</h3><pre id=o>loading…</pre><script>fetch('/link/state').then(r=>r.json()).then(j=>o.textContent=JSON.stringify(j,null,2))</script>"
                self._send(200, html.encode(), "text/html; charset=utf-8")
            else:
                self._send(404, b'{"error":"not found"}')
        except Exception as exc:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False).encode())

    def log_message(self, *a):  # noqa: ANN001
        pass


def main() -> int:
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"linkd 已启动 0.0.0.0:{PORT}   （Ctrl+C 退出）")
    _refresh()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
