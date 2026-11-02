# -*- coding: utf-8 -*-
"""monitor 的文本工具（纯函数，不依赖 AstrBot）。

从 main.py 搬出来的第一批（2026-11-02）：思考剥离、流式 chunk 解析、内部请求识别。
这些函数原来散在 5793 行的 main.py 里，且只有 main 自己用；
搬出来之后可以离线测（见 测试-text_utils.py），main.py 用别名导入保持调用点不变。
"""

from __future__ import annotations

from typing import Any

# 跨插件约定：工具箱的「网页总结」调用会在 prompt 开头带上这个标记自报身份，
# 本插件据此把它排除在会话统计之外。两侧都各自定义了一份，改一处要同步另一处。
# （toolbox/main.py 的 _summarize_page 里是同一个字面量。）
INTERNAL_REQUEST_MARKERS = ("[内部·网页总结]",)


def is_think_part(part: Any) -> bool:
    """这个内容块是不是"思考"。

    运行时可能是 ThinkPart（pydantic 对象），也可能是 dict 形态的
    {"type": "think", "think": ...} —— 两种都认。
    """
    if isinstance(part, dict):
        return part.get("type") == "think"
    return getattr(part, "type", None) == "think"


def strip_think_parts(messages: list[Any]) -> int:
    """把消息里的 ThinkPart 摘掉，返回摘掉了几段。

    ★ 只动 ThinkPart，别的都不碰：
      · 正文（TextPart）原样保留；
      · **tool_calls 必须留着** —— 摘掉它工具链就断了；
      · 摘完这条消息什么都不剩（纯思考）就整条丢弃，免得给供应商一个空壳。
    ★ 原地改传进来的那个列表（messages[:] = …）：AstrBot 在压缩决策和落盘时
      用的就是同一个列表对象，换成新对象等于没改。
    """
    removed = 0
    kept = []
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            kept.append(msg)
            continue
        remaining = [p for p in content if not is_think_part(p)]
        if len(remaining) != len(content):
            removed += len(content) - len(remaining)
            if not remaining and not getattr(msg, "tool_calls", None):
                continue
            msg.content = remaining
        kept.append(msg)
    if removed:
        messages[:] = kept
    return removed


def count_think_chars(messages: list[Any]) -> int:
    """数一下这些消息里思考一共多少字（剥离前调用，用于记账）。"""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for p in content:
            if not is_think_part(p):
                continue
            if isinstance(p, dict):
                total += len(p.get("think") or "")
            else:
                total += len(getattr(p, "think", "") or "")
    return total


def is_delta_chunk(chunk: Any) -> bool:
    """这个流式 chunk 是【增量片段】还是【最终完整结果】。"""
    return bool(getattr(chunk, "is_chunk", False))


def plugin_content_text(content: Any) -> str:
    """content → 够判断前缀的一小段文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    return str(content)


def is_internal_request(contexts: Any, prompt: str) -> bool:
    """这次 provider 调用是不是本插件之外的内部调用（不是某个会话的对话）。"""
    candidates = [prompt or ""]
    for m in contexts or []:
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content")
        candidates.append(plugin_content_text(content))
        break
    for text in candidates:
        stripped = (text or "").lstrip()
        if any(stripped.startswith(mark) for mark in INTERNAL_REQUEST_MARKERS):
            return True
    return False


def has_final_text(chunk: Any) -> bool:
    """这个非 delta chunk 是不是【真的】带着完整文本的最终结果。"""
    text = getattr(chunk, "completion_text", None)
    if isinstance(text, str) and text.strip():
        return True
    chain = getattr(chunk, "result_chain", None)
    for comp in getattr(chain, "chain", None) or []:
        part = getattr(comp, "text", None)
        if isinstance(part, str) and part.strip():
            return True
    return False


def chunk_text(chunk: Any) -> str:
    """取出这个增量 chunk 里【新增的那段文本】。"""
    chain = getattr(chunk, "result_chain", None)
    parts: list[str] = []
    for comp in getattr(chain, "chain", None) or []:
        text = getattr(comp, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if parts:
        return "".join(parts)
    text = getattr(chunk, "completion_text", None)
    return text if isinstance(text, str) else ""
