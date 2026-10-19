# -*- coding: utf-8 -*-
"""回归护栏：压缩期间的「施工中」状态 + 压缩摘要的流式显示。

背景：
  压缩器用的是 `provider.text_chat(contexts=...)`（compressor.py:276）——【非流式】，
  所以压缩那段时间面板上什么都看不到，只能靠 GPU 占用猜。
  而且压缩器只取 `completion_text`（compressor.py:279），
  而 `text_chat_stream` 最后一次 yield 就是完整结果（provider.py:154），
  所以把这次调用换成流式是【等价】的。

  于是：压缩期间改走流式 → 摘要在右栏逐字显示，那块套黄框标「正在压缩中」。

这里守三件事：
  ① 压缩时确实改走了流式，且返回给压缩器的仍是等价的完整结果
  ② 流式过程中能观测到 mode="compress" 且文本【递增】（不是流完才一次性出现）
  ③ 流式一旦失败【必须退回非流式】—— 监控代码不能把压缩搞挂
  ④ 压缩标记用完必须复位，否则后面所有普通回复都会被当成压缩

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-压缩流式.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""
import asyncio
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")


class StubContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


class _Plain:
    def __init__(self, text):
        self.text = text


class _Chain:
    def __init__(self, *parts):
        self.chain = list(parts)


class FakeChunk:
    """照抄 openai_source._query_stream 的形状。"""

    def __init__(self, text=None, *, is_chunk=True, usage=None):
        self.is_chunk = is_chunk
        self.result_chain = _Chain(_Plain(text)) if text else None
        self.completion_text = "" if is_chunk else (text or "")
        self.usage = usage
        self.tools_call_name = None
        self.reasoning_content = None


class Self:
    """只当 self_ 用。

    ★ 不能用 Provider.__new__ —— Provider 是抽象类，
      "Can't instantiate abstract class Provider without an implementation for
       abstract methods 'get_current_key', 'get_models', 'set_key', 'text_chat'"。
      而 _run_compress_stream 需要 self_.text_chat_stream 能解析到包装函数，
      所以下面把它【绑】到这个对象上（MethodType），而不是挂类属性。
    """


def make_self(provider_cls):
    import types

    obj = Self()
    obj.text_chat_stream = types.MethodType(
        provider_cls.__dict__["text_chat_stream"], obj
    )
    return obj


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_compress_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_compress_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


async def main() -> int:
    from astrbot.core.agent.context import compressor as compressor_mod
    from astrbot.core.provider.provider import Provider

    bad: list[str] = []
    calls = {"stream": 0, "plain": 0}
    # 流式过程中抓的实时块快照（用来证明"增量可见"）
    snaps: list[dict] = []

    async def raw_plain(self_, *a, **k):
        calls["plain"] += 1
        return FakeChunk("【非流式结果】", is_chunk=False)

    def make_stream(texts, final_text, *, fail_after=None, inst_holder=None):
        async def stream(self_, *a, **k):
            calls["stream"] += 1
            for idx, t in enumerate(texts):
                yield FakeChunk(t)
                if inst_holder and inst_holder.get("inst") is not None:
                    snaps.append(inst_holder["inst"]._live_payload())
                if fail_after is not None and idx == fail_after:
                    raise RuntimeError("模拟压缩流式失败")
            yield FakeChunk(final_text, is_chunk=False)

        return stream

    # 压缩器原函数换成假的：记录它被调用时看到的 _COMPRESSING
    seen_flag: list[bool] = []

    async def fake_compressor_call(self_, messages):
        seen_flag.append(mod._COMPRESSING.get())
        return []

    # ★ 必须在加载插件【之前】把这些假方法装到类上 ——
    #   插件的包装是在加载时抓取当时的原函数，之后再换就换不掉了。
    Provider.text_chat = raw_plain
    Provider.text_chat_stream = make_stream(["上下文", "压缩", "摘要"], "上下文压缩摘要")
    compressor_mod.LLMSummaryCompressor.__call__ = fake_compressor_call

    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1
    inst = cls(StubContext())
    holder = {"inst": inst}

    provider = make_self(Provider)  # 只当 self_ 用，见 Self 的说明

    def repatch(stream):
        """还原全部包装 → 换成新的假流 → 重新包装。三个用例各来一次。"""
        mod._unwrap_all()
        Provider.text_chat = raw_plain
        Provider.text_chat_stream = stream
        inst._patched_provider_names.clear()
        inst._patched_stream_names.clear()
        inst._ensure_providers_patched()
        # ★ 重新包装后必须把新的包装函数重新绑到 provider 上
        provider.text_chat_stream = __import__("types").MethodType(
            Provider.__dict__["text_chat_stream"], provider
        )
        return Provider.__dict__["text_chat"]

    # ══════════════ 用例 1：压缩时改走流式 ══════════════
    print("===== 用例 1：压缩中 → 改走流式，增量可见 =====")
    stream = make_stream(["上下文", "压缩", "摘要"], "上下文压缩摘要", inst_holder=holder)
    wrapper = repatch(stream)
    calls["stream"] = calls["plain"] = 0
    snaps.clear()

    token = mod._COMPRESSING.set(True)
    try:
        resp = await wrapper(provider, contexts=[{"role": "user", "content": "x"}])
    finally:
        mod._COMPRESSING.reset(token)

    text = getattr(resp, "completion_text", "") or ""
    print(f"  返回给压缩器的文本 = {text!r}")
    print(f"  流式调用 {calls['stream']} 次 / 非流式调用 {calls['plain']} 次")
    if text != "上下文压缩摘要":
        bad.append(f"用例1：返回的不是流式最终结果，而是 {text!r}")
    if calls["stream"] != 1:
        bad.append(f"用例1：流式被调 {calls['stream']} 次，应为 1")
    if calls["plain"] != 0:
        bad.append(f"用例1：不该调非流式，却调了 {calls['plain']} 次")

    print(f"  流式过程中抓到的快照 {len(snaps)} 个")
    if len(snaps) < 3:
        bad.append(f"用例1：流式过程中只抓到 {len(snaps)} 个快照，测不出增量")
    else:
        modes = {s.get("mode") for s in snaps}
        lens = [s.get("chars") for s in snaps]
        print(f"    各快照 mode={modes} chars={lens}")
        if modes != {"compress"}:
            bad.append(f"用例1：压缩期间 mode 应为 compress，实际 {modes}")
        if len(set(lens)) < 3:
            bad.append(f"用例1：文本长度没有递增（{lens}），说明不是增量显示")
        # ★ 这里查的是【回退】（长度变小）。写成 b > a 就变成"对递增报警"了 ——
        #   第一版就是写反的，结果正确的实现被判失败。
        if any(b < a for a, b in zip(lens, lens[1:])):
            bad.append(f"用例1：文本长度出现回退（{lens}）")

    live = inst._live_payload()
    print(f"  结束后 live: mode={live.get('mode')} done={live.get('done')} "
          f"chars={live.get('chars')}")
    if live.get("mode") != "compress":
        bad.append("用例1：结束后 mode 被改成了别的")
    if not live.get("done"):
        bad.append("用例1：结束后 done 应为 True（前端据此摘掉黄框）")

    # ══════════════ 用例 2：流式失败必须退回非流式 ══════════════
    print("\n===== 用例 2：压缩流式失败 → 退回非流式（压缩不受影响）=====")
    stream = make_stream(["部分"], "不该用到", fail_after=0, inst_holder=holder)
    wrapper = repatch(stream)
    calls["stream"] = calls["plain"] = 0

    token = mod._COMPRESSING.set(True)
    try:
        resp = await wrapper(provider, contexts=[{"role": "user", "content": "x"}])
    finally:
        mod._COMPRESSING.reset(token)

    text = getattr(resp, "completion_text", "") or ""
    print(f"  返回文本 = {text!r}")
    print(f"  流式调用 {calls['stream']} 次 / 非流式调用 {calls['plain']} 次")
    if text != "【非流式结果】":
        bad.append(f"用例2：没有退回非流式，拿到的是 {text!r}")
    if calls["plain"] != 1:
        bad.append(f"用例2：非流式应被调 1 次，实际 {calls['plain']}")
    live = inst._live_payload()
    if not live.get("done"):
        bad.append("用例2：失败后黄框状态没收尾（done 应为 True）")

    # ══════════════ 用例 3：非压缩时走原路，不碰实时块 ══════════════
    print("\n===== 用例 3：非压缩 → 走原路，不碰实时块 =====")
    stream = make_stream(["不该出现"], "不该出现", inst_holder=holder)
    wrapper = repatch(stream)
    calls["stream"] = calls["plain"] = 0
    inst._live_clear()

    resp = await wrapper(provider, contexts=[{"role": "user", "content": "x"}])
    text = getattr(resp, "completion_text", "") or ""
    print(f"  返回文本 = {text!r}  流式={calls['stream']} 非流式={calls['plain']}")
    print(f"  实时块 = {inst._live_payload()}")
    if text != "【非流式结果】":
        bad.append(f"用例3：非压缩时应走非流式，拿到 {text!r}")
    if calls["stream"] != 0:
        bad.append(f"用例3：非压缩时不该走流式，却调了 {calls['stream']} 次")
    if inst._live_payload() is not None:
        bad.append("用例3：非压缩时不该动实时块")

    # ══════════════ 用例 4：压缩标记必须复位 ══════════════
    print("\n===== 用例 4：压缩标记用完即复位 =====")
    mod._unwrap_all()
    Provider.text_chat = raw_plain
    Provider.text_chat_stream = make_stream(["x"], "x")
    inst._patched_provider_names.clear()
    inst._patched_stream_names.clear()
    inst._ensure_providers_patched()
    inst._patch_compressor()

    seen_flag.clear()
    mod._ACTIVE = inst
    comp_cls = compressor_mod.LLMSummaryCompressor
    comp_wrapper = comp_cls.__dict__.get("__call__")
    if comp_wrapper is None or comp_wrapper is fake_compressor_call:
        bad.append("用例4：压缩器包装没装上，这条测不出东西")
    else:
        await comp_wrapper(object(), [])
        print(f"  压缩器原函数执行时看到的标记 = {seen_flag}")
        print(f"  执行完之后标记 = {mod._COMPRESSING.get()}")
        if seen_flag != [True]:
            bad.append(f"用例4：压缩期间标记应为 True，实际 {seen_flag}")
        if mod._COMPRESSING.get() is not False:
            bad.append("用例4：标记没复位 —— 后面所有回复都会被当成压缩")

    mod._unwrap_all()

    if bad:
        print("\n失败项:")
        for item in bad:
            print(f"  ✗ {item}")
        return 1
    print("\nPASS: 压缩改走流式 / 增量可见 / 失败退回非流式 / 标记复位 全部正确")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
