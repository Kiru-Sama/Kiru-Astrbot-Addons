# -*- coding: utf-8 -*-
"""验证【AstrBot 自动注入块】被正确捕获并挂到最后一条 user 消息上。

为什么要有这个文件：
    面板以前只看 `contexts` / `system_prompt` / `prompt`，
    而 AstrBot 的「群聊上下文感知」走的是**另一个独立参数**
    `extra_user_content_parts`（见 README §9）。那条链路一直在跑，
    面板却完全看不见 —— 表现就是"AI 明明知道群里刚才聊了什么，
    但面板上请求里找不到任何来源"。

    所以这里专门钉死三件事：
      1. TextPart 的文本 → 挂到最后一条消息的 `inject` 字段
      2. ImageURLPart 计入 image_count（不能漏算图片）
      3. `inject` 的字数要计入 total_chars / estimated_tokens
         （否则上下文用量条会低估，压缩阈值判断跟着偏）

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-自动注入捕获.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8，否则 ✅/❌ 打不出来）
"""
import asyncio
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")


class StubContext:
    """顶掉 register_web_api，让插件能在没有 AstrBot 运行时的情况下实例化。"""

    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


class TextPart:
    """替身：真身是 astrbot.core.agent.message.TextPart，这里只用 .text。"""

    def __init__(self, text):
        self.text = text


class ImageURLPart:
    """替身：类名必须【一模一样】—— _split_extra_parts 靠类名识别图片。"""


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_inject_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_inject_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


def last_snapshot(inst):
    return inst.context_snapshots[-1]


async def main() -> int:
    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    inst = cls(StubContext())
    bad = []

    # ── 用例 1：群聊上下文感知那种注入块，附在 prompt 上 ──────────────
    block = (
        "<system_reminder>You are in a group chat. "
        "Belows are group chat context after your last reply:\n"
        "--- BEGIN CONTEXT---\n"
        "[示例群友A/10:52:01]:  不是说好更新两百m吗\n"
        "[示例群友B/10:52:09]:  十二g了\n"
        "--- END CONTEXT ---\n</system_reminder>"
    )
    inst._capture_text_chat(
        {
            "system_prompt": "你是小助手。",
            "contexts": [],
            "prompt": "在吗",
            "extra_user_content_parts": [TextPart(block), ImageURLPart()],
        }
    )
    snap = last_snapshot(inst)
    msgs = snap["messages"]
    if not msgs:
        bad.append("用例1：messages 是空的")
    else:
        last = msgs[-1]
        if last.get("role") != "user" or last.get("content") != "在吗":
            bad.append(f"用例1：最后一条应为 user/在吗，实际 {last.get('role')!r}/{last.get('content')!r}")
        got = last.get("inject") or ""
        if "BEGIN CONTEXT" not in got:
            bad.append("用例1：inject 里没有注入块的正文")
        if snap.get("inject_chars") != len(got):
            bad.append(f"用例1：inject_chars={snap.get('inject_chars')} 应等于 {len(got)}")
        if snap.get("image_count") != 1:
            bad.append(f"用例1：image_count={snap.get('image_count')} 应为 1（ImageURLPart）")
        if snap.get("total_chars", 0) <= len(got):
            bad.append("用例1：total_chars 没把 inject 算进去")

    # ── 用例 2：没有注入块时不能凭空造出非空 inject ──────────────────
    inst._capture_text_chat(
        {
            "system_prompt": "你是小助手。",
            "contexts": [],
            "prompt": "第二条",
            "extra_user_content_parts": None,
        }
    )
    snap2 = last_snapshot(inst)
    last2 = snap2["messages"][-1]
    if last2.get("inject"):
        bad.append(f"用例2：不该有 inject，却有 {last2['inject']!r}")
    if snap2.get("inject_chars"):
        bad.append("用例2：inject_chars 应为 0")
    if snap2.get("image_count"):
        bad.append("用例2：image_count 应为 0")

    # ── 用例 3：只有注入块、没有 prompt（纯 @ + 空文本的场景）────────
    inst._capture_text_chat(
        {
            "system_prompt": "",
            "contexts": [],
            "prompt": "",
            "extra_user_content_parts": [TextPart("<system_reminder>只有注入</system_reminder>")],
        }
    )
    snap3 = last_snapshot(inst)
    if not snap3["messages"]:
        bad.append("用例3：只有注入块时应该仍然产生一条消息承载它")
    elif "只有注入" not in (snap3["messages"][-1].get("inject") or ""):
        bad.append("用例3：注入块没被挂上（挂在最后一条消息上）")

    # ── 用例 4：_split_extra_parts 对 dict 形式的块也要认 ────────────
    #   entities.py:215-217 同时接受 pydantic 对象和 dict，所以两种形态都得认
    text, imgs, blobs = cls._split_extra_parts(
        [{"type": "text", "text": "  dict 形式的块  "}, {"type": "image_url"}]
    )
    if text != "dict 形式的块":
        bad.append(f"用例4：dict 文本块解析错，得到 {text!r}")
    if blobs != 1:
        bad.append(f"用例4：dict 图片块没认出来，blobs={blobs} 应为 1")
    if imgs:
        bad.append(f"用例4：无路径的图片块不该进 images，得到 {imgs!r}")

    # ── 用例 5：★★ 引用图的标记在【注入块】里，必须挖出来 ───────────
    # 这是用户实报的 bug：引用图"看起来没送到模型"，其实是面板没显示。
    # 根因：标记由 astr_main_agent.py:744-747 写进 extra_user_content_parts，
    #       而先前只在消息 content 里找标记 —— 那条路根本看不到它。
    # 顺便覆盖"直接发的图"，它的标记同样在 extra parts（:1402-1404）。
    quoted_block = (
        "正文在这里。"
        "[Image Attachment in quoted message: path F:\\tmp\\quoted_shot.jpg]"
        "<Quoted Message>\n(示例群友A): [Image]\n</Quoted Message>\n"
        "[Image Attachment: path F:\\tmp\\direct_shot.jpg]"
    )
    text5, imgs5, blobs5 = cls._split_extra_parts([TextPart(quoted_block)])
    kinds = sorted((im["q"], im["n"]) for im in imgs5)
    print(f"  用例5：挖出 {len(imgs5)} 张图 -> {kinds}")
    if len(imgs5) != 2:
        bad.append(f"用例5：应挖出 2 张图（1 引用 + 1 直发），实际 {len(imgs5)}")
    if sorted(im["q"] for im in imgs5) != [False, True]:
        bad.append(f"用例5：引用/直发标记不对，得到 {[im['q'] for im in imgs5]}")
    if any(not im["k"] for im in imgs5):
        bad.append("用例5：图片应带上缩略图缓存键")
    for im in imgs5:
        if im["n"] not in ("quoted_shot.jpg", "direct_shot.jpg"):
            bad.append(f"用例5：文件名解析错 {im['n']!r}")
    if "Image Attachment" in text5:
        bad.append(f"用例5：标记文本没被摘干净，残留 {text5!r}")
    if "正文在这里" not in text5:
        bad.append(f"用例5：正常正文被误删了，得到 {text5!r}")

    # 自检：把 _extract_images 换成空操作，图就该一个都挖不出来 ——
    # 证明上面那 2 张【确实是这个提取步骤挖出来的】，不是从别处漏进来的。
    # （不做这步的话，一个恒真的断言看起来也是绿的。）
    orig_extract = cls.__dict__["_extract_images"]

    @classmethod
    def _noop(cls_, raw):  # noqa: ANN001, ANN202
        return raw, []

    cls._extract_images = _noop
    try:
        _t0, imgs0, _b0 = cls._split_extra_parts([TextPart(quoted_block)])
    finally:
        cls._extract_images = orig_extract
    print(f"  用例5自检：禁用提取后挖出 {len(imgs0)} 张（应为 0）")
    if imgs0:
        bad.append(
            f"用例5自检：禁用 _extract_images 后仍挖出 {len(imgs0)} 张，"
            f"说明这条断言测不到提取逻辑（形同虚设）"
        )

    # 这个块进快照后，图要挂到消息上、且计入 image_count
    inst._capture_text_chat(
        {
            "system_prompt": "",
            "contexts": [],
            "prompt": "看看这个",
            "extra_user_content_parts": [TextPart(quoted_block)],
        }
    )
    snap5 = last_snapshot(inst)
    last5 = snap5["messages"][-1]
    got5 = last5.get("images") or []
    print(f"  用例5：快照最后一条消息带图 {len(got5)} 张，"
          f"image_count={snap5.get('image_count')}")
    if len(got5) != 2:
        bad.append(f"用例5：图应挂到最后一条消息上，实际 {len(got5)} 张")
    if snap5.get("image_count") != 2:
        bad.append(
            f"用例5：image_count 应为 2，实际 {snap5.get('image_count')}"
            f"（引用图没被计数就是这个 bug 的表征）"
        )
    # 缩略图索引必须登记，否则 /thumb 会 404
    for im in got5:
        if not inst._thumb_paths.get(im["k"]):
            bad.append(f"用例5：缩略图索引里没有登记 {im['k']}")

    if bad:
        print("FAIL")
        for b in bad:
            print("  -", b)
        return 1

    print("PASS  自动注入块捕获 5 个用例都过（含引用图/直发图标记）")
    print(f"  用例1 inject 长度 = {len(last.get('inject') or '')} 字，"
          f"image_count = {snap.get('image_count')}，"
          f"total_chars = {snap.get('total_chars')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
