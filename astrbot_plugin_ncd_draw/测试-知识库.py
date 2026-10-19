# -*- coding: utf-8 -*-
"""回归护栏：标签库（kb/core 默认，kb/full 按需）。

用户 2026-10-20 的规则：
  · 「让 ai 只加载核心，全量非必要不用」
  · 核心 = 动作/表情/身体/服饰/物件/场景/风格/常用（9 文件，3.4 MB）
  · 全量 = 再加角色/作品/画师/长尾（16 文件，16 MB）—— 只有模型显式要 full 才碰

钉住：
  1. 核心库首次检索能建索引，行数合理、耗时可接受
  2. 全量库懒加载：没要 full 之前不建
  3. 核心库查不到时，提示里要给「可加 scope=full」
  4. scope=full 能查到核心库里没有的东西（作品/角色）
  5. allow_full=False 时，scope=full 被降级回核心

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-知识库.py
"""

import asyncio
import sys
import time
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import kb_index  # noqa: E402
import main as plugin_main  # noqa: E402

plugin_main.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    debug=lambda *a, **k: None,
)

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


# =====================================================================
print("===== ① 核心库：懒加载 + 行数 =====")
check("①", not kb_index._INDEXES, "模块导入时就建了索引（应该是懒加载）")
t0 = time.time()
core = kb_index.index("core")
dt = time.time() - t0
print(f"  核心库 {len(core.files)} 文件 / {len(core.lines):,} 行 / {dt:.2f}s")
check("①", len(core.files) == 9, f"核心库文件数不对：{len(core.files)}")
check("①", len(core.lines) > 30000, f"核心库行数太少：{len(core.lines)}")
check("①", dt < 5, f"核心库建索引太慢：{dt:.2f}s")
check("①", "full" not in kb_index._INDEXES, "★ 没要 full 却把全量库也建了")

# =====================================================================
print("\n===== ② 核心库检索：英文/中文都能中 =====")
out = kb_index.render("long_hair", limit=3)
print("  " + out.splitlines()[0])
check("②", "long_hair" in out and "核心库" in out, f"英文标签没命中：{out[:80]}")
check("②", "〔core/" in out, "结果里没标出处")

out_cn = kb_index.render("头饰", limit=3)
print("  " + out_cn.splitlines()[0])
check("②", "hair_ornament" in out_cn or "头饰" in out_cn, "中文查询没命中")

# =====================================================================
print("\n===== ③ 核心库查不到 → 提示可加 scope=full =====")
out_miss = kb_index.render("zzzzqqqq", limit=3)
print("  " + out_miss.replace("\n", " / ")[:100])
check("③", "没有匹配" in out_miss, "查不到时的提示不对")
check("③", 'scope="full"' in out_miss, "★ 没提示可以查全量库")

# =====================================================================
print("\n===== ④ 全量库：只在要 full 时才建，能查到作品 =====")
check("④", "full" not in kb_index._INDEXES, "★ 前面几次检索就把全量库建了")
t0 = time.time()
out_full = kb_index.render("laffey_(azur_lane)", scope="full", limit=3)
dt = time.time() - t0
print(f"  全量库检索 {dt:.2f}s → " + out_full.splitlines()[0])
check("④", sum(1 for ln in core.lines if "laffey_(azur_lane)" in ln.text) == 0,
      "测试前提不成立：核心库里居然有 laffey（那测不出「按需全量」）")
check("④", "full" in kb_index._INDEXES, "要了 full 却没建全量索引")
check("④", "全量库" in out_full, "结果没标全量库")
check("④", "〔full/" in out_full, "全量结果没标出处")
check("④", dt < 20, f"全量库首次检索太慢：{dt:.2f}s")

# =====================================================================
print("\n===== ⑤ allow_full=False 时 full 被降级回核心 =====")
out_deny = kb_index.render("laffey_(azur_lane)", scope="full", limit=3, allow_full=False)
print("  " + out_deny.splitlines()[0])
check("⑤", "核心库" in out_deny, "★ 禁了全量还是查了 full")

# =====================================================================
print("\n===== ⑥ 工具接线：默认核心 / 显式 full / 闸门 =====")


class Ev:
    unified_msg_origin = "t:GroupMessage:1"

    def get_sender_id(self):
        return "1"


def make_plugin(**conf):
    inst = plugin_main.NcdDrawPlugin.__new__(plugin_main.NcdDrawPlugin)
    inst._conf = dict(conf)
    inst._draw_revealed = {"t:GroupMessage:1": 1.0}
    inst.context = types.SimpleNamespace()
    return inst


inst = make_plugin()
out1 = asyncio.run(inst.draw_kb_search(Ev(), "long_hair"))
print("  默认 → " + out1.splitlines()[0])
check("⑥", "核心库" in out1, "默认没查核心库")

out2 = asyncio.run(inst.draw_kb_search(Ev(), "laffey_(azur_lane)", "full"))
check("⑥", "全量库" in out2, "显式 full 没生效")

inst2 = make_plugin(kb_allow_full=False)
out3 = asyncio.run(inst2.draw_kb_search(Ev(), "laffey_(azur_lane)", "full"))
check("⑥", "核心库" in out3, "配置禁了全量，工具却还查")

inst3 = make_plugin(kb_enabled=False)
out4 = asyncio.run(inst3.draw_kb_search(Ev(), "long_hair"))
check("⑥", "已关闭" in out4, "总开关没生效")

inst4 = make_plugin()
inst4._draw_revealed = {}
out5 = asyncio.run(inst4.draw_kb_search(Ev(), "long_hair"))
check("⑥", out5.startswith("画图失败") and "drawing_tools" in out5, "闸门没生效")

# 空 query = 核心库文件清单
inst5 = make_plugin()
out6 = asyncio.run(inst5.draw_kb_search(Ev()))
print("  清单 → " + out6.splitlines()[0])
check("⑥", "核心库：" in out6 and "文件" in out6, "空 query 没给文件清单")
check("⑥", 'scope="full"' in out6, "清单里没提全量库")

# =====================================================================
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 懒加载 / 核心命中 / 缺省提示 / 全量按需 / 禁用降级 / 工具接线")
