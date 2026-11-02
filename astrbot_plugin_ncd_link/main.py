# -*- coding: utf-8 -*-
"""链路看护：盯 QQ 在不在线，断线恢复后主动告知。

背景（2026-11-01）：
  手机在「校园网 ↔ 电脑热点」之间手动切换时，真正断的不是
  NapCat ↔ AstrBot（那是本机 127.0.0.1 的 WebSocket，切网不影响），
  而是 NapCat ↔ 腾讯（QQ 服务器）。恢复靠 NapCat 自己的重连；
  但断线期间收到的消息腾讯不会补发，用户也不知情。

  这个插件做两件事：
    1. 每 N 秒向 OneBot 探一次 get_status，记录 on/off 切换与断线时长
    2. 断线超过阈值后恢复，主动给「最后说话的那个会话」发一条提示，
       避免 bot 假装连续、答非所问
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.web import json_response

PLUGIN_DIR_NAME = "astrbot_plugin_ncd_link"


class NcdLinkPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf = config or {}
        self._task: asyncio.Task | None = None
        self._online: bool | None = None
        self._off_since = 0.0
        self._last_ok = 0.0
        self._last_error = ""
        self._last_umo = ""
        self._gap_events: list[dict] = []
        self._closing = False

        try:
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-link/status",
                self.api_status,
                ["GET"],
                "QQ 在线状态与最近断线记录",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[link] 注册 API 失败: {exc}")

    # ------------------------------------------------------------------
    #  配置
    # ------------------------------------------------------------------
    def _int(self, key: str, default: int) -> int:
        try:
            return int(self._conf.get(key, default))
        except (TypeError, ValueError):
            return default

    def _bool(self, key: str, default: bool) -> bool:
        v = self._conf.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    # ------------------------------------------------------------------
    #  探测
    # ------------------------------------------------------------------
    def _cqhttp_inst(self):
        try:
            for inst in self.context.platform_manager.get_insts():
                bot = getattr(inst, "bot", None)
                if bot is not None and hasattr(bot, "call_action"):
                    return inst
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
        return None

    async def _probe(self) -> tuple[bool, str]:
        inst = self._cqhttp_inst()
        if inst is None:
            return False, "找不到 OneBot 适配器"
        try:
            ret = await asyncio.wait_for(inst.bot.call_action("get_status"), timeout=8.0)
        except asyncio.TimeoutError:
            return False, "get_status 超时"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if isinstance(ret, dict):
            online = bool(ret.get("online", ret.get("good", True)))
            return online, "" if online else "QQ 未在线"
        return True, ""

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        interval = max(10, min(300, self._int("probe_interval", 30)))
        notice_after = max(10, self._int("notice_after", 45))
        notify = self._bool("notify", True)
        while True:
            try:
                online, err = await self._probe()
                now = time.time()
                if online:
                    self._last_ok = now
                    self._last_error = ""
                    if self._online is False and self._off_since:
                        gap = now - self._off_since
                        self._gap_events.append(
                            {"from": self._off_since, "to": now, "seconds": round(gap, 1)}
                        )
                        del self._gap_events[:-30]
                        logger.info(f"[link] QQ 已恢复在线（断了 {gap:.0f} 秒）")
                        if notify and gap >= notice_after and self._last_umo:
                            text = (
                                f"📶 网络恢复了（断了约 {int(gap)} 秒）。"
                                "这期间你发的消息我可能没收到，有的话麻烦重发一下。"
                            )
                            try:
                                await self.context.send_message(
                                    self._last_umo, MessageChain([Plain(text)])
                                )
                            except Exception as exc2:  # noqa: BLE001
                                logger.warning(f"[link] 恢复提示发送失败: {exc2}")
                        self._off_since = 0.0
                    self._online = True
                else:
                    self._last_error = err
                    if self._online is not False:
                        self._off_since = now
                        logger.warning(f"[link] QQ 掉线: {err}")
                    self._online = False
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(interval)

    def _ensure_task(self) -> None:
        """惰性启动。

        ★ 2026-11-01：@filter.on_astrbot_loaded() 只在 AstrBot 启动那一刻触发，
          而本插件是热装进去的 —— 装完不会补触发，探测循环就永远不启动
          （表现为 /ncd-link/status 里 online 一直是 null）。
          所以再加两条拉起路径：第一条消息、第一次 API 调用。
        """
        if self._closing:
            return
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.create_task(self._loop())
                logger.info("[link] 链路看护已启动（惰性）")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[link] 启动看护失败: {exc}")

    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        self._ensure_task()

    # ------------------------------------------------------------------
    #  记录最后说话的会话（恢复提示发这里）
    # ------------------------------------------------------------------
    @filter.event_message_type(EventMessageType.ALL)
    async def _remember(self, event: AstrMessageEvent) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo:
            self._last_umo = umo
        self._ensure_task()

    async def terminate(self) -> None:
        """插件被禁用/重载时收掉探测循环。

        ★ 2026-11-02（P1-5）：原来没有 terminate，热重载一次就多一个循环，
          同一条「网络恢复了」可能被发好几次；禁用后还会幽灵发通知。
        """
        self._closing = True
        task = getattr(self, "_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[link] 停止看护时异常（忽略）: {exc}")
        self._task = None
        logger.info("[link] 链路看护已停止")

    # ------------------------------------------------------------------
    #  Web API
    # ------------------------------------------------------------------
    async def api_status(self):
        self._ensure_task()
        return {
            "online": self._online,
            "last_ok": self._last_ok,
            "off_since": self._off_since,
            "last_error": self._last_error,
            "last_umo": self._last_umo,
            "gaps": self._gap_events[-10:],
            "now": time.time(),
        }
