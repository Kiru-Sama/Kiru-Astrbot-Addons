# -*- coding: utf-8 -*-
"""回归护栏：搜索增强（清洗/去重/限站/Bing 参数）+ 出图串行。

背景：
  · 模型常把「帮我搜一下 xxx」原样当 query；Bing 第一页常被同一个大站刷满；
    问"最新"却搜到几年前的文章 —— 三件都不需要新依赖就能修。
  · 出图那边：聊天和生图页可能同时点，两条管线并发会把显存峰值叠起来
    （2026-10-17 实测过一次进程被直接干掉）。所以要串行。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-搜索增强.py
"""

import asyncio
import collections
import sys
import pathlib
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import browser as browser_mod  # noqa: E402
import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


# =====================================================================
#  用例 1：查询清洗
# =====================================================================
print("===== 用例 1：查询清洗 =====")
cases = {
    "帮我搜一下 今天显卡价格 谢谢": "今天显卡价格",
    "查查AstrBot插件": "AstrBot插件",
    "我想知道 RTX 5070 Ti 的显存": "RTX 5070 Ti 的显存",
    "请问一下怎么装 AstrBot": "怎么装 AstrBot",
    # ★ 不能误伤：这两个开头/结尾长得像客套词，剥了意思就变了
    "请求超时怎么改": "请求超时怎么改",
    "看看是不是内存不够": "是不是内存不够",   # "看看" 是客套，可以剥
    "阿": "阿",                               # 太短，不该被剥空
}
for src, want in cases.items():
    got = plugin_main.clean_query(src)
    print(f"  {src!r:34} → {got!r}")
    check("用例1", got == want, f"期望 {want!r}，实际 {got!r}")

# =====================================================================
#  用例 2：网址规范化与去重
# =====================================================================
print("\n===== 用例 2：网址规范化 / 去重 =====")
u = plugin_main._norm_result_url("https://zhihu.com/q/1?utm_source=x&utm_medium=y")
print(f"  去跟踪参数: {u}")
check("用例2", "utm_" not in u, "跟踪参数没去掉")
check("用例2", plugin_main._norm_result_url("https://a.com/p/") == "https://a.com/p",
      "末尾斜杠没去掉")

items = [
    {"title": "z1", "url": "https://zhihu.com/q/1?utm_source=x"},
    {"title": "z1dup", "url": "https://zhihu.com/q/1/"},          # 与上面同一条
    {"title": "z2", "url": "https://zhihu.com/q/2"},
    {"title": "z3", "url": "https://zhihu.com/q/3"},              # 同站第 3 条，应被限
    {"title": "c1", "url": "https://csdn.net/p/9"},
    {"title": "empty", "url": ""},
]
out = plugin_main.dedupe_results(items)
names = [x["title"] for x in out]
print(f"  去重后: {names}")
check("用例2", names == ["z1", "z2", "c1"],
      f"应剩 z1/z2/c1（同站上限 2 + 去重 + 丢空），实际 {names}")

# =====================================================================
#  用例 3：Bing 参数（时间过滤 / 翻页 / 中文）
# =====================================================================
print("\n===== 用例 3：Bing 参数拼接 =====")
captured = {}


def make_browser():
    b = browser_mod.Browser()          # 不启浏览器，只构造
    async def fake_open(url, ready, extract):
        captured["url"] = url
        return '{"items": [{"title":"t1","url":"https://x.com/1"}], "title": "ok"}'
    b._open_and_extract = fake_open
    return b


b = make_browser()
asyncio.run(b.search("测试", 5, time_filter="day"))
print(f"  day: {captured['url']}")
check("用例3", "qft=+filterui:age-lt1440" in captured["url"], "没带一天的时间过滤")
check("用例3", "setlang=zh-CN" in captured["url"], "没带中文优先参数")
check("用例3", "first=" not in captured["url"], "第 1 页不该带 first")

asyncio.run(b.search("测试", 5, time_filter="week", page=2))
print(f"  week+page2: {captured['url']}")
check("用例3", "qft=+filterui:age-lt10080" in captured["url"], "一周的分钟数不对")
check("用例3", "first=11" in captured["url"], "第 2 页应带 first=11")

asyncio.run(b.search("测试", 5))
print(f"  无过滤: {captured['url']}")
check("用例3", "qft=" not in captured["url"], "没配时间过滤却带了 qft")

# =====================================================================
#  用例 4：出图串行（同一时刻只有一条管线）
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 查询清洗 / 网址去重与限站 / Bing 参数 / 出图串行 全部正确")
