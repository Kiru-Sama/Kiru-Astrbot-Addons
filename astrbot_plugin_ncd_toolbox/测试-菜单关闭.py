# -*- coding: utf-8 -*-
"""回归护栏：工具菜单关掉时，ncd_tools 必须从工具集里消失。

背景（2026-10-18 实测）：`tool_menu=false` 后，DeepSeek 仍然先调一次
ncd_tools 拿清单、再调真正的工具 —— 多一次往返、多几百 token，
而且清单里还列着它自己。根因：菜单关掉时根本没调用 _apply_tool_menu，
ncd_tools 原样留在请求里。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-菜单关闭.py
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


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeToolSet:
    """够用的替身：只要 tools / remove_tool / add_tool。"""

    def __init__(self, names) -> None:
        self._d = {n: FakeTool(n) for n in names}

    @property
    def tools(self):
        return list(self._d.values())

    @property
    def names(self):
        return list(self._d)

    def remove_tool(self, name: str) -> None:
        if name not in self._d:
            raise KeyError(name)
        self._d.pop(name)

    def add_tool(self, tool) -> None:
        self._d[tool.name] = tool


def make_inst(conf: dict):
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = dict(conf)
    inst._revealed = {}
    inst._orig_descriptions = {}
    inst._prompt_log = __import__("collections").deque(maxlen=5)
    return inst


def make_req(names) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        func_tool=FakeToolSet(names),
        system_prompt="你是测试人格。",
    )


ALL = list(plugin_main.TOOL_NAMES)
print(f"本插件注册的工具（{len(ALL)} 个）：{ALL}")

# =====================================================================
#  用例 1：菜单关闭 → ncd_tools 必须被摘掉，其余一个不少
# =====================================================================
print("\n===== 用例 1：菜单关闭时移除 ncd_tools =====")
inst = make_inst({"tool_menu": False})
req = make_req(ALL)
inst._hide_menu_tool(req)
left = [t.name for t in req.func_tool.tools]
print(f"  移除后剩 {len(left)} 个: {left}")
check("1", plugin_main.MENU_TOOL not in left,
      "★菜单关着，ncd_tools 还留在工具集里（DeepSeek 会先点它再调真正的工具）")
check("1", len(left) == len(ALL) - 1, f"应剩 {len(ALL) - 1} 个，实际 {len(left)}")
check("1", all(n in left for n in ALL if n != plugin_main.MENU_TOOL),
      "把别的工具也删掉了")

# =====================================================================
#  用例 2：菜单开启 + 未点开 → 只留 ncd_tools
# =====================================================================
print("\n===== 用例 2：菜单开启、未点开 → 只留 ncd_tools =====")
inst2 = make_inst({"tool_menu": True})
req2 = make_req(ALL)
event2 = types.SimpleNamespace(unified_msg_origin="aiocqhttp:GroupMessage:123456789")
inst2._apply_tool_menu(req2, event2)
left2 = [t.name for t in req2.func_tool.tools]
print(f"  剩余: {left2}")
check("2", left2 == [plugin_main.MENU_TOOL], f"应只剩菜单工具，实际 {left2}")

# =====================================================================
#  用例 3：幂等 + 容错（重复调用、func_tool 为 None 都不能抛）
# =====================================================================
print("\n===== 用例 3：幂等与容错 =====")
inst3 = make_inst({"tool_menu": False})
req3 = make_req(ALL)
inst3._hide_menu_tool(req3)
inst3._hide_menu_tool(req3)
print(f"  连调两次后剩 {len(req3.func_tool.tools)} 个（应为 {len(ALL) - 1}）")
check("3", len(req3.func_tool.tools) == len(ALL) - 1, "重复调用把工具越删越少")

for bad in (types.SimpleNamespace(func_tool=None), types.SimpleNamespace()):
    try:
        inst3._hide_menu_tool(bad)
        print("  func_tool 缺失/为 None → 正常返回 ✓")
    except Exception as exc:  # noqa: BLE001
        check("3", False, f"容错失败：{type(exc).__name__}: {exc}")

# =====================================================================
#  用例 4：源码层面确认这条分支真的接上了
# =====================================================================
print("\n===== 用例 4：_tune_request 里的分支 =====")
src = (PLUGIN / "main.py").read_text(encoding="utf-8")
check("4", "self._hide_menu_tool(req)" in src, "_tune_request 里没调用 _hide_menu_tool")
tail = src[src.find('if self._conf_bool("tool_menu", True):'):][:600]
check("4", "else:" in tail, "菜单开关没有 else 分支")
check("4", tail.find("else:") < tail.find("_hide_menu_tool"),
      "else 分支里没有接上移除逻辑")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 菜单关闭移除 ncd_tools / 菜单开启只留入口 / 幂等容错 / 分支已接上")
