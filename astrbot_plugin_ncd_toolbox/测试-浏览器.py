"""自测：真跑一次 Bing 搜索 + 抓正文 + 标签页复用。

默认无头模式，不弹窗口。需要本机装有 Edge。
用法：<AstrBot>\\.venv\\Scripts\\python.exe 测试-浏览器.py
"""

import asyncio
import pathlib
import sys
import tempfile

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

from browser import Browser, find_edge  # noqa: E402


async def run_once() -> list[str]:
    """跑一遍全部检查，返回失败项（空 = 通过）。"""
    edge = find_edge()
    print(f"Edge 路径 = {edge or '(未找到)'}")
    if not edge:
        print("FAIL: 找不到 Edge，请检查安装或在插件配置里填 edge_path")
        return ["找不到 Edge"]

    browser = Browser(
        edge_path=edge,
        debug_port=9333,
        profile_dir=tempfile.mkdtemp(prefix="toolbox_test_"),
        headless=True,
        page_timeout=25,
    )
    failures: list[str] = []
    try:
        items = await browser.search("RTX 5070 Ti 显存", 5)
        print(f"\n搜索返回 {len(items)} 条")
        for it in items:
            print(f"  - {it['title'][:50]}")
            print(f"    {it['url'][:90]}")
            print(f"    {(it['snippet'] or '(无摘要)')[:70]}")

        if len(items) < 3:
            failures.append(f"搜索只返回 {len(items)} 条，期望 >= 3")
        for idx, it in enumerate(items):
            if not it.get("title") or not it.get("url"):
                failures.append(f"第 {idx} 条缺 title 或 url")
        if items and not any(it.get("snippet") for it in items):
            failures.append("所有结果都没有摘要")

        page = await browser.fetch_text("https://www.nvidia.cn/geforce/graphics-cards/", 1500)
        print(f"\n抓正文 {page['url']}")
        print(f"  标题 = {page['title'][:60]!r}  总字数 = {page['total_chars']}")
        if page["total_chars"] < 200:
            failures.append(f"正文只有 {page['total_chars']} 字，像是没抓到")
        if not page["title"]:
            failures.append("正文页没有标题")

        # 回归 1：有资源永远加载不完的站点。
        # 旧判据 readyState === 'complete' 在这里必然超时（实测 notebookcheck-cn 超时 25s）。
        slow = await browser.fetch_text(
            "https://www.notebookcheck-cn.com/NVIDIA-GeForce-RTX-5070-Ti-Laptop.1072562.0.html",
            2000,
        )
        print(f"\n慢站 {slow['url']}")
        print(f"  状态={slow['status']} 标题={slow['title'][:50]!r} 字数={slow['total_chars']}")
        print(f"  开头={slow['text'][:90]!r}")
        if slow["status"] and slow["status"] != 200:
            failures.append(f"慢站返回 HTTP {slow['status']}")
        if slow["total_chars"] < 150:
            failures.append(f"慢站只抓到 {slow['total_chars']} 字，期望 >= 150")

        # 回归 2：错误页必须带 HTTP 状态，不能把 403 页面当正文交出去。
        if "status" not in page:
            failures.append("抓正文结果里没有 status 字段，错误页会被当成正文")
        if "status" not in slow:
            failures.append("慢站结果里没有 status 字段")

        # 断言的是「我们复用了同一个标签页」，而不是「标签页总数不变」——
        # Edge 自己会弹 sync 确认框之类的页面，总数不受我们控制。
        first_tab = browser._tab_id
        print(f"\n第一次搜索后我们的标签页 id = {first_tab}")

        await browser.search("AstrBot 插件", 3)
        second_tab = browser._tab_id
        print(f"第二次搜索后我们的标签页 id = {second_tab}")
        if not first_tab:
            failures.append("没有记录到复用的标签页 id")
        elif second_tab != first_tab:
            failures.append(f"标签页换人了：{first_tab} -> {second_tab}，没有复用")

        targets = await browser._http("GET", "/json/list")
        ids = {t.get("id") for t in targets if t.get("type") == "page"}
        if first_tab and first_tab not in ids:
            failures.append("我们那个标签页已经不存在了")
    finally:
        browser.shutdown()

    return failures


async def attempt() -> list[str]:
    """跑一次，并把【抛异常】也归一成失败项。

    ★ 这是实测补上的：慢站就绪超时是 `raise TimeoutError`，而不是把失败
      append 进列表 —— 于是异常一路冒穿 run_once()，main() 直接带非零码退出，
      **下面那套"两次都失败才算真失败"的重试根本没机会跑**。
      计划里的护栏，实际没生效；护栏本身也要被验。
    """
    try:
        return await run_once()
    except Exception as exc:  # noqa: BLE001
        return [f"run_once 抛异常：{type(exc).__name__}: {exc}"]


async def main() -> int:
    """★ 这个测试要连外网（Bing / nvidia / notebookcheck）。

    目标站点抖一下就会失败，而"会随机报警的测试"最后没人看 ——
    所以失败时【重试一次】，并且明确告诉你"首次失败、重试通过"，
    而不是悄悄吞掉。两次都失败才算真失败。
    """
    failures = await attempt()

    if not failures:
        print("\nPASS: 搜索与抓正文均正常，标签页复用正常")
        return 0

    print("\n失败项:")
    for item in failures:
        print(f"  ✗ {item}")

    print("\n⚠️ 首次失败，重试一次（这个测试依赖外网，目标站点抖动会误报）…\n")
    retry = await attempt()
    if not retry:
        print(
            "\nPASS（重试通过）: 首次失败很可能是外网抖动或站点临时抽风，"
            "\n     但如果它反复只靠重试才过，就该查了 —— 别让报警器一直响。"
        )
        return 0

    print("\n重试仍然失败:")
    for item in retry:
        print(f"  ✗ {item}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
