# -*- coding: utf-8 -*-
"""链路迎新：新人入群时直接回复固定欢迎语，不经过 LLM。

设计要点（都对着 AstrBot 4.28.2 源码核实过）：
  · aiocqhttp 把 OneBot 的 notice 事件转成 AstrBotMessage（见
    aiocqhttp_platform_adapter._convert_handle_notice_event）：
      有 group_id 时 type=GROUP_MESSAGE，并保留 abm.raw_message = event。
  · 所以入群通知会走到 @filter.event_message_type(GROUP_MESSAGE) 的监听器里，
    再靠 raw_message 里的 post_type / notice_type 把「普通群消息」滤掉。
  · 不经过 LLM：should_call_llm(False) + stop_event()，并用 bot 直接发群消息。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType

# 默认欢迎语；配置 welcome_text 留空时用它
DEFAULT_TEXT = "欢迎{name}入群喵，我是大画家丝瓜喵，可以@我免费约稿哦"


class NcdWelcomePlugin(star.Star):
    def __init__(
        self, context: star.Context, config: dict[str, Any] | None = None
    ) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf: dict[str, Any] = config or {}

    # ------------------------------------------------------------------
    #  配置
    # ------------------------------------------------------------------
    def _conf_bool(self, key: str, default: bool = False) -> bool:
        raw = self._conf.get(key, default)
        if isinstance(raw, str):
            return raw.strip().lower() not in ("", "false", "0", "no", "off")
        return bool(raw)

    def _conf_str(self, key: str, default: str = "") -> str:
        raw = self._conf.get(key, default)
        return str(raw).strip() if raw is not None else default

    # ------------------------------------------------------------------
    #  入群通知判定
    # ------------------------------------------------------------------
    @staticmethod
    def _raw_event(event: AstrMessageEvent) -> dict | None:
        """取原始的 OneBot 事件；只认 dict / 类 dict。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            return raw
        # aiocqhttp 的 Event 也是 dict 子类，保险起见再试一次取值
        try:
            if raw is not None and hasattr(raw, "__getitem__"):
                return dict(raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
        return None

    @classmethod
    def _is_group_increase(cls, event: AstrMessageEvent) -> bool:
        """只认「新人入群」这一种通知。"""
        raw = cls._raw_event(event)
        if not raw:
            return False
        if str(raw.get("post_type") or "") != "notice":
            return False
        return str(raw.get("notice_type") or "") == "group_increase"

    # ------------------------------------------------------------------
    #  新成员名字
    # ------------------------------------------------------------------
    @classmethod
    def _newcomer_id(cls, event: AstrMessageEvent) -> str:
        """新成员的 QQ 号。

        OneBot 的 group_increase 里 user_id 就是进群的人；
        适配器也把它塞进了 abm.sender.user_id，所以 get_sender_id() 可用。
        raw 优先，拿不到再退回 get_sender_id()。
        """
        raw = cls._raw_event(event) or {}
        uid = str(raw.get("user_id") or "")
        if uid:
            return uid
        try:
            return str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _newcomer_name(self, event: AstrMessageEvent, uid: str) -> str:
        """昵称/群名片；拿不到就用 QQ 号兜底。"""
        name = ""
        # 1) 事件自带的名片/昵称（群成员事件里通常有 card / nickname）
        raw = self._raw_event(event) or {}
        for key in ("card", "nickname"):
            val = str(raw.get(key) or "").strip()
            if val:
                name = val
                break
        # 2) AstrBot 的发送者昵称
        if not name:
            try:
                name = str(event.get_sender_name() or "").strip()
            except Exception:  # noqa: BLE001
                name = ""
        # 3) 问协议端要群成员信息（最准，但多一次调用；失败不影响）
        if not name and uid and self._conf_bool("welcome_fetch_member_name", True):
            name = await self._fetch_member_name(event, uid)
        return name or uid or "新朋友"

    async def _fetch_member_name(self, event: AstrMessageEvent, uid: str) -> str:
        """用 get_group_member_info 拿群名片/昵称。"""
        bot = getattr(event, "bot", None)
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            group_id = ""
        if bot is None or not group_id or not uid:
            return ""
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(uid),
                no_cache=False,
            )
            if isinstance(info, dict):
                return str(
                    info.get("card") or info.get("nickname") or ""
                ).strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[welcome] 取群成员信息失败 {uid}: {exc}")
        return ""

    # ------------------------------------------------------------------
    #  欢迎语
    # ------------------------------------------------------------------
    def _welcome_text(self, name: str) -> str:
        tpl = self._conf_str("welcome_text", "")
        if not tpl:
            tpl = DEFAULT_TEXT
        try:
            return tpl.replace("{name}", name)
        except Exception:  # noqa: BLE001
            return DEFAULT_TEXT.replace("{name}", name)

    # ------------------------------------------------------------------
    #  入口：群消息类型（含被转成消息的 notice）
    # ------------------------------------------------------------------
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_join(self, event: AstrMessageEvent) -> None:
        if not self._conf_bool("welcome_enabled", False):
            return
        if not self._is_group_increase(event):
            return

        uid = self._newcomer_id(event)
        name = await self._newcomer_name(event, uid)
        text = self._welcome_text(name)

        group_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            group_id = ""

        # ★ 不经过 LLM
        try:
            event.should_call_llm(False)
        except Exception:  # noqa: BLE001
            pass

        bot = getattr(event, "bot", None)
        if bot is not None and group_id:
            try:
                await bot.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=text,
                )
                logger.info(f"[welcome] 已迎新 -> 群 {group_id} 新成员 {uid}({name})")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[welcome] 发送欢迎语失败 群 {group_id}: {exc}")

        # 阻断后续处理（避免这类空内容事件再进别的环节）
        try:
            event.stop_event()
        except Exception:  # noqa: BLE001
            pass
