# -*- coding: utf-8 -*-
"""回归护栏：每个工具的描述可以被改，而且改回来要真能回到代码原文。

最容易错的地方（本测试重点）：
    `_apply_tool_descriptions` 如果拿"当前的 description"当原文，
    那覆盖过一次之后就永远回不去了 —— 面板上点「清空」也没用。
    所以原文必须【第一次见到时】记下来，之后一直以它为基。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-工具描述.py
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


def make_plugin(conf=None):
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = dict(conf or {})
    inst._revealed = {}
    inst._orig_descriptions = {}
    return inst


ORIG = {
    "web_search_bing": "【原文】用本机浏览器搜网页",
    "web_search_bing": "【原文】搜网页",
    "ncd_tools": "【原文】查出我能用的工具",
    "某个别人的工具": "【别人的】不该被碰",
}


def make_req(names=None):
    names = names or ["web_search_bing", "ncd_tools", "某个别人的工具"]
    tools = [types.SimpleNamespace(name=n, description=ORIG.get(n, "")) for n in names]
    return types.SimpleNamespace(func_tool=types.SimpleNamespace(tools=tools))


def desc_of(req, name):
    for t in req.func_tool.tools:
        if t.name == name:
            return t.description
    return None


# =====================================================================
#  用例 1：配置读取
# =====================================================================
print("===== 用例 1：读取覆盖表 =====")
check("用例1", make_plugin()._tool_desc_overrides() == {}, "没配置时应为空表")
check("用例1",
      make_plugin({"tool_descriptions": {"web_search_bing": "搜"}})._tool_desc_overrides()
      == {"web_search_bing": "搜"},
      "正常配置没读到")
check("用例1",
      make_plugin({"tool_descriptions": "这不是对象"})._tool_desc_overrides() == {},
      "配置写歪时应退化为空表而不是抛")

# =====================================================================
#  用例 2：覆盖生效
# =====================================================================
print("\n===== 用例 2：覆盖生效 =====")
inst = make_plugin({"tool_descriptions": {"web_search_bing": "你自己写的搜索说明"}})
req = make_req()
inst._apply_tool_descriptions(req)
print(f"  web_search_bing → {desc_of(req, 'web_search_bing')!r}")
check("用例2", desc_of(req, "web_search_bing") == "你自己写的搜索说明", "覆盖没生效")
check("用例2", desc_of(req, "ncd_tools") == ORIG["ncd_tools"],
      "没覆盖的工具不该被改")
check("用例2", desc_of(req, "某个别人的工具") == ORIG["某个别人的工具"],
      "★ 把别家插件的工具也改了")
check("用例2", inst._orig_descriptions["web_search_bing"] == ORIG["web_search_bing"],
      "原文没记对")

# =====================================================================
#  用例 3：清空覆盖要能回到代码原文（关键）
# =====================================================================
print("\n===== 用例 3：清空后回到原文 =====")
# 模拟"面板把覆盖清空了" —— 注意此时注册表里那个工具对象的 description
# 已经是覆盖后的文本了（上一轮改过），这正是容易出错的场景。
inst._conf["tool_descriptions"] = {}
req2 = make_req()
# 模拟共享对象仍带着上一轮的覆盖值
for t in req2.func_tool.tools:
    if t.name == "web_search_bing":
        t.description = "你自己写的搜索说明"
inst._apply_tool_descriptions(req2)
print(f"  清空后 web_search_bing → {desc_of(req2, 'web_search_bing')!r}")
check("用例3", desc_of(req2, "web_search_bing") == ORIG["web_search_bing"],
      "★清空覆盖后没回到代码原文（很可能把覆盖值当成了原文）")

# =====================================================================
#  用例 4：空白覆盖 = 用原文（不是把描述清空）
# =====================================================================
print("\n===== 用例 4：空白覆盖按原文处理 =====")
inst4 = make_plugin({"tool_descriptions": {"ncd_tools": "   "}})
req4 = make_req()
inst4._apply_tool_descriptions(req4)
print(f"  ncd_tools → {desc_of(req4, 'ncd_tools')!r}")
check("用例4", desc_of(req4, "ncd_tools") == ORIG["ncd_tools"],
      "★空白覆盖把描述清空了（模型会不知道这工具干什么）")

# =====================================================================
#  用例 5：没有 func_tool / 工具缺 description 也不能抛
# =====================================================================
print("\n===== 用例 5：边界 =====")
inst5 = make_plugin({"tool_descriptions": {"web_search_bing": "x"}})
inst5._apply_tool_descriptions(types.SimpleNamespace(func_tool=None))
check("用例5", True, "func_tool 为 None 时不该抛")
odd = types.SimpleNamespace(
    func_tool=types.SimpleNamespace(tools=[types.SimpleNamespace(name="web_search_bing")])
)
inst5._apply_tool_descriptions(odd)
check("用例5", odd.func_tool.tools[0].description == "x",
      "工具原本没有 description 时要能直接赋上")
print("  边界都过了")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 读取覆盖 / 覆盖生效 / 清空回到原文 / 空白按原文 / 边界 全部正确")

