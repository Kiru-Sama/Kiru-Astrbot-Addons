# -*- coding: utf-8 -*-
"""回归护栏：搜索结果瘦身 + 抓正文先总结再给模型。

背景（实测数字）：
  · web_search_bing 带摘要时 8 条 = 617 token；只回序号+标题+网址 ≈ 250
  · fetch_page 不总结时一篇正文 = 2604 token，占 8192 上下文的 32%
    而且这些字会【一直留在对话历史里】（压缩还压不掉，见 README）
  · 总结后约 300 token

这里守四件事：
  ① 默认搜索结果里【没有】摘要字段，且体量明显变小
  ② 打开 search_with_snippet 时摘要回来
  ③ 长正文会被总结（返回 summary、不返回全文）
  ④ 短正文不总结、总结失败、没有可用模型 —— 三种情况都必须【退回原文】，不能空手而归

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-搜索瘦身.py
"""
import asyncio
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")


class FakeEvent:
    unified_msg_origin = "test:FriendMessage:1"

    def __init__(self, text="RTX 5070 Ti 显存多大"):
        self._text = text

    def get_message_str(self):
        return self._text


class FakeBrowser:
    def __init__(self, items, page):
        self.items = items
        self.page = page
        self.fetch_calls = []

    async def search(self, query, count, **kwargs):  # 新版 search 带 time_filter/page
        return self.items[:count]

    async def fetch_text(self, url, max_chars):
        self.fetch_calls.append((url, max_chars))
        return dict(self.page)


class FakeProvider:
    def __init__(self, summary="要点：显存 12GB。", raises=False):
        self.summary = summary
        self.raises = raises
        self.calls = []

    async def text_chat(self, contexts=None, **kwargs):
        self.calls.append(contexts)

        class R:
            completion_text = self.summary

        if self.raises:
            raise RuntimeError("模拟总结失败")
        return R()


class StubContext:
    def __init__(self, provider):
        self.routes = []
        self._provider = provider

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))

    async def get_using_provider_async(self, umo=None):
        return self._provider


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_slim_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_slim_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


def est_tokens(text):
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + max(1, (len(text) - cjk) // 4)


ITEMS = [
    {
        "title": f"结果标题第{i}条关于显存的说明",
        # ★ 用公网字面 IP（example.com 的真实地址）而不是域名：本测试关注的是
        #   「搜索瘦身 / 抓正文后总结」，不该依赖 DNS。而 block_private_urls 会真去解析
        #   域名 —— 这台机器上 example.com 恰好被解析成 127.0.0.1（DNS 屏蔽手法），
        #   于是会被守卫拦下，测的就不是本意了。字面 IP 不需要解析，测试保持自洽。
        "url": f"https://93.184.216.34/page-{i}",
        "snippet": "这是一段足够长的摘要文字，用来体现带摘要时会多占多少上下文空间，故意写长一点。" * 2,
    }
    for i in range(1, 9)
]
LONG_PAGE = {
    "title": "显卡规格页",
    "url": "https://93.184.216.34/spec",
    "status": 200,
    "text": "正文内容。" * 600,   # 3000 字
    "truncated": False,
    "total_chars": 3000,
}
SHORT_PAGE = dict(LONG_PAGE, text="就一句话。", total_chars=5)


async def main() -> int:
    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    bad = []

    # ══════════ 用例 1：默认搜索结果不带摘要 ══════════
    print("===== 用例 1：默认搜索结果只有标题+网址 =====")
    prov = FakeProvider()
    inst = cls(StubContext(prov), {})
    inst._browser = FakeBrowser(ITEMS, LONG_PAGE)
    out = json.loads(await inst.web_search_bing(FakeEvent(), query="显存", count=8))
    rows = out["results"]
    has_snippet = any("snippet" in r for r in rows)
    compact = json.dumps(out, ensure_ascii=False)
    full = json.dumps(
        {"query": "显存", "results": ITEMS}, ensure_ascii=False
    )
    print(f"  条数={len(rows)}  含摘要字段={has_snippet}")
    print(f"  瘦身后 {len(compact)} 字符 ≈ {est_tokens(compact)} token")
    print(f"  带摘要 {len(full)} 字符 ≈ {est_tokens(full)} token")
    print(f"  省 {100 - len(compact) / len(full) * 100:.0f}%")
    if has_snippet:
        bad.append("用例1：默认不该带 snippet")
    if set(rows[0]) != {"i", "title", "url"}:
        bad.append(f"用例1：字段不对 {sorted(rows[0])}")
    if est_tokens(compact) >= est_tokens(full):
        bad.append("用例1：瘦身后没有变小")

    # ══════════ 用例 2：打开开关时带摘要 ══════════
    print("\n===== 用例 2：search_with_snippet=true 时带摘要 =====")
    inst2 = cls(StubContext(FakeProvider()), {"search_with_snippet": True})
    inst2._browser = FakeBrowser(ITEMS, LONG_PAGE)
    out2 = json.loads(await inst2.web_search_bing(FakeEvent(), query="显存", count=8))
    print(f"  含摘要字段 = {all('snippet' in r for r in out2['results'])}")
    if not all("snippet" in r for r in out2["results"]):
        bad.append("用例2：开了开关却没带摘要")

    # ══════════ 用例 3：长正文会被总结 ══════════
    print("\n===== 用例 3：长正文 → 总结后才给模型 =====")
    prov3 = FakeProvider(summary="要点：RTX 5070 Ti 笔记本显存 12GB，GDDR7。")
    inst3 = cls(StubContext(prov3), {})
    inst3._browser = FakeBrowser(ITEMS, LONG_PAGE)
    out3 = json.loads(await inst3.fetch_page(FakeEvent(), url="https://93.184.216.34/spec"))
    payload3 = json.dumps(out3, ensure_ascii=False)
    print(f"  字段 = {sorted(out3)}")
    print(f"  summarized = {out3.get('summarized')}  原文 {out3.get('source_chars')} 字")
    print(f"  返回体 {len(payload3)} 字符 ≈ {est_tokens(payload3)} token"
          f"（原文是 {est_tokens(LONG_PAGE['text'])} token）")
    print(f"  总结调用次数 = {len(prov3.calls)}")
    if not out3.get("summarized"):
        bad.append("用例3：长正文没有被总结")
    if "text" in out3:
        bad.append("用例3：总结后不该再带全文")
    if len(prov3.calls) != 1:
        bad.append(f"用例3：总结应调用 1 次，实际 {len(prov3.calls)}")
    if est_tokens(payload3) >= est_tokens(LONG_PAGE["text"]) // 2:
        bad.append("用例3：总结后体量没有明显下降")
    # 总结时应当把「用户当前问题」带进去
    sent = json.dumps(prov3.calls[0][0].__dict__, ensure_ascii=False, default=str)
    if "5070" not in sent:
        bad.append("用例3：总结请求里没有带上用户的问题")

    # ══════════ 用例 4：短正文不总结 ══════════
    print("\n===== 用例 4：短正文 → 不总结，直接给原文 =====")
    prov4 = FakeProvider(raises=True)   # 一旦被调用就会炸
    inst4 = cls(StubContext(prov4), {})
    inst4._browser = FakeBrowser(ITEMS, SHORT_PAGE)
    out4 = json.loads(await inst4.fetch_page(FakeEvent(), url="https://93.184.216.34/x"))
    print(f"  带全文={('text' in out4)}  总结调用次数={len(prov4.calls)}")
    if "text" not in out4:
        bad.append("用例4：短正文应直接返回原文")
    if prov4.calls:
        bad.append("用例4：短正文不该触发总结调用")

    # ══════════ 用例 5：总结失败退回原文 ══════════
    print("\n===== 用例 5：总结失败 → 退回原文（不能空手而归）=====")
    # ★ 必须清缓存：搜索/抓取/总结缓存是【进程级】的，前面的用例可能已经把
    #   同一页、同一问题的成功总结缓存住了 —— 那样这里就根本不会调用模型，
    #   "让模型失败"的设定形同虚设（实测就是这么红的）。
    mod._CACHE.clear()
    prov5 = FakeProvider(raises=True)
    inst5 = cls(StubContext(prov5), {})
    inst5._browser = FakeBrowser(ITEMS, LONG_PAGE)
    out5 = json.loads(await inst5.fetch_page(FakeEvent(), url="https://93.184.216.34/spec"))
    print(f"  带全文={('text' in out5)}  总结调用次数={len(prov5.calls)}")
    if "text" not in out5:
        bad.append("用例5：总结失败后应退回原文")
    if len(prov5.calls) != 1:
        bad.append("用例5：应当尝试过总结")

    # ══════════ 用例 6：没有可用模型时退回原文 ══════════
    print("\n===== 用例 6：没有可用对话模型 → 退回原文 =====")
    mod._CACHE.clear()  # 同上：不清理的话会命中上一轮留下的总结
    inst6 = cls(StubContext(None), {})
    inst6._browser = FakeBrowser(ITEMS, LONG_PAGE)
    out6 = json.loads(await inst6.fetch_page(FakeEvent(), url="https://93.184.216.34/spec"))
    print(f"  带全文={('text' in out6)}")
    if "text" not in out6:
        bad.append("用例6：没有模型时应退回原文")

    # ══════════ 用例 7：关掉总结开关 ══════════
    print("\n===== 用例 7：summarize_page=false → 不总结 =====")
    prov7 = FakeProvider(raises=True)
    inst7 = cls(StubContext(prov7), {"summarize_page": False})
    inst7._browser = FakeBrowser(ITEMS, LONG_PAGE)
    out7 = json.loads(await inst7.fetch_page(FakeEvent(), url="https://93.184.216.34/spec"))
    print(f"  带全文={('text' in out7)}  总结调用次数={len(prov7.calls)}")
    if "text" not in out7 or prov7.calls:
        bad.append("用例7：关掉开关后不该总结")

    if bad:
        print("\n失败项:")
        for item in bad:
            print(f"  ✗ {item}")
        return 1
    print("\nPASS: 搜索瘦身 / 开关行为 / 总结生效 / 三种退回路径 全部正确")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
