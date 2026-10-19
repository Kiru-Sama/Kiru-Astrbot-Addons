# -*- coding: utf-8 -*-
"""回归护栏：监控代码出错时，【不能】影响被监控的对话压缩。

背景（真实修复过，别再回来）：
    压缩器包装原本长这样：

        async def patched(self_, messages):
            before_tokens = plugin._estimate_messages_tokens(messages)   # ← try 之外
            before_count = len(messages)                                 # ← try 之外
            result = await original(self_, messages)                     # ← 真正的压缩
            try:
                plugin._on_compressed(...)
            except Exception: ...

    前置估算裸在 try 之外，于是它一抛异常，**真正的压缩 original() 根本不会被调用**，
    异常直接冒进 AstrBot 的压缩流程。
    性质是"监控代码把被监控对象搞挂了"，比"少一块数据"严重得多。
    （同一个文件里 Provider 包装的捕获是在 try 内的，两处保护级别不一致。）

★ 这个测试的关键手法：
    在插件加载【之前】把 LLMSummaryCompressor.__call__ 换成一个哨兵函数，
    于是包装把哨兵当成 original 闭包捕获。之后只要看哨兵有没有被调用，
    就知道"真正的压缩有没有被执行" —— 不用起真的压缩流程。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-压缩器不阻断.py
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


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_comp_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_comp_probe"] = mod
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
    from astrbot.core.agent.context.compressor import LLMSummaryCompressor

    # ── 先把 __call__ 换成哨兵，让插件把它当 original 包起来 ──────────
    calls = []

    async def sentinel(self_, messages):
        calls.append(list(messages))
        return "SENTINEL_RESULT"

    LLMSummaryCompressor.__call__ = sentinel

    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    bad = []
    inst = cls(StubContext())
    if "LLMSummaryCompressor" not in str(inst._patch_compressor.__doc__ or ""):
        pass  # 只是确认方法存在，不做断言

    wrapper = LLMSummaryCompressor.__dict__.get("__call__")
    if wrapper is sentinel:
        print("FAIL  压缩器没有被包装（wrapper 还是哨兵）")
        return 1
    print(f"压缩器已被包装: {getattr(wrapper, '__name__', wrapper)}")
    print()

    # ── 用例 1：估算函数抛异常，压缩必须照常执行 ────────────────────
    print("===== 用例 1：_estimate_messages_tokens 抛异常 =====")

    def boom(*_a, **_k):
        raise RuntimeError("模拟估算失败（比如 AstrBot 改了消息结构）")

    # 实例属性遮蔽掉 classmethod，让 wrapper 里那一行必然抛
    inst._estimate_messages_tokens = boom

    calls.clear()
    raised = None
    try:
        result = await wrapper(object(), ["m1", "m2"])
    except Exception as exc:  # noqa: BLE001
        raised = exc
        result = None

    print(f"  哨兵（=真正的压缩）被调用 {len(calls)} 次: {calls}")
    print(f"  异常是否冒出去: {type(raised).__name__ if raised else '否'}")
    print(f"  返回值: {result!r}")

    if raised is not None:
        bad.append(
            f"★核心回归：估算失败时异常冒出去了（{type(raised).__name__}: {raised}）"
            f" —— 监控代码把压缩搞挂了"
        )
    if calls != [["m1", "m2"]]:
        bad.append(
            f"★核心回归：真正的压缩没有被调用（哨兵调用次数 {len(calls)}，应为 1）"
            f" —— 压缩会被静默跳过"
        )
    if result != "SENTINEL_RESULT":
        bad.append(f"返回值应原样透传，实际 {result!r}")
    print()

    # ── 用例 2：估算正常时，摘要仍要记录（别把好路径改坏）────────────
    print("===== 用例 2：估算正常，摘要仍被记录 =====")
    inst._estimate_messages_tokens = lambda messages: 1234  # noqa: ARG005
    before_summaries = len(inst.summaries)
    calls.clear()
    result = await wrapper(object(), ["m1", "m2", "m3"])
    print(f"  哨兵调用 {len(calls)} 次，返回值 {result!r}")
    print(f"  摘要条数 {before_summaries} -> {len(inst.summaries)}")
    if calls != [["m1", "m2", "m3"]]:
        bad.append("用例2：正常路径下真正的压缩也没被调用")
    if len(inst.summaries) <= before_summaries:
        bad.append("用例2：正常路径下摘要没有被记录（修坏了正常功能）")
    print()

    await inst.terminate()

    print("===== 判定 =====")
    if bad:
        print("FAIL")
        for x in bad:
            print("  -", x)
        return 1
    print("PASS  估算失败不阻断压缩；估算正常时摘要照常记录")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
