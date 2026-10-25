# -*- coding: utf-8 -*-
"""链路醒着：一键阻止 Windows 空闲睡眠/休眠/熄屏，让 bot 常驻在线。

只改本进程的“执行状态请求”，不写注册表、不改电源计划：
  SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED [| ES_DISPLAY_REQUIRED])
必须在一个常驻线程里调用，线程退出时用 ES_CONTINUOUS 还原。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.web import json_response, request

PLUGIN_DIR = Path(__file__).resolve().parent

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class KeepAwake:
    """把“保持唤醒”做成可反复开关的后台线程（Windows 专用）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active = False
        self._since = 0.0
        self._keep_display = False
        self._method = ""
        self._error = ""
        self._refresh = 30.0

    def status(self) -> dict:
        thread = self._thread
        if self._active and (thread is None or not thread.is_alive()):
            self._active = False
            if not self._error:
                self._error = "保持线程已结束（可能被系统或异常中断）"
        return {
            "active": self._active,
            "since": self._since,
            "keep_display": self._keep_display,
            "method": self._method,
            "error": self._error,
        }

    def start(self, keep_display: bool, refresh: float = 30.0) -> tuple[bool, str]:
        """启动（或按新参数重启）保持线程；返回 (是否成功, 错误)。"""
        if sys.platform != "win32":
            return False, f"只支持 Windows，当前系统是 {sys.platform}"

        with self._lock:
            if (
                self._active
                and self._thread is not None
                and self._thread.is_alive()
                and self._keep_display == bool(keep_display)
            ):
                return True, ""
            self._stop.set()
            old = self._thread

        if old is not None and old.is_alive():
            old.join(timeout=3.0)
        if old is not None and old.is_alive():
            return False, "旧的保持唤醒线程没有退出，稍后再试"

        with self._lock:
            stop_event = threading.Event()
            ready = threading.Event()
            self._stop = stop_event
            self._keep_display = bool(keep_display)
            self._refresh = max(5.0, min(600.0, float(refresh)))
            self._active = False
            self._since = 0.0
            self._method = ""
            self._error = ""
            thread = threading.Thread(
                target=self._run,
                args=(stop_event, ready, bool(keep_display)),
                name="ncd-awake",
                daemon=True,
            )
            self._thread = thread
            thread.start()

        # 等线程真正把系统请求挂上（正常是毫秒级）
        ready.wait(3.0)
        with self._lock:
            if self._error:
                return False, self._error
            if not self._active:
                return False, "没能进入保持唤醒状态（未知原因）"
        return True, ""

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        if thread is not None and thread.is_alive():
            self._error = "保持唤醒线程没有退出，未强制清空状态"
            return
        with self._lock:
            self._thread = None
            self._active = False
            self._since = 0.0
            self._method = ""
            self._error = ""

    def _run(
        self, stop_event: threading.Event, ready: threading.Event, keep_display: bool
    ) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        if keep_display:
            flags |= ES_DISPLAY_REQUIRED
        try:
            # 返回值是“上一次的执行状态”，0 才代表失败
            if not kernel32.SetThreadExecutionState(flags):
                self._error = (
                    "SetThreadExecutionState 失败"
                    f"（WinError {ctypes.get_last_error()}）"
                )
                return
            self._active = True
            self._since = time.time()
            self._method = "Windows SetThreadExecutionState" + (
                "（含屏幕常亮）" if keep_display else "（仅系统）"
            )
            logger.info(f"[awake] 已保持系统唤醒（keep_display={keep_display}）")
            while not stop_event.wait(self._refresh):
                kernel32.SetThreadExecutionState(flags)
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[awake] 保持唤醒线程异常：{exc}")
        finally:
            try:
                kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:  # noqa: BLE001
                pass
            self._active = False
            ready.set()


class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self._conf: dict = config if isinstance(config, dict) else {}
        self._awake = KeepAwake()
        self._register_apis()

    # ---------------- 配置 ----------------
    def _conf_bool(self, key: str, default: bool = False) -> bool:
        raw = self._conf.get(key, default)
        if isinstance(raw, str):
            return raw.strip().lower() not in ("", "false", "0", "no", "off")
        return bool(raw)

    def _refresh(self) -> float:
        try:
            value = int(self._conf.get("refresh_seconds", 30))
        except Exception:  # noqa: BLE001
            value = 30
        return float(max(5, min(600, value)))

    def _save_cfg(self, **values) -> None:
        for key, value in values.items():
            try:
                self._conf[key] = value
            except Exception:  # noqa: BLE001
                logger.warning(f"[awake] 配置写入内存失败：{key}")
        save = getattr(self._conf, "save_config", None)
        if callable(save):
            try:
                save(dict(values))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[awake] 配置落盘失败：{exc}")

    # ---------------- 生命周期 ----------------
    async def initialize(self) -> None:
        if not self._conf_bool("enabled"):
            return
        ok, err = self._awake.start(self._conf_bool("keep_display"), self._refresh())
        if ok:
            logger.info("[awake] 已按配置自动保持系统唤醒")
        else:
            logger.warning(f"[awake] 自动保持唤醒失败：{err}")

    async def terminate(self) -> None:
        self._awake.stop()
        logger.info("[awake] 已恢复系统默认睡眠策略")

    # ---------------- Web API ----------------
    def _register_apis(self) -> None:
        try:
            name = PLUGIN_DIR.name
            self.context.register_web_api(
                f"/{name}/awake/status", self.api_status, ["GET"], "保持唤醒状态"
            )
            self.context.register_web_api(
                f"/{name}/awake/toggle", self.api_toggle, ["POST"], "开关保持唤醒"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[awake] 注册 API 失败：{exc}")

    def _status_payload(self) -> dict:
        data = {
            "ok": True,
            "supported": sys.platform == "win32",
            "platform": sys.platform,
            "refresh_seconds": self._refresh(),
        }
        data.update(self._awake.status())
        return data

    async def api_status(self):
        return json_response(self._status_payload())

    async def api_toggle(self):
        body = await request.json({}) or {}
        raw = body.get("on")
        if isinstance(raw, bool):
            on = raw
        else:
            on = str(raw or "").strip().lower() in ("1", "true", "on", "yes")

        keep_display = body.get("keep_display")
        if keep_display is None:
            keep_display = self._conf_bool("keep_display")
        keep_display = bool(keep_display)

        if on:
            ok, err = self._awake.start(keep_display, self._refresh())
        else:
            self._awake.stop()
            ok, err = True, ""

        self._save_cfg(enabled=bool(ok and on), keep_display=keep_display)
        data = self._status_payload()
        data["ok"] = ok
        data["error"] = err or data.get("error") or ""
        if ok:
            logger.info(f"[awake] 面板开关：{'开启' if on else '关闭'}（屏幕={keep_display}）")
        else:
            logger.warning(f"[awake] 面板开启失败：{err}")
        return json_response(data)
