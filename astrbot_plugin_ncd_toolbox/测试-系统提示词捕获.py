# -*- coding: utf-8 -*-
"""回归护栏：要能证明「注入真的进了最终系统提示词」。

背景（2026-10-17）：
    用户说"我还是看不到注入的提示词"。查下来不是没注入，而是**根本没地方
    记录最终发出去的那段** —— 面板上显示的是"你填的值"，不是"发出去的结果"。
    所以现在每轮都记一份 `req.system_prompt`，本测试保证：

        ① 记录里确实有那段提示（has_hint=True）
        ② 没有提示时 has_hint=False（不能恒为 True，否则这个标志没意义）
        ③ 记录的是【最终】状态 —— 提示是追加在末尾的，原文必须完整保留
        ④ 只留最近 5 条，不会无限涨

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-系统提示词捕获.py
"""

import asyncio
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


def make_plugin(conf=None):
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = dict(conf or {})
    inst._revealed = {}
    inst._orig_descriptions = {}
    inst._prompt_log = plugin_main.deque(maxlen=5)
    return inst


def make_req(sp="你是丝瓜，说话简短。"):
    return types.SimpleNamespace(
        system_prompt=sp,
        func_tool=None,
        extra_user_content_parts=[],
    )


def make_event(umo="x:FriendMessage:1"):
    return types.SimpleNamespace(unified_msg_origin=umo)


# =====================================================================
#  用例 1：注入的提示确实出现在最终系统提示词里
# =====================================================================
print("===== 用例 1：注入进了最终提示词 =====")
inst = make_plugin({"tool_menu": True})
req = make_req()
asyncio.run(inst._tune_request(make_event(), req))
print(f"  最终提示词 {len(req.system_prompt)} 字")
check("用例1", len(inst._prompt_log) == 1, f"应记 1 条（实际 {len(inst._prompt_log)}）")
if inst._prompt_log:
    rec = inst._prompt_log[0]
    print(f"  has_hint = {rec['has_hint']}  chars = {rec['chars']}  umo = {rec['umo']}")
    check("用例1", rec["has_hint"] is True, "★记录里没有那段提示（注入没生效？）")
    check("用例1", plugin_main.DEFAULT_MENU_HINT in rec["prompt"],
          "★提示不在捕获到的系统提示词里")
    check("用例1", rec["chars"] == len(req.system_prompt), "字符数记错了")
    check("用例1", rec["umo"] == "x:FriendMessage:1", "会话没记对")
    check("用例1", rec["prompt"].startswith("你是丝瓜"), "原文被破坏（必须完整保留）")

# =====================================================================
#  用例 2：没有提示时 has_hint 必须是 False
# =====================================================================
print("\n===== 用例 2：不注入时 has_hint=False =====")
inst2 = make_plugin({"tool_menu": True, "tool_menu_hint": ""})
req2 = make_req()
asyncio.run(inst2._tune_request(make_event(), req2))
rec2 = inst2._prompt_log[0]
print(f"  has_hint = {rec2['has_hint']}  chars = {rec2['chars']}")
check("用例2", rec2["has_hint"] is False, "★没注入却报 has_hint=True（标志没意义）")
check("用例2", rec2["prompt"] == "你是丝瓜，说话简短。", "留空时不该往提示词里加东西")

# =====================================================================
#  用例 3：关掉菜单时也不注入
# =====================================================================
print("\n===== 用例 3：菜单关掉不注入 =====")
inst3 = make_plugin({"tool_menu": False})
req3 = make_req()
asyncio.run(inst3._tune_request(make_event(), req3))
rec3 = inst3._prompt_log[0]
check("用例3", rec3["has_hint"] is False, "★菜单关着仍在注入")

# =====================================================================
#  用例 4：只留最近 5 条
# =====================================================================
print("\n===== 用例 4：最多 5 条 =====")
inst4 = make_plugin({"tool_menu": True})
for i in range(8):
    asyncio.run(inst4._tune_request(make_event(f"x:FriendMessage:{i}"), make_req()))
print(f"  发了 8 轮，日志里 {len(inst4._prompt_log)} 条")
check("用例4", len(inst4._prompt_log) == 5, f"应封顶 5 条（实际 {len(inst4._prompt_log)}）")
check("用例4", inst4._prompt_log[0]["umo"] == "x:FriendMessage:7",
      "最新的应该排最前")

# =====================================================================
#  用例 5：记录失败不能影响这一轮
# =====================================================================
print("\n===== 用例 5：记录失败不影响对话 =====")
inst5 = make_plugin({"tool_menu": True})
inst5._prompt_log = None  # 故意弄坏，模拟记录环节出错


class _Boom:
    @property
    def system_prompt(self):
        return "hello"

    @system_prompt.setter
    def system_prompt(self, v):
        pass

    func_tool = None
    extra_user_content_parts = []


try:
    asyncio.run(inst5._tune_request(make_event(), _Boom()))
    check("用例5", True, "")
    print("  没有抛异常 ✓（日志里会有一条 error，属于预期）")
except Exception as exc:  # noqa: BLE001
    check("用例5", False, f"★记录环节出错把整轮带挂了：{type(exc).__name__}: {exc}")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 注入进最终提示词 / 不注入时标志正确 / 关菜单不注入 / 封顶 5 条 / 记录失败不影响对话")
