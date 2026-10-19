# -*- coding: utf-8 -*-
"""上下文超感知：把更长的历史整理成一段可读的转写文本。

为什么需要单独一个模块：
  · 这一步全是纯字符串处理（角色名、截断、图片占位、总量封顶），
    和网络/数据库无关，放独立文件才好测。
  · 容易踩的坑都在这里：多模态 content 是列表、tool 结果是 JSON、
    单条太长会把 token 预算吃光、总量不封顶会一次塞爆上下文。
"""

from __future__ import annotations

import json
import re

# 允许的档位。模型只能选这四个（其它值就近取整）——
# 档位太少会导致"想要 40 条"没得选，太多会让模型挑花眼。
ALLOWED_COUNTS = (30, 50, 70, 90)
DEFAULT_COUNT = 30

# 单条消息最多取多少字符；整段最多多少字符。
# ★ 90 条 × 180 字 ≈ 16k 字（约 8k token），已经不小了，所以要封顶。
PER_MSG_CHARS = 180
TOTAL_CHARS = 14000

_ROLE_NAMES = {
    "user": "对方",
    "assistant": "我",
    "system": "系统",
    "tool": "工具结果",
    "function": "工具结果",
}


def snap_count(value, default: int = DEFAULT_COUNT) -> int:
    """把任意输入折算到允许的档位（就近取整）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(ALLOWED_COUNTS, key=lambda c: abs(c - n))


def _content_to_text(content) -> str:  # noqa: ANN001
    """把 OpenAI 格式的 content 压成纯文本。

    · str → 原样
    · list（多模态）→ 取其中的 text 段；图片一律记成 [图片]
    · 其它 → JSON 字符串兜底
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            kind = str(item.get("type") or "")
            if kind in ("text", "input_text"):
                parts.append(str(item.get("text") or ""))
            elif kind in ("image_url", "input_image", "image"):
                parts.append("[图片]")
            elif kind in ("audio", "input_audio"):
                parts.append("[语音]")
            else:
                # 未知结构：把 text 字段捞出来，没有就整体当 JSON
                if item.get("text"):
                    parts.append(str(item["text"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False)[:120])
        return " ".join(p for p in parts if p)
    if isinstance(content, dict):
        if content.get("text"):
            return str(content["text"])
        return json.dumps(content, ensure_ascii=False)
    return str(content)


_NOISE_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)


def strip_noise(text: str) -> str:
    """去掉 AstrBot 自己塞进消息里的内部标记。

    ★ 实测真实历史里，user 的一条消息长这样：
        "你好啊 <system_reminder>User ID: 10000002, Nickname: ...</system_reminder>"
      这些标记本来就在模型上下文里别处可见，转写时再抄一遍纯属白烧 token。
    """
    if not text:
        return text
    return _NOISE_RE.sub("", text).strip()


def format_transcript(
    messages: list,
    *,
    limit: int = DEFAULT_COUNT,
    per_msg_chars: int = PER_MSG_CHARS,
    total_chars: int = TOTAL_CHARS,
) -> str:
    """把最后 limit 条消息整理成带序号的转写。

    ★ 从【最新】往旧的方向截总量：宁可丢最早的几条，也别把最近的话截掉 ——
      模型要判断"刚才说到哪"，靠的就是最近那几条。
    """
    limit = snap_count(limit)
    rows = [m for m in (messages or []) if isinstance(m, dict)]
    if not rows:
        return "（这个会话还没有可读的历史）"

    picked = [
        m
        for m in rows[-limit:]
        if _content_to_text(m.get("content")).strip()
    ]
    if not picked:
        return "（最近这些消息里没有可读的文字内容）"

    lines: list[str] = []
    used = 0
    # 倒着遍历，先保证最新的在里面
    for offset, msg in enumerate(reversed(picked), start=1):
        role = str(msg.get("role") or "?").lower()
        name = _ROLE_NAMES.get(role, role)
        text = " ".join(strip_noise(_content_to_text(msg.get("content"))).split())
        if len(text) > per_msg_chars:
            text = text[:per_msg_chars] + "…（截断）"
        line = f"[{len(picked) - offset + 1}] {name}: {text}"
        if used + len(line) > total_chars:
            lines.append(f"（更早的 {len(picked) - len(lines)} 条因为太长没放进来）")
            break
        lines.append(line)
        used += len(line)

    lines.reverse()
    head = f"以下是这个会话最近的 {len(picked)} 条（从旧到新）："
    return "\n".join([head, ""] + lines)
