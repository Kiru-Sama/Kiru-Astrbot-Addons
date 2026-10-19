# -*- coding: utf-8 -*-
"""回归护栏：流式链路的捕获、「常驻输出」与「提前关闭也不能丢回复」。

背景（两件事都是真实踩出来的）：

  ① 开流式之后，tool_loop_agent_runner.py:515 会改调 text_chat_stream，
     只包了 text_chat 的插件【整条被绕过去】—— 面板右栏直接变空、零报错。
     这就是报告里的 4.6。

  ② 收尾写在 `async for` 之后是不可靠的：消费者可以【提前 return】或调
     aclose()，那时会在 yield 处抛 GeneratorExit，循环后面的代码一行都不执行。
     实测现象是"模型明明回了、QQ 也收到了，面板上这条却没有回复"。
     （runner 里就有 `if resp is None: return`，它的 finally 还会关掉 stream。）
     ⇒ 收尾必须放 finally。

另外「常驻」是产品语义：生成完、发出去之后那块【不消失】，留着回头看，
下次开始生成才覆盖。所以这里的断言是"还在"，不是"没了"。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-流式捕获.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""
import asyncio
import importlib.util
import logging
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


class _Usage:
    def __init__(self, i, o, c):
        self.input = i
        self.output = o
        self.input_cached = c


class FakeChunk:
    """照抄 openai_source._query_stream 的形状：
    增量片段（is_chunk=True）文本在 result_chain 里、completion_text 是空串；
    最终结果（is_chunk=False）completion_text 是完整文本。

    ★ reasoning_content 也照抄那个坑：openai_source.py:663 每轮把它清空再填
      【这一轮的增量】，所以只有逐块读才拿得到全部；事后读最后那个对象只剩残片。
    """

    def __init__(self, text=None, *, is_chunk=True, usage=None, tools=None,
                 reasoning=None):
        self.is_chunk = is_chunk
        self.result_chain = _Chain(_Plain(text)) if text else None
        self.completion_text = "" if is_chunk else (text or "")
        self.usage = usage
        self.tools_call_name = tools
        self.reasoning_content = reasoning


def make_fake_stream(chunks, fail_after=None):
    async def fake_stream(self_, *args, **kwargs):
        for idx, c in enumerate(chunks):
            yield c
            if fail_after is not None and idx == fail_after:
                raise RuntimeError("模拟流中途失败")

    return fake_stream


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_stream_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_stream_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):  # noqa: ANN001
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass


async def expected_old_shape_attaches(chunks) -> bool:
    """自检用：用【旧写法】（收尾写在 async for 之后）跑一遍，看它会不会挂上响应。

    返回值应当是 False —— 否则说明"提前关闭会丢响应"这个前提不成立，
    也就意味着用例 4 测不出东西。
    """
    attached = {"done": False}

    async def old_shape():
        acc = []
        try:
            for c in chunks:
                acc.append(getattr(c, "completion_text", "") or "")
                yield c
        finally:
            pass
        # ↓↓↓ 旧写法：收尾在这里（循环之后）。提前 aclose() 时这一行不会执行。
        attached["done"] = True

    gen = old_shape()
    await anext(gen)
    await gen.aclose()
    return attached["done"]


async def drain(wrapper, **kwargs):
    got = []
    async for c in wrapper(None, **kwargs):
        got.append(c)
    return got


async def main() -> int:
    from astrbot.core.provider.provider import Provider

    WANT = ["你好", "，世界", "！"]
    USAGE = _Usage(4111, 17, 200)
    bad = []

    # ── 用例 1~3：正常一条流 ────────────────────────────────────────
    print("===== 用例 1~3：正常流（3 个增量 + 1 个完整结果）=====")
    chunks = [FakeChunk(t) for t in WANT] + [
        FakeChunk("你好，世界！", is_chunk=False, usage=USAGE, tools=["future_task"])
    ]
    Provider.text_chat_stream = make_fake_stream(chunks)

    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1
    inst = cls(StubContext())

    wrapper = Provider.__dict__.get("text_chat_stream")
    print(f"  _patched_stream_names = {inst._patched_stream_names}")

    got = await drain(wrapper, prompt="测试", contexts=[])
    print(f"  收到的 chunk 数 = {len(got)}（应为 {len(chunks)}）")
    if len(got) != len(chunks):
        bad.append(f"用例1：chunk 数量不对，{len(got)} != {len(chunks)}")
    if any(g is not c for g, c in zip(got, chunks)):
        bad.append("用例1：透传的 chunk 不是原对象（改了流式语义）")

    live_text = inst._live_text()
    print(f"  实时缓冲文本 = {live_text!r}")
    if live_text != "你好，世界！":
        bad.append(f"用例2：实时缓冲应为 '你好，世界！'，实际 {live_text!r}")
    if live_text == "你好，世界！你好，世界！":
        bad.append("用例3：最后那个完整结果被重复累加进缓冲了（文字翻倍）")

    snap = inst.context_snapshots[-1] if inst.context_snapshots else None
    if snap is None:
        bad.append("用例5：根本没有生成快照")
    else:
        r = snap.get("response") or {}
        print(f"  快照响应文本 = {r.get('text')!r}  token = "
              f"{r.get('input_tokens')}/{r.get('output_tokens')}")
        if r.get("text") != "你好，世界！":
            bad.append(f"用例5：快照响应文本应为完整结果，实际 {r.get('text')!r}")
        if r.get("input_tokens") != 4111:
            bad.append(f"用例5：真实 token 丢了，input={r.get('input_tokens')}")
        if r.get("tool_calls") != ["future_task"]:
            bad.append(f"用例5：工具名没记上，{r.get('tool_calls')}")

    # ── 用例 6：「常驻」—— 结束之后不能自己消失 ──────────────────────
    print()
    print("===== 用例 6：常驻（结束后仍在，且能标出「已发出」）=====")
    p = inst._live_payload()
    print(f"  结束后的 _live_payload(): done={p and p.get('done')} "
          f"chars={p and p.get('chars')}")
    if not p or not p.get("done"):
        bad.append("用例6：结束后 _live_payload() 应仍是 done=True（常驻）")
    if p and p.get("text") != "你好，世界！":
        bad.append("用例6：常驻内容应保持完整的生成文本")
    inst._live_mark_sent()          # ← on_message_sent 干的事
    p2 = inst._live_payload()
    print(f"  标记已发出后: sent={p2 and p2.get('sent')} text 还在={bool(p2 and p2.get('text'))}")
    if not p2 or not p2.get("sent"):
        bad.append("用例6：标记已发出后 payload.sent 应有时间戳")
    if not p2 or not p2.get("text"):
        bad.append("★核心：标记已发出之后【不应该清空】——用户要的是常驻")
    print()

    # ── 用例 4：流中途抛异常必须原样抛出 ───────────────────────────
    print("===== 用例 4：流中途抛异常 =====")
    await inst.terminate()
    Provider.text_chat_stream = make_fake_stream(chunks, fail_after=1)
    inst2 = cls(StubContext())
    wrapper2 = Provider.__dict__["text_chat_stream"]

    raised = None
    got2 = []
    try:
        async for c in wrapper2(None, prompt="测试2", contexts=[]):
            got2.append(c)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    print(f"  收到 {len(got2)} 个 chunk 后异常：{type(raised).__name__}")
    if not isinstance(raised, RuntimeError):
        bad.append(f"用例4：异常应原样抛出 RuntimeError，实际 {type(raised).__name__}")
    if len(got2) != 2:
        bad.append(f"用例4：抛出前应已透传 2 个 chunk，实际 {len(got2)}")
    if (inst2._live_current()).get("ok") is not False:
        bad.append("用例4：异常结束后实时缓冲应标记 ok=False")
    snap2 = inst2.context_snapshots[-1] if inst2.context_snapshots else None
    if snap2 is None or not snap2.get("error"):
        bad.append("用例4：异常的快照应被标记 error")
    print()

    # ── 用例 8：消费者【提前关闭】流，响应必须仍然挂上 ────────────────
    print("===== 用例 8：消费者提前 aclose()（← 这条最容易丢回复）=====")
    await inst2.terminate()
    Provider.text_chat_stream = make_fake_stream(chunks)

    # 先自检：旧写法（收尾在循环之后）在这种情况下确实挂不上响应。
    # 否则这条用例就是摆设 —— 断言必须能失败才有意义。
    old_ok = await expected_old_shape_attaches(chunks[:2])
    print(f"  自检·旧写法在这种场景下挂上响应了吗 = {old_ok}（应为 False）")
    if old_ok:
        bad.append("自检失败：旧写法竟然也挂上了，说明用例8测不出问题")

    cap = _Capture()
    mod.logger.addHandler(cap)
    old_level = mod.logger.level
    mod.logger.setLevel(logging.INFO)
    try:
        inst3 = cls(StubContext())
        wrapper3 = Provider.__dict__["text_chat_stream"]
        gen = wrapper3(None, prompt="测试8", contexts=[])
        await anext(gen)           # 收第 1 个增量
        await anext(gen)           # 收第 2 个增量
        await gen.aclose()         # ← 模拟消费者提前 return / _close_executor
    finally:
        mod.logger.removeHandler(cap)
        mod.logger.setLevel(old_level)

    snap3 = inst3.context_snapshots[-1] if inst3.context_snapshots else None
    r3 = (snap3 or {}).get("response") or {}
    print(f"  提前关闭后：快照有响应 = {bool(r3)}  文本 = {r3.get('text')!r}")
    print(f"  实时缓冲 finished 有值 = {(inst3._live_current()).get('finished') is not None}")
    if not r3:
        bad.append(
            "★核心：提前关闭时响应【丢了】—— 收尾必须放 finally，"
            "否则 QQ 收到了、面板上这条却没有回复"
        )
    elif r3.get("text") != "你好，世界":
        bad.append(f"用例8：提前关闭时应记录已生成的部分文本，实际 {r3.get('text')!r}")
    if (inst3._live_current()).get("finished") is None:
        bad.append("用例8：提前关闭后实时缓冲也应标记结束（否则面板永远显示「生成中」）")
    if not any("被提前关闭" in x for x in cap.lines):
        bad.append("用例8：诊断日志里应标出「被提前关闭」，方便以后定位")
    if (inst3._live_current()).get("ok") is not False:
        bad.append("用例8：没拿到完整结果就被关闭，应算异常（ok=False）")
    print()

    # ── 用例 9：拿到【完整结果】之后才被关闭 = 正常收尾，不能报"提前结束" ──
    # 这是实测出来的：每次真实请求都是这个形状 —— wrapper 把最后那个完整 chunk
    # yield 出去、消费者拿走就收工关流，GeneratorExit 落在那个 yield 上。
    # 一律算失败的话，面板会每次显示「提前结束」，真出问题时反而没人看。
    print("===== 用例 9：拿到完整结果后 aclose（真实请求的正常形状）=====")
    await inst3.terminate()
    Provider.text_chat_stream = make_fake_stream(chunks)
    inst4 = cls(StubContext())
    wrapper4 = Provider.__dict__["text_chat_stream"]
    gen4 = wrapper4(None, prompt="测试9", contexts=[])
    last_chunk = None
    for _ in range(len(chunks)):
        last_chunk = await anext(gen4)   # 一路收到最后那个完整结果
    await gen4.aclose()                  # 消费者收工关流
    r4 = (inst4.context_snapshots[-1].get("response") or {})
    ok4 = (inst4._live_current()).get("ok")
    print(f"  完整结果已收到 = {last_chunk is chunks[-1]}"
          f"  快照响应文本 = {r4.get('text')!r}  live.ok = {ok4}")
    if r4.get("text") != "你好，世界！":
        bad.append(f"用例9：快照响应应为完整结果，实际 {r4.get('text')!r}")
    if ok4 is not True:
        bad.append(
            "★用例9：拿到完整结果后被关闭属于正常收尾，ok 应为 True"
            "（否则面板每次都显示「提前结束」= 狼来了）"
        )
    print()

    # ── 用例 10：压缩那一次不能被抓成两条快照 ──────────────────────────
    # ★ 这是实测出来的回归：让压缩改走流式之后，压缩请求会【连着穿过两个包装】——
    #   先从 text_chat 包装进（_run_compress_stream 就是从那儿出发的），
    #   再进 text_chat_stream 包装。两边都 _capture_text_chat 的话，
    #   面板上会同时出现两条一模一样的 compress 快照（实测同一毫秒两条）。
    #   所以 stream 包装在 _COMPRESSING 为真时必须跳过捕获。
    print("===== 用例 10：压缩时不重复抓快照 =====")
    await inst4.terminate()
    Provider.text_chat_stream = make_fake_stream(chunks)
    inst5 = cls(StubContext())
    wrapper5 = Provider.__dict__["text_chat_stream"]

    before = len(inst5.context_snapshots)
    await drain(wrapper5, prompt="普通", contexts=[])
    after_normal = len(inst5.context_snapshots)
    print(f"  非压缩：快照 {before} → {after_normal}（应 +1）")
    if after_normal != before + 1:
        bad.append(
            f"用例10：非压缩时该抓一条快照，实际 +{after_normal - before}"
        )

    token = mod._COMPRESSING.set(True)
    try:
        await drain(wrapper5, prompt="压缩", contexts=[])
    finally:
        mod._COMPRESSING.reset(token)
    after_compress = len(inst5.context_snapshots)
    print(f"  压缩中：快照 {after_normal} → {after_compress}（应 +0，"
          f"由 text_chat 包装那边抓）")
    if after_compress != after_normal:
        bad.append(
            f"用例10：压缩时又抓了一条重复快照（+{after_compress - after_normal}）"
            " —— 面板上会出现两条一模一样的 compress 记录"
        )

    # 复位之后必须恢复抓取，否则是「关掉捕获」而不是「压缩时跳过」
    await drain(wrapper5, prompt="复位后", contexts=[])
    after_back = len(inst5.context_snapshots)
    print(f"  复位后：快照 {after_compress} → {after_back}（应 +1）")
    if after_back != after_compress + 1:
        bad.append("用例10：压缩标记复位后没有恢复抓取 —— 会丢掉普通请求的快照")
    print()

    await inst5.terminate()

    # ── 用例 11：收尾跟着一个「空壳」非 delta，不能顶掉真正的完整结果 ────
    # ★ 这是审查提出、我实测确认的一条：只看 is_chunk=False 就当成最终结果，
    #   是 openai_source 的约定；别的 provider 会在收尾再吐一个纯 usage/done 的
    #   空壳。它一旦被当成 final，面板就会【安静地显示错内容】—— 因为兜底只在
    #   final is None 时触发，永远不会纠正它。
    print("===== 用例 11：收尾的空壳非 delta 不能顶掉真正的完整结果 =====")
    await inst5.terminate()
    chunks11 = [
        FakeChunk("你好"),
        FakeChunk("，世界"),
        FakeChunk("你好，世界", is_chunk=False, usage=USAGE),
        FakeChunk(None, is_chunk=False),        # ← 收尾空壳
    ]

    # 自检：按【旧规则】（任何非 delta 都算 final）走一遍，它应该选中那个空壳。
    # 不成立的话这条用例就是摆设。
    old_final = None
    for c in chunks11:
        if not getattr(c, "is_chunk", False):
            old_final = c
    old_text = getattr(old_final, "completion_text", "") or ""
    print(f"  自检·旧规则选中的 final 文本 = {old_text!r}（应为空）")
    if old_text != "":
        bad.append("自检失败：旧规则下这个空壳也有文本，用例11测不出东西")

    Provider.text_chat_stream = make_fake_stream(chunks11)
    inst6 = cls(StubContext())
    wrapper6 = Provider.__dict__["text_chat_stream"]
    await drain(wrapper6, prompt="测试11", contexts=[])
    r11 = (inst6.context_snapshots[-1].get("response") or {})
    print(f"  实际快照文本 = {r11.get('text')!r}（应为 '你好，世界'）")
    if r11.get("text") != "你好，世界":
        bad.append(
            f"★用例11：空壳顶掉了真正的完整结果，面板显示 {r11.get('text')!r}"
        )
    print()

    # ── 用例 12：只有空壳、没有真正的完整结果 → 退回累积文本，不能显示空 ──
    print("===== 用例 12：只有空壳时退回累积文本（不能显示空）=====")
    await inst6.terminate()
    chunks12 = [FakeChunk("你"), FakeChunk("好"), FakeChunk(None, is_chunk=False)]
    Provider.text_chat_stream = make_fake_stream(chunks12)
    inst7 = cls(StubContext())
    wrapper7 = Provider.__dict__["text_chat_stream"]
    await drain(wrapper7, prompt="测试12", contexts=[])
    r12 = (inst7.context_snapshots[-1].get("response") or {})
    print(f"  快照文本 = {r12.get('text')!r}（应为 '你好'，即累积文本）")
    if r12.get("text") != "你好":
        bad.append(
            f"用例12：应退回累积文本 '你好'，实际 {r12.get('text')!r}"
            " —— 面板会整条显示空内容"
        )
    await inst7.terminate()
    print()

    # ── 用例 13：思考过程必须【逐块攒】，不能只留最后一轮残片 ──────────
    # openai_source.py:663 每轮把 llm_response.reasoning_content 清空再填这一轮的
    # 增量（跟 completion_text 同一个"对象复用"坑）。所以事后从最后那个对象上读
    # 只能拿到残片 —— 必须像正文一样，在 yield 的那一刻就把增量抠出来。
    print("===== 用例 13：思考过程（reasoning）的捕获 =====")
    await inst7.terminate()
    chunks13 = [
        FakeChunk("答案第一段", reasoning="分析第一步："),
        FakeChunk("答案第二段", reasoning="分析第二步。"),
        # 最终结果按 openai_source 的形状【不带】完整思考（或被清成 None）
        FakeChunk("答案第一段答案第二段", is_chunk=False, usage=USAGE),
    ]
    Provider.text_chat_stream = make_fake_stream(chunks13)
    inst8r = cls(StubContext())
    wrapper8 = Provider.__dict__["text_chat_stream"]
    await drain(wrapper8, prompt="测试13", contexts=[])

    acc = inst8r._live_reasoning()
    snap8 = inst8r.context_snapshots[-1] if inst8r.context_snapshots else None
    r8 = (snap8 or {}).get("response") or {}
    live8 = inst8r._live_payload() or {}
    print(f"  逐块攒到 = {acc!r}")
    print(f"  快照里   = {r8.get('reasoning')!r}")
    print(f"  实时块里 = {live8.get('reasoning_chars')} 字")
    if acc != "分析第一步：分析第二步。":
        bad.append(f"★用例13：思考过程没攒全，实际 {acc!r}")
    if r8.get("reasoning") != acc:
        bad.append(
            f"★用例13：快照里的思考过程应等于逐块攒的全文，实际 {r8.get('reasoning')!r}"
        )
    if live8.get("reasoning_chars") != len(acc):
        bad.append("用例13：实时块没把思考过程的字数暴露给前端")

    # 负面对照：证明"事后从最后那个对象读"确实拿不到（否则这条用例是空转）
    final_chunk = chunks13[-1]
    if getattr(final_chunk, "reasoning_content", None) is not None:
        bad.append("自检失败：最终结果竟然带着完整思考，用例13测不出东西")
    else:
        print("  负面对照：最终结果的 reasoning_content 是 None（事后读只剩空）")

    # 正文不能因此受影响：思考块是分开存的
    if r8.get("text") != "答案第一段答案第二段":
        bad.append(f"用例13：正文被思考过程影响了，实际 {r8.get('text')!r}")
    await inst8r.terminate()
    print()

    print("===== 判定 =====")
    if bad:
        print("FAIL")
        for x in bad:
            print("  -", x)
        return 1
    print("PASS  流式：透传不变 / 累积正确 / 不重复累加 / 异常原样抛 /")
    print("      常驻不消失 / 提前关闭也不丢回复")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
