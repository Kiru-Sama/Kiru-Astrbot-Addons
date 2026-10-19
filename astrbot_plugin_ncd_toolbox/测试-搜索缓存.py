# -*- coding: utf-8 -*-
"""回归护栏：搜索/抓取/总结的缓存要真的省事，且不能串味。

三件必须成立的事：
    ① 键的规范化：搜索词大小写/多空格、网址末尾斜杠，不能算成不同条目
    ② 命中缓存时【不能再开浏览器】—— 否则缓存只是白记一遍
    ③ 总结的键必须含"用户这一问"—— 同一页换个问题问，不能拿到上一问的要点
       （这是最隐蔽的错：结果看着有内容，但答的是上一个问题）

另外两条边界：
    ④ TTL=0 必须完全关闭（不读也不写），方便临时强制重搜
    ⑤ 条数上限生效，不会无限涨

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-搜索缓存.py
"""

import asyncio
import pathlib
import sys
import time
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
    return inst


class FakeBrowser:
    def __init__(self):
        self.search_calls = []
        self.fetch_calls = []

    async def search(self, query, count, **kwargs):  # 新版 search 带 time_filter/page
        self.search_calls.append((query, count))
        return [
            {"title": f"结果{i}", "url": f"https://example.com/{i}", "snippet": "s"}
            for i in range(1, count + 1)
        ]

    async def fetch_text(self, url, max_chars):
        self.fetch_calls.append((url, max_chars))
        return {
            "url": url,
            "title": "标题",
            "text": "正文" * 20,
            "total_chars": 40,
            "status": 200,
        }


def bind_browser(inst, browser):
    async def fake_get_browser():
        return browser

    inst._get_browser = fake_get_browser


def clear_cache():
    plugin_main._CACHE.clear()


# =====================================================================
#  用例 1：键的规范化
# =====================================================================
print("===== 用例 1：键规范化 =====")
check("用例1", plugin_main._norm_query("  AstrBot   插件 ") == "astrbot 插件",
      f"查询词没规范好：{plugin_main._norm_query('  AstrBot   插件 ')!r}")
check("用例1", plugin_main._norm_query("A\tB\nC") == "a b c", "制表符/换行没折叠")
check("用例1", plugin_main._norm_url("https://a.com/x/") == "https://a.com/x",
      "末尾斜杠没去掉")
# 根路径的斜杠也去掉：https://a.com/ 和 https://a.com 是同一个页面，合并才对
check("用例1", plugin_main._norm_url("https://a.com/") == "https://a.com",
      f"根路径斜杠处理不对：{plugin_main._norm_url('https://a.com/')!r}")
check("用例1", plugin_main._norm_url(" https://a.com ") == "https://a.com", "首尾空白没去")

# =====================================================================
#  用例 2：命中缓存就不再开浏览器（搜索）
# =====================================================================
print("\n===== 用例 2：搜索命中缓存 =====")
clear_cache()
inst = make_plugin({"search_cache_ttl": 600, "search_with_snippet": False})
browser = FakeBrowser()
bind_browser(inst, browser)
ev = types.SimpleNamespace()

r1 = asyncio.run(inst.web_search_bing(ev, "AstrBot 插件", 3))
r2 = asyncio.run(inst.web_search_bing(ev, "  astrbot   插件  ", 3))
print(f"  浏览器实际搜索 {len(browser.search_calls)} 次（应为 1）")
check("用例2", len(browser.search_calls) == 1,
      f"★大小写/空格不同的同一个词被当成两次搜索（实际 {len(browser.search_calls)} 次）")
check("用例2", r1 == r2, "两次返回内容不一致（缓存没命中或序列化不同）")
check("用例2", '"results"' in r1, "返回值不是预期 JSON")

# 换个条数 → 必须是另一条缓存
asyncio.run(inst.web_search_bing(ev, "AstrBot 插件", 5))
print(f"  条数改成 5 后累计 {len(browser.search_calls)} 次（应为 2）")
check("用例2", len(browser.search_calls) == 2, "★不同条数居然命中了同一缓存")

# =====================================================================
#  用例 3：抓正文命中缓存
# =====================================================================
print("\n===== 用例 3：抓正文命中缓存 =====")
clear_cache()
# ★ 关掉内网检查：example.com 在本机可能解析不到，会先被守卫挡在抓取之前，
#   那样测的就不是缓存了。守卫本身在用例 4 单独测。
inst3 = make_plugin({"search_cache_ttl": 600, "block_private_urls": False,
                     "summarize_page": False, "fetch_max_chars": 4000})
browser3 = FakeBrowser()
bind_browser(inst3, browser3)
u = "https://example.com/page"
asyncio.run(inst3.fetch_page(ev, u))
asyncio.run(inst3.fetch_page(ev, "https://example.com/page/"))  # 末尾斜杠
print(f"  浏览器实际抓取 {len(browser3.fetch_calls)} 次（应为 1）")
check("用例3", len(browser3.fetch_calls) == 1,
      f"★同一网址（差一个斜杠）被当成两次（实际 {len(browser3.fetch_calls)} 次）")

# =====================================================================
#  用例 4：内网地址被拒的不能进缓存
# =====================================================================
print("\n===== 用例 4：被拒的地址不进缓存 =====")
clear_cache()
inst4 = make_plugin({"search_cache_ttl": 600, "block_private_urls": True,
                     "summarize_page": False})
browser4 = FakeBrowser()
bind_browser(inst4, browser4)
r4a = asyncio.run(inst4.fetch_page(ev, "http://127.0.0.1:6185/"))
r4b = asyncio.run(inst4.fetch_page(ev, "http://127.0.0.1:6185/"))
print(f"  两次都拦下：{'error' in r4a and 'error' in r4b}，浏览器抓取 {len(browser4.fetch_calls)} 次")
check("用例4", "error" in r4a and "error" in r4b, "内网地址没被拒")
check("用例4", len(browser4.fetch_calls) == 0, "内网地址居然去抓了")

# =====================================================================
#  用例 5：总结的键必须含"用户这一问"
# =====================================================================
print("\n===== 用例 5：总结键含问题 =====")


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def text_chat(self, contexts=None, **kwargs):
        prompt = contexts[0].content if contexts else ""
        self.calls.append(prompt)
        return types.SimpleNamespace(completion_text="要点")


class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    async def get_using_provider_async(self, umo):
        return self.provider


provider = FakeProvider()
clear_cache()
inst5 = make_plugin({"search_cache_ttl": 600, "summary_max_chars": 400})
inst5.context = FakeContext(provider)

TEXT = "这是一段很长的网页正文。" * 50


def event_with(q):
    return types.SimpleNamespace(
        unified_msg_origin="x:FriendMessage:1",
        get_message_str=lambda: q,
    )


s1 = asyncio.run(inst5._summarize_page(event_with("这页讲了什么"), TEXT))
s1b = asyncio.run(inst5._summarize_page(event_with("这页讲了什么"), TEXT))
print(f"  同一问题两次 → 模型调用 {len(provider.calls)} 次（应为 1）")
check("用例5", len(provider.calls) == 1, f"同一问题没命中缓存（调了 {len(provider.calls)} 次）")
check("用例5", s1 == s1b, "两次总结内容不一致")

s2 = asyncio.run(inst5._summarize_page(event_with("这页提到哪些型号"), TEXT))
print(f"  换一个问题后 → 模型调用 {len(provider.calls)} 次（应为 2）")
check("用例5", len(provider.calls) == 2,
      "★换了问题却命中了上一问的总结缓存 —— 会答非所问")
check("用例5", "这页提到哪些型号" in provider.calls[-1], "新问题的提示词里没有新问题")

# =====================================================================
#  用例 6：TTL=0 完全关闭
# =====================================================================
print("\n===== 用例 6：TTL=0 关闭缓存 =====")
clear_cache()
inst6 = make_plugin({"search_cache_ttl": 0, "search_with_snippet": False})
browser6 = FakeBrowser()
bind_browser(inst6, browser6)
asyncio.run(inst6.web_search_bing(ev, "同一个词", 3))
asyncio.run(inst6.web_search_bing(ev, "同一个词", 3))
print(f"  TTL=0 时浏览器搜索 {len(browser6.search_calls)} 次（应为 2）")
check("用例6", len(browser6.search_calls) == 2, "★TTL=0 时仍在用缓存")
check("用例6", len(plugin_main._CACHE) == 0, "TTL=0 时不该写入缓存")

# =====================================================================
#  用例 7：过期与容量上限
# =====================================================================
print("\n===== 用例 7：过期与上限 =====")
clear_cache()
# ★ 别用小数 TTL：_conf_int 会把它截成 0（等于关了缓存），测出来是假绿。
#   直接改条目时间戳来模拟"很久以前写的"。
inst7 = make_plugin({"search_cache_ttl": 600})
inst7._cache_put("k", "v")
check("用例7", inst7._cache_get("k") == "v", "刚放进去就取不到")
plugin_main._CACHE["k"] = (time.time() - 10_000, "v")
check("用例7", inst7._cache_get("k") is None, "★过期了还能取到")
check("用例7", "k" not in plugin_main._CACHE, "过期的条目应该被删掉，不能留着占位")

clear_cache()
inst7b = make_plugin({"search_cache_ttl": 600})
for i in range(plugin_main.CACHE_MAX_ENTRIES + 5):
    inst7b._cache_put(f"k{i}", "v")
print(f"  放了 {plugin_main.CACHE_MAX_ENTRIES + 5} 条，实际剩 {len(plugin_main._CACHE)} 条")
check("用例7", len(plugin_main._CACHE) == plugin_main.CACHE_MAX_ENTRIES,
      f"上限没生效（实际 {len(plugin_main._CACHE)}）")
check("用例7", inst7b._cache_get("k0") is None, "最旧的没被淘汰")

clear_cache()

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 键规范化 / 搜索命中 / 抓取命中 / 拒址不入缓存 / 总结键含问题 / TTL=0 / 过期与上限 全部正确")
