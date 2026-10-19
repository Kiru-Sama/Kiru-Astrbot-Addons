"""自测：沉浸模式的两件事 —— 剥离 AstrBot 注入的工具说明、只保留本插件工具。

不需要 AstrBot 在跑：直接构造 req 对象喂给钩子。
用法：<AstrBot>\\.venv\\Scripts\\python.exe 测试-沉浸模式.py
"""

import asyncio
import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from astrbot.core.agent.tool import FunctionTool, ToolSet  # noqa: E402
from astrbot.core.astr_main_agent_resources import (  # noqa: E402
    TOOL_CALL_PROMPT,
    TOOL_CALL_PROMPT_SKILLS_LIKE_MODE,
)

import main as plugin_main  # noqa: E402

failures: list[str] = []
ALL_TOOLS = list(plugin_main.TOOL_NAMES) + [
    "send_message_to_user",
    "get_group_message_history",
]
PERSONA = "你是测试助手。\n\n# Persona Instructions\n\n保持人设。\n"


def make_req(prompt: str, tool_names: list[str]):
    toolset = ToolSet()
    for name in tool_names:
        toolset.add_tool(
            FunctionTool(name=name, description=f"{name} 的描述", parameters={})
        )
    return types.SimpleNamespace(system_prompt=prompt, func_tool=toolset)


def make_plugin(rp_mode: bool, rp_mode_half: bool = False):
    """绕过 __init__，只放配置进去，专注测钩子逻辑。"""
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = {"rp_mode": rp_mode, "rp_mode_half": rp_mode_half}
    return inst


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


# --- 情况 1：沉浸模式开
req = make_req(PERSONA + f"\n{TOOL_CALL_PROMPT}\n", ALL_TOOLS)
asyncio.run(make_plugin(True)._tune_request(None, req))
print("=== 沉浸模式开")
print(f"  还有工具说明吗 = {TOOL_CALL_PROMPT in req.system_prompt}")
print(f"  剩余工具 = {[t.name for t in req.func_tool.tools]}")
check("情况1", TOOL_CALL_PROMPT not in req.system_prompt, "没剥掉工具说明")
check("情况1", "\n\n\n" not in req.system_prompt, "剥离后残留连续空行")
check("情况1", "测试助手" in req.system_prompt, "误删人格提示词")
check("情况1", "保持人设" in req.system_prompt, "误删人格提示词")
left = {t.name for t in req.func_tool.tools}
check("情况1", left == set(plugin_main.TOOL_NAMES), f"剩余工具不对：{left}")

# --- 情况 2：另一个提示词变体（tool_schema_mode != full）
req2 = make_req(PERSONA + f"\n{TOOL_CALL_PROMPT_SKILLS_LIKE_MODE}\n", ALL_TOOLS)
asyncio.run(make_plugin(True)._tune_request(None, req2))
print("\n=== 沉浸模式开（skills-like 变体）")
print(f"  还有工具说明吗 = {TOOL_CALL_PROMPT_SKILLS_LIKE_MODE in req2.system_prompt}")
check(
    "情况2",
    TOOL_CALL_PROMPT_SKILLS_LIKE_MODE not in req2.system_prompt,
    "没剥掉 skills-like 变体",
)

# --- 情况 3：沉浸模式关，什么都不该动
req3 = make_req(PERSONA + f"\n{TOOL_CALL_PROMPT}\n", ALL_TOOLS)
asyncio.run(make_plugin(False)._tune_request(None, req3))
print("\n=== 沉浸模式关")
print(f"  工具说明保留 = {TOOL_CALL_PROMPT in req3.system_prompt}")
print(f"  工具数量 = {len(req3.func_tool.tools)}")
check("情况3", TOOL_CALL_PROMPT in req3.system_prompt, "关闭时不该改提示词")
check("情况3", len(req3.func_tool.tools) == len(ALL_TOOLS), "关闭时不该屏蔽工具")

# --- 情况 4：没有工具时不崩
asyncio.run(make_plugin(True)._tune_request(None, make_req(PERSONA, [])))
print("\n=== 无工具时不崩 = OK")

# --- 情况 5：半沉浸模式 —— 语气剥掉、行为约束必须留下
# 实测动机：切到 4B 后模型开始"只回台词、不真调用工具"，
# 而整段剥掉的那句里恰好有 "never return an empty response"。
KEEP = ["never return an empty response", "do not invent parameters"]
DROP = ["briefly explain the purpose", "keep the conversation style consistent"]

req5 = make_req(PERSONA + f"\n{TOOL_CALL_PROMPT}\n", ALL_TOOLS)
asyncio.run(make_plugin(False, rp_mode_half=True)._tune_request(None, req5))
sp5 = req5.system_prompt
print("\n=== 半沉浸模式")
print(f"  整段还在吗 = {TOOL_CALL_PROMPT in sp5}（应为 False）")
for k in KEEP:
    print(f"  保留「{k}」= {k in sp5}")
for d in DROP:
    print(f"  剥掉「{d}」= {d not in sp5}")
check("情况5", TOOL_CALL_PROMPT not in sp5, "整段原文还在（应已裁短）")
for k in KEEP:
    check("情况5", k in sp5, f"★行为约束被误删了：{k}")
for d in DROP:
    check("情况5", d not in sp5, f"语气分句没剥掉：{d}")
check("情况5", "\n\n\n" not in sp5, "裁剪后残留连续空行")
check("情况5", "测试助手" in sp5, "误删人格提示词")
left5 = {t.name for t in req5.func_tool.tools}
check("情况5", left5 == set(plugin_main.TOOL_NAMES), f"半沉浸也应屏蔽自带工具：{left5}")

# --- 情况 6：两个都开时，沉浸模式优先（档位要可预期，不做魔法）
req6 = make_req(PERSONA + f"\n{TOOL_CALL_PROMPT}\n", ALL_TOOLS)
asyncio.run(make_plugin(True, rp_mode_half=True)._tune_request(None, req6))
print("\n=== 两个都开（应 full 优先）")
print(f"  整段已移除 = {TOOL_CALL_PROMPT not in req6.system_prompt}")
print(f"  行为约束也没了 = {KEEP[0] not in req6.system_prompt}")
check("情况6", TOOL_CALL_PROMPT not in req6.system_prompt, "两个都开时应整段移除")
check(
    "情况6",
    KEEP[0] not in req6.system_prompt,
    "两个都开时应按 full 处理（行为约束一起移除），而不是裁短",
)

# --- 情况 7：认不出的新措辞必须【原样保留】，不能把行为约束丢掉
req7 = make_req(PERSONA + "\nTotally new prompt from a future AstrBot.\n", ALL_TOOLS)
asyncio.run(make_plugin(False, rp_mode_half=True)._tune_request(None, req7))
print("\n=== 半沉浸 + 不认识的措辞")
print(f"  原样保留 = {'Totally new prompt' in req7.system_prompt}")
check(
    "情况7",
    "Totally new prompt" in req7.system_prompt,
    "不认识的措辞不该被动（保守：宁可留语气，也不能丢约束）",
)

# --- 情况 8：skills-like 变体也要裁对（它的语气句是另一种写法）
# ★ 实测漏过：那一版把同一句写成 "Keep the role-play and style consistent…"，
#   只按第一种写法做关键词匹配会漏掉它。
req8 = make_req(PERSONA + f"\n{TOOL_CALL_PROMPT_SKILLS_LIKE_MODE}\n", ALL_TOOLS)
asyncio.run(make_plugin(False, rp_mode_half=True)._tune_request(None, req8))
sp8 = req8.system_prompt
print("\n=== 半沉浸（skills-like 变体）")
for k in ("You MUST NOT return an empty response", "Do not guess arguments"):
    print(f"  保留「{k[:38]}」= {k in sp8}")
for d in ("provide a brief explanatory message", "Keep the role-play and style consistent"):
    print(f"  剥掉「{d[:38]}」= {d not in sp8}")
check("情况8", "You MUST NOT return an empty response" in sp8, "★行为约束被误删")
check("情况8", "Do not guess arguments" in sp8, "★行为约束被误删")
check("情况8", "provide a brief explanatory message" not in sp8, "语气分句没剥掉（第一种写法）")
check(
    "情况8",
    "Keep the role-play and style consistent" not in sp8,
    "语气分句没剥掉（第二种写法 —— 这里漏过）",
)
check(
    "情况8",
    "you must briefly summarize the results returned by the tool" in sp8,
    "「工具调用后要总结结果」是行为约束，不能被当语气剥掉",
)

if failures:
    print("\n失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("\nPASS: 沉浸模式的剥离与屏蔽行为全部正确")
