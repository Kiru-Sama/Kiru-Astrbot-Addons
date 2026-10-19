# -*- coding: utf-8 -*-
"""回归护栏：工具清单必须是「自动生成」的，不能手写死。

事故（2026-10-18）：
    加了 view_avatar 之后 ——
      · TOOL_NAMES 里有它       ✅
      · 各模式的过滤都走 TOOL_NAMES，所以都能看到它  ✅
      · 但 ncd_tools 返回的清单是【手写的 6 条】，没有它  ❌
    结果：菜单模式下模型根本不知道有"看头像"这个工具。

现在的做法：清单由 TOOL_NAMES + TOOL_SUMMARY 生成。
本测试就是钉住这件事。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-工具清单.py
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


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


def menu_text() -> str:
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._revealed = {}
    inst._conf = {}
    ev = types.SimpleNamespace(unified_msg_origin="test:GroupMessage:1")
    return asyncio.run(inst.ncd_tools(ev))


text = menu_text()
real_tools = [n for n in plugin_main.TOOL_NAMES if n != plugin_main.MENU_TOOL]

# =====================================================================
print("===== ① 清单覆盖每一个已注册工具 =====")
print(f"  TOOL_NAMES {len(plugin_main.TOOL_NAMES)} 个（含菜单），"
      f"应出现在清单里的 {len(real_tools)} 个")
missing = [n for n in real_tools if n not in text]
print(f"  清单里缺: {missing or '无'}")
check("①", not missing, f"★菜单清单漏了这些工具：{missing}")

# =====================================================================
print("\n===== ② 清单里不许出现未注册的名字 =====")
import re  # noqa: E402

mentioned = set(re.findall(r"·\s*([a-z_]+)\(", text))
print(f"  清单提到的工具: {sorted(mentioned)}")
unknown = mentioned - set(plugin_main.TOOL_NAMES)
check("②", not unknown, f"★清单里有未注册的工具名：{unknown}")
check("②", plugin_main.MENU_TOOL not in mentioned,
      "清单里不该列菜单工具自己（它已经在调了）")

# =====================================================================
print("\n===== ③ TOOL_SUMMARY 必须覆盖全部工具 =====")
summ = getattr(plugin_main, "TOOL_SUMMARY", {})
print(f"  TOOL_SUMMARY {len(summ)} 条")
lack = [n for n in real_tools if n not in summ]
check("③", not lack, f"★TOOL_SUMMARY 缺这些工具的一行说明：{lack}")
extra = set(summ) - set(plugin_main.TOOL_NAMES)
check("③", not extra, f"TOOL_SUMMARY 里有已不存在的工具：{extra}")

# =====================================================================
print("\n===== ④ 清单不是手写死的（源码层面）=====")
src = (PLUGIN / "main.py").read_text(encoding="utf-8")
i = src.find('@filter.llm_tool(name="ncd_tools")')
j = src.find("\n    @filter.llm_tool", i + 10)
body = src[i : j if j != -1 else i + 4000]
check("④", "for name in TOOL_NAMES" in body,
      "★ncd_tools 里没有遍历 TOOL_NAMES，清单可能又是手写的")
check("④", "TOOL_SUMMARY.get(name" in body, "没用 TOOL_SUMMARY 组装清单")

# =====================================================================
print("\n===== ⑤ 各模式都能看到 view_avatar（过滤都走 TOOL_NAMES）=====")
for marker, why in (
    ("if name not in TOOL_NAMES:", "沉浸模式过滤"),
    ("if name not in TOOL_NAMES or name == MENU_TOOL:", "菜单收窄"),
):
    ok = marker in src
    print(f"  {why}: {ok}")
    check("⑤", ok, f"{why} 没有按 TOOL_NAMES 过滤（新工具可能被漏掉）")

check("⑤", "view_avatar" in plugin_main.TOOL_NAMES, "TOOL_NAMES 里没有 view_avatar")
check("⑤", "view_avatar" in text, "菜单清单里没有 view_avatar")
# ★ 2026-10-19：画图工具已改回常驻，源码里不应该再有"按 DRAW_TOOLS 收起来"的代码
check("⑤", "_apply_draw_gate" not in src and "draw_opened" not in src,
      "★ 又把画图工具藏起来了（会重现「未找到指定的工具」）")

# =====================================================================
print("\n===== ⑥ 清单指向画图二级目录（规则不再塞进 ncd_tools）=====")
check("⑥", "draw_tools" in text, "ncd_tools 的清单里没提 draw_tools（模型不会想到去展开）")
for kw in ("masterpiece, best quality, highres", "77 token", "四条硬规则"):
    ok = kw not in text
    print(f"  ncd_tools 里不含「{kw}」: {ok}")
    check("⑥", ok, f"★「{kw}」又回到 ncd_tools 清单里了（应该只在 draw_tools 里）")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 清单自动生成 / 覆盖全部工具 / 无多余名字 / 说明齐全 / 各模式可见")
