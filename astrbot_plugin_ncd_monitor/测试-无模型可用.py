# -*- coding: utf-8 -*-
"""回归护栏：出现「无模型可用」时要留下现场，而不是只吐一句无从下手的文案。

背景（2026-10-17 查源码确认）：
    聊天里偶尔出现
        All available chat models are unavailable.
    AstrBot 把它当普通回复吐出来，没有任何上下文。查
    tool_loop_agent_runner.py:635-650 才知道它有**两个**分支：

        candidates = [self.provider, *self.fallback_providers]   # 至少 1 个
        if last_exception:  → "All chat models failed: <类型>: <消息>"   # 有真异常
        else:               → "All available chat models are unavailable."

    候选至少有一个，所以走到第二句只能是：**每个候选都回了空流**。
    也就是说它不是"没配模型"，而是"模型回了空"——这个结论得记下来。

本测试覆盖：
    ① 认得出这句话（且不误认带真异常的那个分支）
    ② 现场记录的内容（候选 / 上下文条数 / chunk 数）
    ③ 记录封顶，不会无限涨
    ④ 包装装上之后可重复调用、不重复包装

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-无模型可用.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""

import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


# =====================================================================
#  用例 1：认句子（原文照抄 AstrBot 的两个分支）
# =====================================================================
print("===== 用例 1：识别「无模型可用」 =====")
cases = {
    "All available chat models are unavailable.": True,
    # 另一个分支：带真异常，本来就有信息，不该被当成"无模型可用"
    "All chat models failed: TimeoutError: timed out": False,
    "": False,
    "画好啦，看看～": False,
}
for text, expect in cases.items():
    got = plugin_main._is_lm_unavailable(text)
    print(f"  {text[:52]!r:56} → {got}（应 {expect}）")
    check("用例1", got == expect, f"_is_lm_unavailable({text!r}) 应为 {expect}")


# =====================================================================
#  用例 2：现场记录的内容
# =====================================================================
print("\n===== 用例 2：现场记录内容 =====")


def make_plugin():
    inst = plugin_main.Main.__new__(plugin_main.Main)
    inst._conf = {}
    inst._lm_unavailable = __import__("collections").deque(maxlen=3)
    inst.last_prompt_tokens = 12345
    return inst


def make_runner(ids, n_messages):
    cands = [
        types.SimpleNamespace(provider_config={"id": pid}) for pid in ids
    ]
    return types.SimpleNamespace(
        provider=cands[0],
        fallback_providers=cands[1:],
        run_context=types.SimpleNamespace(messages=[None] * n_messages),
    )


inst = make_plugin()
inst._record_lm_unavailable(make_runner(["lm-sdd/qwen3.8-4b", "deepseek-account"], 27), 0)
recs = list(inst._lm_unavailable)
print(f"  记录条数 {len(recs)}")
print(f"  {recs[0] if recs else None}")
check("用例2", len(recs) == 1, f"应记 1 条（实际 {len(recs)}）")
if recs:
    r = recs[0]
    check("用例2", r["candidates"] == ["lm-sdd/qwen3.8-4b", "deepseek-account"],
          f"候选列表不对：{r['candidates']}")
    check("用例2", r["messages"] == 27, f"上下文条数不对：{r['messages']}")
    check("用例2", r["chunks"] == 0, f"chunk 数不对：{r['chunks']}")
    check("用例2", r["prompt_tokens"] == 12345, f"prompt token 不对：{r['prompt_tokens']}")
    check("用例2", "ts" in r, "缺时间戳")

# 空候选（provider 为 None）也不能炸
inst._record_lm_unavailable(types.SimpleNamespace(provider=None), 3)
check("用例2", len(inst._lm_unavailable) == 2, "provider 为 None 时也要能记下（不能抛）")

# =====================================================================
#  用例 3：记录封顶（deque maxlen=3）
# =====================================================================
print("\n===== 用例 3：记录封顶 =====")
for i in range(10):
    inst._record_lm_unavailable(make_runner(["p"], i), 0)
print(f"  连记 10 次后条数 = {len(inst._lm_unavailable)}（上限 3）")
check("用例3", len(inst._lm_unavailable) == 3, f"应封顶 3 条（实际 {len(inst._lm_unavailable)}）")

# =====================================================================
#  用例 4：包装能装上、且重复调用不会重复包装
# =====================================================================
print("\n===== 用例 4：包装安装与幂等 =====")
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner  # noqa: E402

cls = ToolLoopAgentRunner
original_before = cls.__dict__.get("_iter_llm_responses_with_fallback")
inst4 = make_plugin()
inst4._patch_lm_diag()
patched = cls.__dict__.get("_iter_llm_responses_with_fallback")
check("用例4", cls.__dict__.get("_ncd_monitor_patched_lmdiag") is True, "标记没装上")
check("用例4", patched is not original_before, "★方法没被换掉（包装没生效）")

# 再装一次：应该跳过，方法对象不应该再变
inst4._patch_lm_diag()
check("用例4", cls.__dict__.get("_iter_llm_responses_with_fallback") is patched,
      "重复包装了（应该幂等跳过）")

# 卸载还原：模拟 terminate() 的还原逻辑
wraps = [w for w in plugin_main._WRAPS if w[1] == "_iter_llm_responses_with_fallback"]
check("用例4", len(wraps) == 1, f"应该只登记 1 条还原记录（实际 {len(wraps)}）")
if wraps:
    cls2, attr2, orig2, marker2 = wraps[0]
    setattr(cls2, attr2, orig2)
    delattr(cls2, marker2)
    plugin_main._WRAPS.remove(wraps[0])
    check("用例4", cls.__dict__.get("_iter_llm_responses_with_fallback") is original_before,
          "★还原后不是原来那个方法")
    check("用例4", "_ncd_monitor_patched_lmdiag" not in cls.__dict__, "还原后标记没删掉")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 识别文案 / 现场内容 / 记录封顶 / 包装安装与幂等还原 全部正确")
