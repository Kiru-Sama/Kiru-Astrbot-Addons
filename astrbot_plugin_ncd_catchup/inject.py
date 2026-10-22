# -*- coding: utf-8 -*-
"""把 OneBot 的一条历史消息转成 AstrBot 的事件对象。

单独拆出来是为了能离线单测：不依赖真实 QQ 连接。
"""

from __future__ import annotations

from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType

try:  # AstrBot 以包形式加载
    import astrbot.api.message_components as Comp
except ImportError:  # pragma: no cover
    Comp = None


def extract_text(raw: dict) -> str:
    """从 OneBot 消息段里抽纯文本；没有文字就返回空串（图片/表情不补）。"""
    segments = raw.get("message") or []
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "text":
            text = (seg.get("data") or {}).get("text") or ""
            if text:
                parts.append(str(text))
        elif seg.get("type") == "at":
            name = (seg.get("data") or {}).get("qq") or ""
            if name:
                parts.append(f"@{name}")
    text = "".join(parts).strip()
    if text:
        return text
    # 有些协议端只给 raw_message 字符串
    raw_str = str(raw.get("raw_message") or "").strip()
    return raw_str if raw_str and not raw_str.startswith("[CQ:") else ""


def build_message(
    *,
    self_id: str,
    user_id: str,
    nickname: str,
    text: str,
    message_id: str,
    timestamp: int,
) -> "AstrBotMessage":
    """构造一条私聊消息事件（和适配器 create_event 需要的字段对齐）。"""
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE
    msg.self_id = str(self_id)
    msg.session_id = str(user_id)
    msg.message_id = str(message_id or f"catchup-{timestamp}")
    msg.group = None
    msg.sender = MessageMember(user_id=str(user_id), nickname=nickname or str(user_id))
    msg.message = [Comp.Plain(text)] if Comp is not None else []
    msg.message_str = text
    msg.raw_message = {"post_type": "message", "message_type": "private", "raw_message": text}
    msg.timestamp = int(timestamp)
    return msg
