# -*- coding: utf-8 -*-
"""回归护栏：插件重载后，运行时包装必须把数据写进【当前活着的实例】。

背景（这是一个真实修复过的 bug，别再让它回来）：
    三个运行时包装写在【类】上，闭包却捕获了【实例】。
    star_manager.reload() 的流程是 terminate() → 清 star_map → load()，
    而 load() 会【新建实例】。类上的包装函数还在、闭包还指着旧实例，
    标记又只写不删 → 新实例看到标记就跳过包装。
    结果：新实例注册了 6 条路由，但它自己的 context_snapshots 永远是空的
    —— 面板右栏「发给模型的完整上下文」和「压缩摘要」静默失效，
    零报错，只有重启 AstrBot 进程能恢复。
    而 README / 安装说明推荐的"点刷新图标重载"恰好走这条路。

修法：包装通过模块级 _ACTIVE 派发（不捕获实例）+ terminate() 里还原包装。

★ 为什么这个测试【不能】靠"检查闭包捕获了谁"来判断：
    修好之后闭包【根本不捕获实例】了，那种检查会找不到东西、
    于是把"没捕获"误判成"已修复" —— 也就是假通过。
    第一版就是这么写的，实际测了个寂寞。
    所以这里改成【行为验证】：真的调一次包装函数，
    看快照落进了哪个实例。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-重载失效.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
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


class DummyProvider:
    """冒充 provider 实例：只为让包装函数能跑起来。

    包装拿到它之后会：
      ① 先调 plugin._capture_text_chat(...)  ← 这一步就会写快照，是我们要观察的
      ② 再调 original(self_, ...)            ← 这一步必然失败（没有真 provider）
    失败没关系，①已经发生了。
    """


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_reload_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_reload_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


async def poke_provider_wrapper() -> None:
    """真的调一次被包装的 Provider.text_chat，让 _capture_text_chat 跑起来。

    必然会抛异常（DummyProvider 不是真 provider），这里吞掉 ——
    我们只关心"快照落进了谁"。
    """
    from astrbot.core.provider.provider import Provider

    wrapper = Provider.__dict__["text_chat"]
    try:
        await wrapper(
            DummyProvider(),
            system_prompt="回归测试的系统提示",
            contexts=[],
            prompt="回归测试",
        )
    except Exception:  # noqa: BLE001
        pass


_NAMES = ["A", "B", "C", "D"]


async def poke_and_see(*instances):
    """调一次包装，返回【本次新增】过快照的实例名。

    ★ 必须看"增量"而不是"是否为非空"：实例一旦收到过快照，
      context_snapshots 就一直非空，用非空判断会把上一轮的旧实例
      也算成"这次收到"（第一版就是这个错，报了个假的 FAIL）。
      这和 README 里 terminate() 那个"判据必须是增量而不是总数"是同一个教训。
    """
    before = [len(inst.context_snapshots) for inst in instances]
    await poke_provider_wrapper()
    grew = [
        name
        for name, inst, b in zip(_NAMES, instances, before)
        if len(inst.context_snapshots) > b
    ]
    return grew


async def main() -> int:
    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    from astrbot.core.agent.context.compressor import LLMSummaryCompressor
    from astrbot.core.agent.context.token_counter import EstimateTokenCounter
    from astrbot.core.provider.provider import Provider

    watches = [
        (Provider, "text_chat", "_ncd_monitor_patched"),
        (Provider, "text_chat_stream", "_ncd_stream_patched"),
        (LLMSummaryCompressor, "__call__", "_ncd_monitor_patched"),
        (EstimateTokenCounter, "count_tokens", "_ncd_monitor_patched"),
    ]

    def flags() -> dict:
        return {
            f"{c.__name__}.{attr}": c.__dict__.get(marker)
            for c, attr, marker in watches
        }

    bad = []
    print("插件类:", cls.__name__)
    print()

    # ── 场景 1：正常首次加载 ────────────────────────────────────────
    print("===== 场景 1：首次加载（实例 A）=====")
    a = cls(StubContext())
    print(f"  A._patched_provider_names = {a._patched_provider_names}")
    print(f"  类上标记 = {flags()}")
    if not a._patched_provider_names:
        bad.append("场景1：首次加载就应该包装 Provider（名字列表为空）")

    got = await poke_and_see(a)
    print(f"  调一次包装后，新增快照的实例 = {got}")
    if got != ["A"]:
        bad.append(f"场景1：新增快照应落进 A，实际 {got}")
    print()

    # ── 场景 2：直接新建实例（没走 terminate）—— 旧版就是在这里挂的 ──
    # 这条模拟"类上标记还在、包装还是旧的"那种状态。
    # _ACTIVE 派发必须保证数据仍然流向新实例。
    print("===== 场景 2：没有 terminate，直接新建实例 B =====")
    b = cls(StubContext())
    print(f"  B._patched_provider_names = {b._patched_provider_names}（因为标记还在，跳过包装）")
    got = await poke_and_see(a, b)
    print(f"  调一次包装后，新增快照的实例 = {got}")
    if got != ["B"]:
        bad.append(
            f"★核心回归：新增快照应只落进 B（当前活实例），实际 {got} —— "
            f"包装又绑死在旧实例上了"
        )
    print()

    # ── 场景 3：正常重载（terminate → 新建）────────────────────────
    print("===== 场景 3：正常重载（走 terminate，再建实例 C）=====")
    await b.terminate()
    print(f"  terminate() 之后类上标记 = {flags()}")
    if any(v for v in flags().values()):
        bad.append("场景3：terminate() 之后标记应全部清掉（" + str(flags()) + "）")
    for c_, attr, _marker in watches:
        fn = c_.__dict__.get(attr)
        # 我们的包装不是 async generator 就是普通函数，名字一定是 patched / patched_stream
        if fn is not None and getattr(fn, "__name__", "").startswith("patched"):
            bad.append(f"场景3：{c_.__name__}.{attr} 仍是我们的包装，没有还原")

    c = cls(StubContext())
    print(f"  C._patched_provider_names = {c._patched_provider_names}")
    print(f"  C._patched_stream_names   = {c._patched_stream_names}")
    if not c._patched_provider_names:
        bad.append(
            "场景3：terminate() 还原后，新实例应重新包装 Provider，"
            "但名字列表为空"
        )
    if not c._patched_stream_names:
        bad.append("场景3：terminate() 还原后，text_chat_stream 也应被重新包装")
    got = await poke_and_see(a, b, c)
    print(f"  调一次包装后，新增快照的实例 = {got}")
    if got != ["C"]:
        bad.append(f"场景3：新增快照应只落进 C，实际 {got}")
    print()

    # ── 收尾：别把包装留在类上（本进程内干净退出）──────────────────
    await c.terminate()

    print("===== 判定 =====")
    if bad:
        print("FAIL")
        for x in bad:
            print("  -", x)
        return 1
    print("PASS  重载后包装仍指向当前活实例；terminate() 能干净还原")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
