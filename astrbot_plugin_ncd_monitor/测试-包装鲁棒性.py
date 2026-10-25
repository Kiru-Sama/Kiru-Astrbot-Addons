# -*- coding: utf-8 -*-
"""回归护栏：monitor 两处「包装/过滤」的鲁棒性。

审查提出、我实测确认的两条：

  ① token 计数器是【类级替换】方法本身，而签名写死了
     `patched(self_, messages, trusted_token_usage=0)`。
     AstrBot 哪天给 count_tokens 加个参数，Python 会在【进入函数体之前】就抛
     TypeError —— 那时还没进 try，异常直接冒到压缩链路。
     另外失败兜底返回 0 也不对：0 的含义是「上下文是空的」，会让压缩永远不触发。

  ⑥ 工具箱抓完正文会自己调 provider.text_chat 做总结，那是另一条管道上的调用。
     monitor 把 Provider.text_chat 整个包了，于是那 4000 字网页正文会被记成一次
     正常会话请求 —— 面板右栏凭空多一坨，「快压缩了吗」的判断跟着虚高。
     靠一个约定标记自报身份排除掉。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-包装鲁棒性.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""
import asyncio
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")
TOOLBOX_MAIN = os.path.join(
    os.path.dirname(HERE), "astrbot_plugin_ncd_toolbox", "main.py"
)


class StubContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


class _Msg:
    """够用的 Message 替身：_capture_text_chat 只读 .content / .role。"""

    def __init__(self, content, role="user"):
        self.content = content
        self.role = role


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
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
    bad = []

    from astrbot.core.agent.context.token_counter import EstimateTokenCounter
    from astrbot.core.agent.message import Message

    mod = load_module("ncd_wrap_probe", PLUGIN)
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1
    inst = cls(StubContext())

    # ── ① count_tokens 补丁 ────────────────────────────────────────
    print("===== 用例 1：token 计数器补丁的签名容错 =====")
    counter = EstimateTokenCounter()
    msgs = [Message(role="user", content="你好，这是一段用来估 token 的文字。")]

    normal = counter.count_tokens(msgs)
    print(f"  正常调用 = {normal}（应 > 0）")
    if not isinstance(normal, int) or normal <= 0:
        bad.append(f"用例1：正常调用应返回正整数，实际 {normal!r}")

    trusted = counter.count_tokens(msgs, trusted_token_usage=9999)
    print(f"  带可信用量 = {trusted}（应 9999）")
    if trusted != 9999:
        bad.append(f"用例1：给了可信用量应直接采用，实际 {trusted!r}")

    # 模拟「AstrBot 以后给这个方法加了一个参数」
    raised = None
    fallback = None
    try:
        fallback = counter.count_tokens(msgs, trusted_token_usage=0, future_arg=1)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    print(f"  多一个未知参数 → 异常={raised!r} 返回值={fallback!r}")
    if raised is not None:
        bad.append(
            f"★用例1：未知参数把异常冒出去了（{type(raised).__name__}）—— "
            "这正是升级 AstrBot 后会炸压缩链路的形状"
        )
    if fallback == 0:
        bad.append(
            "用例1：失败兜底返回了 0 —— 0 的含义是「上下文是空的」，"
            "会让压缩判断偏低、永远不触发压缩"
        )
    if not isinstance(fallback, int) or fallback <= 0:
        bad.append(f"用例1：失败兜底应给一个正的数量级估计，实际 {fallback!r}")

    # 负面对照：旧签名遇到未知参数【确实】会抛。没有这条，上面的断言可能是空转。
    original = EstimateTokenCounter.__dict__.get("count_tokens")
    def old_patched(self_, messages, trusted_token_usage=0):  # noqa: ANN001
        return original(self_, messages, 0)

    old_raised = None
    try:
        old_patched(counter, msgs, trusted_token_usage=0, future_arg=1)
    except TypeError as exc:
        old_raised = exc
    print(f"  负面对照·旧签名遇到未知参数 → {type(old_raised).__name__ if old_raised else '没有抛'}")
    if old_raised is None:
        bad.append("自检失败：旧签名竟然也不抛，用例1测不出东西")
    print()

    # ── ⑥ 内部调用不记进会话统计 ────────────────────────────────────
    print("===== 用例 2：工具箱的网页总结请求不能被当成会话请求 =====")
    marker = mod._INTERNAL_REQUEST_MARKERS[0]
    before = len(inst.context_snapshots)

    normal_sid = inst._capture_text_chat(
        {"contexts": [_Msg("主人今天想聊什么？")]}, ()
    )
    after_normal = len(inst.context_snapshots)
    print(f"  普通请求：sid={normal_sid} 快照 {before} → {after_normal}")
    if normal_sid is None or after_normal != before + 1:
        bad.append("用例2：普通请求必须照常抓快照")

    internal_sid = inst._capture_text_chat(
        {"contexts": [_Msg(f"{marker}\n下面是一个网页的正文。请只提取…")]}, ()
    )
    after_internal = len(inst.context_snapshots)
    print(f"  带标记的总结请求：sid={internal_sid} 快照 {after_normal} → {after_internal}")
    if internal_sid is not None or after_internal != after_normal:
        bad.append(
            "★用例2：带标记的内部请求被抓成了会话快照 —— "
            "面板右栏会凭空多出一坨网页正文"
        )

    # 不误伤：标记出现在正文中间（不是开头）时，仍应照常抓取。
    inst._capture_text_chat({"contexts": [_Msg(f"用户的原话里提到了 {marker} 这个词")]}, ())
    after_middle = len(inst.context_snapshots)
    print(f"  标记出现在正文中间的普通消息：快照 {after_internal} → {after_middle}（应 +1）")
    if after_middle != after_internal + 1:
        bad.append("用例2：误伤了正文中间含标记的普通消息（判据应收紧到开头）")

    # 复位检查：排除逻辑不能是「一关就再也不抓」
    inst._capture_text_chat({"contexts": [_Msg("复位后的普通请求")]}, ())
    after_reset = len(inst.context_snapshots)
    print(f"  复位后：快照 {after_middle} → {after_reset}（应 +1）")
    if after_reset != after_middle + 1:
        bad.append("用例2：排除内部请求之后没有恢复抓取")
    print()

    # ── 契约：两个插件里的标记字面量必须一致 ────────────────────────
    print("===== 用例 3：跨插件标记的契约 =====")
    pattern = re.compile(r'^\s*(?:_INTERNAL_REQUEST_MARKER|_INTERNAL_REQUEST_MARKERS)\s*=\s*\(?"[^"]+"', re.M)
    tb_src = open(TOOLBOX_MAIN, encoding="utf-8").read()
    tb_marker = re.search(r'_INTERNAL_REQUEST_MARKER\s*=\s*"([^"]+)"', tb_src)
    got = tb_marker.group(1) if tb_marker else None
    print(f"  toolbox 定义 = {got!r}")
    print(f"  monitor 定义 = {marker!r}")
    if got != marker:
        bad.append(
            "★用例3：两个插件里的标记字面量不一致 —— "
            "网页总结会重新灌进面板（而且不会有任何报错）"
        )
    if not pattern.search(open(PLUGIN, encoding="utf-8").read()):
        bad.append("用例3：monitor 里找不到标记常量的定义")

    # ── ⑤ 两个压缩流并发：只能保证【结果】不走样 ──────────────────────
    # 审查说压缩流的 key 是"残留 umo"。我实测【推翻了前提】：把压缩事件和
    # 「请求诊断」（那里才 set _CURRENT_UMO）按时间排出来，压缩发生在它之后 63 秒
    # —— 压缩时拿到的是当轮的正确 umo。
    # 但"两个会话同时压缩"仍然存在：它们会共用同一块【实时缓冲】，面板上文字会
    # 混着显示。我试过给压缩流单独编 key，结果弄坏了 _live_clear() 的清理路径
    # （测试-压缩流式.py 用例 3 变红），所以退回了 —— 详见 _live_key 的说明。
    # 真正要守住的性质是下面这条：混流【不能影响压缩结果本身】。
    print("===== 用例 4：两个压缩并发时，压缩【结果】不能被串味 =====")

    class _Plain:
        def __init__(self, text):
            self.text = text

    class _Chain:
        def __init__(self, *parts):
            self.chain = list(parts)

    class _Chunk:
        def __init__(self, text=None, *, is_chunk=True):
            self.is_chunk = is_chunk
            self.result_chain = _Chain(_Plain(text)) if text else None
            self.completion_text = "" if is_chunk else (text or "")
            self.usage = None
            self.tools_call_name = None
            self.reasoning_content = None

    class _FakeCompressProvider:
        """照抄 openai_source 的形状：若干增量 + 一个完整结果。"""

        def __init__(self, text):
            self.text = text

        async def text_chat_stream(self, *args, **kwargs):
            for i in range(0, len(self.text), 2):
                yield _Chunk(self.text[i : i + 2])
            yield _Chunk(self.text, is_chunk=False)

    inst8 = cls(StubContext())
    results = {}

    async def one(tag, text):
        token = mod._COMPRESSING.set(True)
        try:
            resp = await inst8._run_compress_stream(
                _FakeCompressProvider(text), (), {}
            )
            results[tag] = getattr(resp, "completion_text", "") or ""
        finally:
            mod._COMPRESSING.reset(token)

    await asyncio.gather(one("A", "摘要甲甲甲"), one("B", "摘要乙乙乙"))
    print(f"  A 拿到的结果 = {results.get('A')!r}（应为 '摘要甲甲甲'）")
    print(f"  B 拿到的结果 = {results.get('B')!r}（应为 '摘要乙乙乙'）")
    if results.get("A") != "摘要甲甲甲":
        bad.append(f"★用例4：A 的压缩结果被 B 串味了，实际 {results.get('A')!r}")
    if results.get("B") != "摘要乙乙乙":
        bad.append(f"★用例4：B 的压缩结果被 A 串味了，实际 {results.get('B')!r}")
    print("  注：面板的实时缓冲在这种并发下会混着显示 —— 已知限制，只影响显示，")
    print("      把 max_concurrent_replies 设成 1 时压缩不会重叠；默认 0 时可能同屏")
    print()

    # ── ⑤ 压缩没生效必须被判定出来 ──────────────────────────────────
    # 实测踩过：总结那次 LLM 调用失败时，压缩器原样 return messages
    # （compressor.py:282），AstrBot 打出 "13597 -> 13597"，然后退化成减半截断。
    # 全程零报错，面板上原本只看得出"压缩了一次"—— 白烧一次完整 LLM 调用。
    print("===== 用例 5：压缩没生效必须被判出来并报警 =====")
    import logging

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            try:
                self.lines.append(record.getMessage())
            except Exception:  # noqa: BLE001
                pass

    cap = _Cap()
    mod.logger.addHandler(cap)
    old_level = mod.logger.level
    mod.logger.setLevel(logging.INFO)
    try:
        class _Msg2:
            def __init__(self, content):
                self.role = "user"
                self.content = content

        # ① 原样返回（= 总结调用失败）→ 没摘要
        inst._on_compressed(42, 13597, [_Msg2("随便一段历史")])
        r1 = inst.summaries[-1]
        print(f"  ① 原样返回: ineffective={r1.get('ineffective')} verdict={r1.get('verdict')!r}")
        if not r1.get("ineffective") or r1.get("verdict") != "no_summary":
            bad.append(
                f"★用例5：总结失败（压缩器原样返回）没被判出来，"
                f"实际 ineffective={r1.get('ineffective')} verdict={r1.get('verdict')!r}"
            )

        # ② 有摘要但没省下 token → 另一种失败
        inst._on_compressed(42, 0, [
            _Msg2(f"{mod.SUMMARY_PREFIX}这是一段摘要"),
            _Msg2("x" * 400),
        ])
        r2 = inst.summaries[-1]
        print(f"  ② 摘要存在但没省: ineffective={r2.get('ineffective')} verdict={r2.get('verdict')!r}")
        if not r2.get("ineffective") or r2.get("verdict") != "no_saving":
            bad.append(
                f"用例5：摘要生成了但没省 token，应判 no_saving，"
                f"实际 {r2.get('verdict')!r}"
            )

        # ③ 正常压缩 → 不能误报
        inst._on_compressed(42, 100000, [
            _Msg2(f"{mod.SUMMARY_PREFIX}一段摘要"),
            _Msg2("短"),
        ])
        r3 = inst.summaries[-1]
        print(f"  ③ 正常压缩: ineffective={r3.get('ineffective')} verdict={r3.get('verdict')!r} "
              f"省下={r3.get('saved_tokens')}")
        if r3.get("ineffective"):
            bad.append("用例5：正常压缩被误报成没生效（狼来了）")
        if r3.get("saved_tokens", 0) <= 0:
            bad.append(f"用例5：正常压缩应算出省下的 token，实际 {r3.get('saved_tokens')}")

        warns = [x for x in cap.lines if "没有生效" in x]
        print(f"  报警行数 = {len(warns)}（应为 2）")
        if len(warns) != 2:
            bad.append(
                f"用例5：应恰好报警 2 次（①②各一次），实际 {len(warns)} —— "
                "这类失败是静默的，日志里必须留下痕迹"
            )
        if warns:
            print(f"  报警样例: {warns[0][:100]}…")
    finally:
        mod.logger.removeHandler(cap)
        mod.logger.setLevel(old_level)
    print()

    print("===== 判定 =====")
    if bad:
        print("FAIL")
        for x in bad:
            print("  -", x)
        return 1
    print("PASS  补丁签名容错 / 兜底不为 0 / 内部请求不污染统计 / 跨插件标记一致")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
