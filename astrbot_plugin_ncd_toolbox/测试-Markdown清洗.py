# -*- coding: utf-8 -*-
"""回归护栏：出站回复里的 Markdown 必须被洗成纯文本。

背景（2026-10-18 实测）：机器人在 QQ 群里贴了一整篇 Markdown ——
`##` 标题、`|---|` 表格、`**加粗**` 原样显示，手机上一堆乱符号。
QQ 不渲染 Markdown，而 4B 模型（尤其在做网页总结时）极爱输出表格。

两道防线，本测试覆盖第二道（确定性那道）：
  ① 提示词里说"用纯文本"（治不了根，模型会忘）
  ② 发送前把 Plain 文本洗一遍（本测试）

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-Markdown清洗.py
"""

import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402
from astrbot.api import message_components as Comp  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


# =====================================================================
#  用例 1：标题 / 分隔线 / 加粗 / 斜体 / 删除线 / 行内代码
# =====================================================================
print("===== 用例 1：行内标记 =====")
cases = {
    "## 标题": "标题",
    "### 一句话总结": "一句话总结",
    "这是 **加粗** 和 *斜体* 文字": "这是 加粗 和 斜体 文字",
    "~~删掉~~ 保留": "删掉 保留",
    "运行 `pip install x` 就行": "运行 pip install x 就行",
    "---": "",
    "***": "",
}
for src, want in cases.items():
    got = plugin_main.strip_markdown(src)
    print(f"  {src!r:34} → {got!r}")
    check("用例1", got == want, f"期望 {want!r}，实际 {got!r}")

# =====================================================================
#  用例 2：表格（这次踩的就是它）
# =====================================================================
print("\n===== 用例 2：Markdown 表格 =====")
table = """| 维度 | **LM Studio** | **Ollama** |
|---|---|---|
| 定位 | GUI 图形界面 | CLI+API |
| 安装 | 一键启动 | brew install ollama |"""
out = plugin_main.strip_markdown(table)
print("  洗后：")
for line in out.split("\n"):
    print("    " + line)
check("用例2", "|" not in out, "★还有竖线残留（表格没转成纯文本）")
check("用例2", "---" not in out, "分隔行没删掉")
check("用例2", "**" not in out, "加粗标记没去掉")
check("用例2", out.count("\n") == 2, f"应剩 3 行（表头+2 行数据），实际 {out.count(chr(10)) + 1} 行")
check("用例2", "定位" in out and "GUI" in out, "表格内容丢了")

# =====================================================================
#  用例 3：链接 / 列表 / 引用 / 代码块
# =====================================================================
print("\n===== 用例 3：链接 / 列表 / 代码块 =====")
check("用例3",
      plugin_main.strip_markdown("见 [文档](https://a.com/x) 说明") == "见 文档（https://a.com/x） 说明",
      "链接没转成「文字（网址）」")
bullets = "- 第一条\n* 第二条\n+ 第三条"
got_b = plugin_main.strip_markdown(bullets)
print(f"  列表 → {got_b!r}")
check("用例3", got_b == "· 第一条\n· 第二条\n· 第三条", f"列表符号不对：{got_b!r}")

block = "```python\nprint(1)\n```"
check("用例3", plugin_main.strip_markdown(block) == "print(1)", "代码块围栏没去掉")

check("用例3", plugin_main.strip_markdown("> 引用一句") == "引用一句", "引用符号没去掉")

# =====================================================================
#  用例 4：不该动的别动
# =====================================================================
print("\n===== 用例 4：普通文本不受影响 =====")
plain = "今天显卡价格涨了 5%，RTX 5070 Ti 现在 8999 元。\n就这些。"
check("用例4", plugin_main.strip_markdown(plain) == plain, "★把正常文本改坏了")

code_like = "file_name 和 func_x 这种下划线不该被动"
check("用例4", plugin_main.strip_markdown(code_like) == code_like,
      f"★下划线被当成斜体了：{plugin_main.strip_markdown(code_like)!r}")

check("用例4", plugin_main.strip_markdown("") == "", "空值应原样返回")

# =====================================================================
#  用例 5：钩子真的会改出站消息
# =====================================================================
print("\n===== 用例 5：on_decorating_result 钩子 =====")
inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
inst._conf = {}
inst._revealed = {}
inst._orig_descriptions = {}

plain_comp = Comp.Plain(text="## 标题\n**加粗**内容")
result = types.SimpleNamespace(chain=[plain_comp])
event = types.SimpleNamespace(get_result=lambda: result)
import asyncio

asyncio.run(inst._plain_text_reply(event))
print(f"  出站文本 → {plain_comp.text!r}")
check("用例5", plain_comp.text == "标题\n加粗内容",
      f"钩子没改干净：{plain_comp.text!r}")

# result 为 None / chain 为空时不能抛
asyncio.run(inst._plain_text_reply(types.SimpleNamespace(get_result=lambda: None)))
asyncio.run(inst._plain_text_reply(
    types.SimpleNamespace(get_result=lambda: types.SimpleNamespace(chain=[]))
))
print("  result 为 None / chain 为空时不抛 ✓")

print("\n===== 用例 6：源头提示词也要压住（总结 + 搜索轮次）=====")
src = (PLUGIN / "main.py").read_text(encoding="utf-8")
for token, why in (
    ("不要写成对比表/矩阵", "总结提示词没有禁止「对比表」"),
    ("不要 Markdown", "总结提示词没有禁止 Markdown"),
    ("每条只讲一件事", "总结提示词没有要求短句要点"),
    ("再搜一次", "搜索结果没有引导「不对题就再搜」"),
):
    ok = token in src
    print(f"  {'OK ' if ok else '✗  '}{token}")
    check("用例6", ok, why)

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 行内标记 / 表格 / 链接列表代码块 / 不误伤正常文本 / 钩子 全部正确")
