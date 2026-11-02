# -*- coding: utf-8 -*-
"""text_utils 的离线测试（monitor 拆分第一批）。

这些函数原来埋在 monitor/main.py（5793 行）里，只能靠整机跑；
搬出来后用假对象直接测行为：思考剥离、流式 chunk 判别、内部请求识别。
"""

from __future__ import annotations

import os
import sys

MON_DIR = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_monitor"
sys.path.insert(0, MON_DIR)

import text_utils as tu  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, fn):
    try:
        detail = fn() or ""
        results.append((name, True, detail))
        print(f"  PASS  {name}  — {detail}")
    except AssertionError as exc:
        results.append((name, False, str(exc)))
        print(f"  FAIL  {name}\n        └─ {exc}")
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"  ERROR {name}\n        └─ {type(exc).__name__}: {exc}")


class ThinkPart:
    type = "think"

    def __init__(self, think):
        self.think = think


class TextPart:
    type = "text"

    def __init__(self, text):
        self.text = text


class Msg:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class Comp:
    def __init__(self, text):
        self.text = text


class Chain:
    def __init__(self, texts):
        self.chain = [Comp(t) for t in texts]


class Chunk:
    def __init__(self, is_chunk=True, texts=None, completion_text=""):
        self.is_chunk = is_chunk
        self.result_chain = Chain(texts or [])
        self.completion_text = completion_text


def main() -> int:
    print("=" * 70)
    print("text_utils 行为测试（monitor 拆分）")
    print("=" * 70)

    def t_is_think():
        assert tu.is_think_part({"type": "think", "think": "x"}) is True
        assert tu.is_think_part(ThinkPart("x")) is True
        assert tu.is_think_part(TextPart("x")) is False
        assert tu.is_think_part("plain") is False
        return "dict / 对象两种形态都认"

    check("is_think_part", t_is_think)

    def t_strip():
        msgs = [
            Msg([ThinkPart("想 1"), TextPart("正文")]),
            Msg([ThinkPart("想 2")]),                       # 纯思考 → 整条丢
            Msg([ThinkPart("想 3")], tool_calls=["t"]),      # 有 tool_calls → 保留
            Msg("字符串内容不变"),
        ]
        n = tu.strip_think_parts(msgs)
        assert n == 3, f"应摘 3 段，实际 {n}"
        assert len(msgs) == 3, f"纯思考那条应被丢弃，剩 {len(msgs)} 条"
        assert isinstance(msgs[-1].content, str), "字符串内容被改了"
        assert msgs[1].content == [], "带 tool_calls 的消息应保留空 content"
        return f"摘 {n} 段、丢 1 条纯思考、tool_calls 保留"

    check("strip_think_parts 原地修改 + 丢空壳", t_strip)

    def t_count():
        msgs = [Msg([ThinkPart("12345"), TextPart("x")]), Msg([{"type": "think", "think": "abc"}])]
        assert tu.count_think_chars(msgs) == 8, tu.count_think_chars(msgs)
        return "5 + 3 = 8 字"

    check("count_think_chars", t_count)

    def t_delta():
        assert tu.is_delta_chunk(Chunk(is_chunk=True)) is True
        assert tu.is_delta_chunk(Chunk(is_chunk=False)) is False
        assert tu.is_delta_chunk(object()) is False
        return "is_chunk 真值判断"

    check("is_delta_chunk", t_delta)

    def t_chunk_text():
        assert tu.chunk_text(Chunk(texts=["a", "b"])) == "ab"
        assert tu.chunk_text(Chunk(texts=[], completion_text="完整")) == "完整"
        assert tu.chunk_text(object()) == ""
        return "优先 result_chain，回落 completion_text"

    check("chunk_text", t_chunk_text)

    def t_final():
        assert tu.has_final_text(Chunk(texts=[], completion_text="完整")) is True
        assert tu.has_final_text(Chunk(texts=["增量"])) is True
        assert tu.has_final_text(Chunk(texts=[], completion_text="  ")) is False
        return "空壳（只有空白）判 False"

    check("has_final_text", t_final)

    def t_internal():
        assert tu.is_internal_request([], "[内部·网页总结] 正文") is True
        assert tu.is_internal_request([], "   [内部·网页总结] 带前导空格") is True
        assert tu.is_internal_request([], "普通对话") is False
        assert tu.is_internal_request([Msg([TextPart("[内部·网页总结] x")])], "") is True
        return "prompt 与首条消息两条路径都命中"

    check("is_internal_request", t_internal)

    ok = sum(1 for _, p, _ in results if p)
    bad = [r for r in results if not r[1]]
    print()
    print(f"汇总：{ok}/{len(results)} 通过" + ("" if not bad else f"，{len(bad)} 项失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
