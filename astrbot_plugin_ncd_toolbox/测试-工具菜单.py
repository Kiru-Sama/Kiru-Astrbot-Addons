# -*- coding: utf-8 -*-
"""回归护栏：工具菜单（ncd_tools）只暴露一个入口，点开才解锁其余工具。

为什么做这个：6 个工具的 schema 每轮都要塞进提示词，4B 这类小模型在多个工具里
挑一个会明显变笨（挑错、编工具名、只说不做）。收窄成一个入口后首轮负担从
6 个降到 1 个；模型调用 ncd_tools 一次，本会话才解锁具体工具。

本测试覆盖：
    ① 注册与命名一致（MENU_TOOL 与源码里的装饰器名）
    ② 没收窄 vs 收窄：未点开时只剩菜单，点开后全在
    ③ 按会话隔离（A 点开不影响 B）
    ④ 开关关掉时行为回到"全部常驻"（可回滚）
    ⑤ 能力提示注入：默认值 / 配置覆盖 / 留空不注入 / 幂等

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-工具菜单.py
"""

import asyncio
import pathlib
import re
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


class FakeToolSet:
    """只实现 _apply_tool_menu 用到的那点接口：.tools / .remove_tool()。"""

    def __init__(self, names):
        self.tools = [types.SimpleNamespace(name=n) for n in names]

    def remove_tool(self, name):
        self.tools = [t for t in self.tools if t.name != name]

    def names(self):
        return [t.name for t in self.tools]


def make_plugin(conf=None):
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = dict(conf or {})
    inst._revealed = {}
    inst._draw_revealed = {}
    return inst


def make_event(umo: str):
    return types.SimpleNamespace(unified_msg_origin=umo)


ALL = list(plugin_main.TOOL_NAMES)
DRAW = list(plugin_main.DRAW_TOOLS)
REAL = [n for n in ALL if n != plugin_main.MENU_TOOL]
# ★ 2026-10-19：画图工具已改成常驻（不再有"二级目录藏起来"），
#   所以点开 ncd_tools 之后就应该是全部工具，没有中间态了。

# =====================================================================
#  用例 1：注册与命名一致
# =====================================================================
print("===== 用例 1：注册与命名一致 =====")
src = (PLUGIN / "main.py").read_text(encoding="utf-8")
decorated = re.findall(r'@filter\.llm_tool\(name="([^"]+)"\)', src)
print(f"  MENU_TOOL = {plugin_main.MENU_TOOL!r}")
print(f"  源码里的装饰器: {sorted(decorated)}")
check("用例1", plugin_main.MENU_TOOL == "ncd_tools", "MENU_TOOL 字面量不是 ncd_tools")
check("用例1", plugin_main.MENU_TOOL in decorated,
      "★ 源码里没有 name=\"ncd_tools\" 的装饰器（菜单工具没注册）")
check("用例1", plugin_main.MENU_TOOL in plugin_main.TOOL_NAMES,
      "MENU_TOOL 不在 TOOL_NAMES 里（会被沉浸模式过滤掉）")
# ★ 不写死数字：加一个工具就要改一次测试，那这行断言就成了维护负担。
#   真正要钉的是「TOOL_NAMES 和源码里的装饰器完全一致」，下面按源码算。
check("用例1", set(plugin_main.TOOL_NAMES) == set(decorated),
      f"TOOL_NAMES 与源码装饰器不一致：多 {set(plugin_main.TOOL_NAMES) - set(decorated)}，"
      f"少 {set(decorated) - set(plugin_main.TOOL_NAMES)}")

# =====================================================================
#  用例 2：未点开 → 只剩菜单；点开后 → 全在
# =====================================================================
print("\n===== 用例 2：收窄与解锁 =====")
inst = make_plugin()
ev = make_event("x:FriendMessage:1")

ts = FakeToolSet(ALL + ["astrbot_builtin_tool"])
inst._apply_tool_menu(types.SimpleNamespace(func_tool=ts), ev)
print(f"  未点开: {ts.names()}")
check("用例2", ts.names() == [plugin_main.MENU_TOOL, "astrbot_builtin_tool"],
      f"应只剩菜单和别家工具，实际 {ts.names()}")

inst._revealed[ev.unified_msg_origin] = 1.0
ts2 = FakeToolSet(ALL)
inst._apply_tool_menu(types.SimpleNamespace(func_tool=ts2), ev)
print(f"  已点开 ncd_tools: {ts2.names()}")
check("用例2", sorted(ts2.names()) == sorted(ALL),
      f"点开 ncd_tools 后应全部可见（含画图工具），实际 {ts2.names()}")


# =====================================================================
#  用例 3：按会话隔离
# =====================================================================
print("\n===== 用例 3：会话隔离 =====")
inst3 = make_plugin()
inst3._draw_revealed = {}
ev_a = make_event("x:GroupMessage:A")
ev_b = make_event("x:GroupMessage:B")
inst3._revealed[ev_a.unified_msg_origin] = 1.0
inst3._draw_revealed[ev_a.unified_msg_origin] = 1.0
ta = FakeToolSet(ALL)
tb = FakeToolSet(ALL)
inst3._apply_tool_menu(types.SimpleNamespace(func_tool=ta), ev_a)
inst3._apply_tool_menu(types.SimpleNamespace(func_tool=tb), ev_b)
print(f"  A（两个都点开过）: {len(ta.names())} 个")
print(f"  B（没点过）: {tb.names()}")
check("用例3", len(ta.names()) == len(ALL), f"A 应全在（实际 {len(ta.names())}）")
check("用例3", tb.names() == [plugin_main.MENU_TOOL], f"B 应只剩菜单（实际 {tb.names()}）")

# =====================================================================
#  用例 4：开关关掉 → 不收窄（可回滚）
#     直接驱动 _tune_request，验证真正的接线
# =====================================================================
print("\n===== 用例 4：开关与接线 =====")


def make_req():
    return types.SimpleNamespace(
        system_prompt="你是丝瓜。",
        func_tool=FakeToolSet(ALL),
        extra_user_content_parts=[],
    )


async def run_tune(conf, umo="x:FriendMessage:1"):
    inst4 = make_plugin(conf)
    req = make_req()
    await inst4._tune_request(make_event(umo), req)
    return inst4, req


inst_on, req_on = asyncio.run(run_tune({"tool_menu": True}))
print(f"  tool_menu=True  → {req_on.func_tool.names()}")
check("用例4", req_on.func_tool.names() == [plugin_main.MENU_TOOL],
      f"开菜单应只剩 ncd_tools，实际 {req_on.func_tool.names()}")

inst_off, req_off = asyncio.run(run_tune({"tool_menu": False}))
# ★ 2026-10-18 改：菜单关掉时 ncd_tools 也要摘掉 —— 否则模型（尤其 DeepSeek）
#   会先点它拿清单、再调真正的工具，白花一次往返和几百 token。
# ★ 2026-10-19 再改：画图工具常驻（不再藏），所以关菜单时它们也照常在。
expected_off = sorted(n for n in REAL)
print(f"  tool_menu=False → {len(req_off.func_tool.names())} 个（应 {len(expected_off)}，只去掉 ncd_tools）")
check("用例4", sorted(req_off.func_tool.names()) == expected_off,
      f"关菜单应保留全部真实工具（含画图工具）、只去掉 ncd_tools，"
      f"实际 {req_off.func_tool.names()}")
check("用例4", set(plugin_main.DRAW_TOOLS) <= set(req_off.func_tool.names()),
      "★ 画图工具应该常驻，关菜单时也在")



# =====================================================================
#  用例 5：能力提示注入
# =====================================================================
print("\n===== 用例 5：能力提示注入 =====")
# 默认（配置里没有这个键）
inst5 = make_plugin({"tool_menu": True})
check("用例5", inst5._menu_hint_text() == plugin_main.DEFAULT_MENU_HINT,
      "缺配置时应落到默认提示")

req5 = make_req()
inst5._inject_menu_hint(req5)
print(f"  注入后 system_prompt 长度 {len(req5.system_prompt)}")
check("用例5", "ncd_tools" in req5.system_prompt, "★ 默认提示里没提到 ncd_tools")
check("用例5", req5.system_prompt.startswith("你是丝瓜。"), "不该破坏原系统提示词")

# 幂等：再来一次不应变长
before_len = len(req5.system_prompt)
inst5._inject_menu_hint(req5)
check("用例5", len(req5.system_prompt) == before_len, "★ 重复注入（不幂等）")

# 配置覆盖
inst5b = make_plugin({"tool_menu_hint": "记得用 ncd_tools 查工具。"})
check("用例5", inst5b._menu_hint_text() == "记得用 ncd_tools 查工具。", "配置没覆盖默认值")
req5b = make_req()
inst5b._inject_menu_hint(req5b)
check("用例5", req5b.system_prompt.endswith("记得用 ncd_tools 查工具。"), "自定义提示没拼到末尾")

# 留空 = 不注入
inst5c = make_plugin({"tool_menu_hint": "   "})
req5c = make_req()
inst5c._inject_menu_hint(req5c)
print(f"  留空时 system_prompt = {req5c.system_prompt!r}")
check("用例5", req5c.system_prompt == "你是丝瓜。", "★ 留空仍注入了")
check("用例5", inst5c._menu_hint_text() == "", "留空时 _menu_hint_text 应返回空串")

# 关掉菜单时不该注入（只在 tool_menu 开时注入）
inst5d, req5d = asyncio.run(run_tune({"tool_menu": False}))
check("用例5", plugin_main.DEFAULT_MENU_HINT not in (req5d.system_prompt or ""),
      "★ 菜单关着还在注入能力提示")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 命名一致 / 收窄与解锁 / 会话隔离 / 开关回滚 / 提示注入 全部正确")


