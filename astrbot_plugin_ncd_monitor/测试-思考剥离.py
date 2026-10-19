# -*- coding: utf-8 -*-
"""回归护栏：思考过程（reasoning）不该跨轮堆在上下文里。

背景（2026-10-14 实测）：
    推理型模型开了「思考」之后工具调用顺畅很多，但思考会以 ThinkPart 的形式
    写进 run_context.messages —— 而那份列表正是【压缩决策的输入】+【最后落盘的内容】。
    实测库里一条会话攒了 10 段思考、合计 13673 字，全都在 8192 的上下文里反复重发。

    治本点只能在"压缩决策之前"，因为供应商包装（Provider.text_chat）拿到请求时
    压缩早就判完了。

★ 策略 A（本文件守的就是它）：
    只在【每圈对话开始时】剥一次历史里的思考，**同一圈内新产生的思考保留** ——
    模型在多轮工具调用之间还能看到自己刚才的推理（对 4B 的工具连贯性重要）。

    注意 AstrBot 的层级：
        step_until_done()   ← 一圈对话跑一次（工具循环在这）
          └── step()        ← 一个 LLM 轮次跑一次（压缩决策在这）
    挂错层就变成"每次 LLM 调用都剥"＝策略 B，模型轮内也会失忆。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-思考剥离.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""

import asyncio
import pathlib
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from astrbot.core.agent.message import (  # noqa: E402
    AssistantMessageSegment,
    Message,
    TextPart,
    ThinkPart,
    ToolCall,
    ToolCallMessageSegment,
)

import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


def make_plugin(keep: bool):
    """绕过 __init__，只放配置和计数器进去，专注测剥离逻辑。

    ★ 计数器要显式补上：本插件的约定是计数在 __init__ 里初始化
      （_on_compressed 也是这样自增的），绕开 __init__ 就得自己给。
    """
    inst = plugin_main.Main.__new__(plugin_main.Main)
    inst._conf = {"keep_reasoning_in_context": keep}
    inst.reasoning_stripped_segments = 0
    inst.reasoning_stripped_chars = 0
    return inst


def unpatch(cls) -> None:
    """清掉类上的包装标记，让下一次 _patch_runner() 能重新包装。

    ★ 用 delattr，不能用 cls.__dict__.pop() —— 类的 __dict__ 是只读
      mappingproxy（main.py 的 _unwrap_all 注释里记着这个坑，我第一次写测试时
      还是踩了：'mappingproxy' object has no attribute 'pop'）。
    """
    if "_ncd_monitor_patched" in cls.__dict__:
        delattr(cls, "_ncd_monitor_patched")


def tool_call(cid: str = "call_1") -> ToolCall:
    return ToolCall(id=cid, function=ToolCall.FunctionBody(name="list_stickers", arguments="{}"))


def history() -> list:
    """造一段真实形状的历史：用户问 → 助手（带思考+回复）→ 助手（带思考+工具调用）→ 工具结果。"""
    return [
        Message(role="user", content=[TextPart(text="发个表情包")]),
        AssistantMessageSegment(
            content=[ThinkPart(think="用户想要表情，我该先列出来。"), TextPart(text="好嘞～")]
        ),
        AssistantMessageSegment(
            content=[ThinkPart(think="先调 list_stickers 看看有哪些。")],
            tool_calls=[tool_call()],
        ),
        ToolCallMessageSegment(tool_call_id="call_1", content="收藏表情共 82 张…"),
        AssistantMessageSegment(content=[ThinkPart(think="挑 8 号，先预览。")]),
    ]


# =====================================================================
#  用例 1：剥离本身 —— 该删的删、该留的一个字不能动
# =====================================================================
print("===== 用例 1：_strip_think_parts 的剥离面 =====")
msgs = history()
before_len = len(msgs)
removed = plugin_main._strip_think_parts(msgs)

print(f"  摘掉 {removed} 段思考，消息 {before_len} → {len(msgs)} 条")
for m in msgs:
    print(f"    {m.role:9s} content={m.content!r} tool_calls={bool(getattr(m, 'tool_calls', None))}")
check("用例1", removed == 3, f"应摘掉 3 段思考，实际 {removed}")
check("用例1", len(msgs) == before_len - 1, "最后那条只剩思考的消息应整条丢掉")

# 正文必须原样活着（剥思考不能碰正文）
check("用例1", msgs[0].content[0].text == "发个表情包", "用户消息被动了")
check("用例1", msgs[1].content[0].text == "好嘞～", "助手正文被误删")
check("用例1", not any(isinstance(p, ThinkPart) for m in msgs if isinstance(m.content, list)
                       for p in m.content), "还有 ThinkPart 残留")

# 带工具调用的助手消息：思考没了，但【工具调用必须留着】——否则工具链断掉
check("用例1", msgs[2].tool_calls and msgs[2].tool_calls[0].id == "call_1",
      "★工具调用被误删（工具链会断）")

# 工具结果消息不能动
check("用例1", msgs[3].content == "收藏表情共 82 张…", "工具结果被动了")

# =====================================================================
#  用例 2：开关关掉时，一个字都不该动
# =====================================================================
print("\n===== 用例 2：开关（keep_reasoning_in_context）=====")
off = make_plugin(True)
msgs_off = history()
n_off = off._strip_history_reasoning(msgs_off)
print(f"  keep=True  → 摘掉 {n_off} 段")
check("用例2", n_off == 0, "开关开着（保留思考）时不该动任何东西")
check("用例2", sum(1 for m in msgs_off if isinstance(m.content, list)
                   for p in m.content if isinstance(p, ThinkPart)) == 3, "保留模式下思考被动了")

on = make_plugin(False)
msgs_on = history()
n_on = on._strip_history_reasoning(msgs_on)
print(f"  keep=False → 摘掉 {n_on} 段")
check("用例2", n_on == 3, "关掉（不保留思考）时应把 3 段都摘掉")

# =====================================================================
#  用例 3：包装 step() —— 剥在 original 之前，且整圈只剥一次
# =====================================================================
# ★ 这里曾经挂错地方，是本用例放过的：第一版包装的是 step_until_done()，
#   而生产路径走的是 astr_agent_run_util.run_agent()，它自己 while 循环调
#   agent_runner.step()。于是生产里一次都没剥离，测试却全绿 —— 因为测试
#   自己直接调了 step_until_done。教训：测试必须驱动【真实入口】。
print("\n===== 用例 3：step() 包装（策略 A）=====")


class FakeRunner:
    """假 runner：只带 run_context.messages，并把 step() 模拟成"再产生一段思考"。"""

    def __init__(self, messages):
        self.run_context = type("RC", (), {"messages": messages})()

    async def step(self):
        """模拟一个 LLM 轮次：记录入口处还剩几段思考，然后追加新思考。"""
        self.run_context.messages.append(
            AssistantMessageSegment(content=[ThinkPart(think="这一轮新想的")])
        )
        yield "resp"


def think_count(messages) -> int:
    return sum(
        1 for m in messages if isinstance(m.content, list)
        for p in m.content if isinstance(p, ThinkPart)
    )


async def collect(gen):
    return [x async for x in gen]


# ★ 关键手法（照抄 测试-压缩器不阻断.py）：先把类方法换成哨兵，
#   包装就会把哨兵当成 original 捕获。之后看哨兵看到的状态即可。
from astrbot.core.agent.runners.tool_loop_agent_runner import (  # noqa: E402
    ToolLoopAgentRunner,
)

cls = ToolLoopAgentRunner


class RoundSpy:
    """充当"真的 step"：每轮入口先记下还剩几段思考，再追加一段新的。

    这样就能同时看出两件事：
      · 第一轮入口该是 0 段（历史被剥了）
      · 第二轮入口该是 1 段（本轮新产生的【没被】剥）—— 策略 A
    """

    def __init__(self):
        self.seen: list[int] = []

    def __call__(self, self_, *a, **kw):
        self.seen.append(think_count(self_.run_context.messages))
        self_.run_context.messages.append(
            AssistantMessageSegment(content=[ThinkPart(think="这一轮新想的")])
        )

        async def gen():
            yield "resp"

        return gen()


plugin_on = make_plugin(False)
plugin_off = make_plugin(True)

# --- 3a：开关关掉（保留思考）→ 包装不该剥
spy_off = RoundSpy()
unpatch(cls)
cls.step = spy_off  # type: ignore[assignment]
plugin_main._ACTIVE = plugin_off
plugin_off._patch_runner()
runner_off = FakeRunner(history())
asyncio.run(collect(cls.step(runner_off)))
asyncio.run(collect(cls.step(runner_off)))
print(f"  保留模式：各轮 step 入口处剩余思考段数 = {spy_off.seen}")
check("用例3a", spy_off.seen == [3, 4],
      f"保留模式下不该动思考（应 [3, 4]），实际 {spy_off.seen}")

# --- 3b：开关打开（不保留）→ 第一轮入口 0 段；第二轮【必须】看到本轮新产生的
spy_on = RoundSpy()
unpatch(cls)
cls.step = spy_on  # type: ignore[assignment]
plugin_main._ACTIVE = plugin_on
plugin_on._patch_runner()
runner_on = FakeRunner(history())
asyncio.run(collect(cls.step(runner_on)))
asyncio.run(collect(cls.step(runner_on)))
print(f"  剥离模式：各轮 step 入口处剩余思考段数 = {spy_on.seen}")
check("用例3b", spy_on.seen[:1] == [0],
      f"★第一轮入口就该是 0 段（历史被剥干净），实际 {spy_on.seen}")
check("用例3b", spy_on.seen[1:2] == [1],
      f"★策略 A 被破坏：第二轮把【本轮新产生的】思考也剥了（应 [0, 1]），实际 {spy_on.seen}")

# --- 3c：下一圈对话（新 runner）必须【重新】剥 —— 标记不能跨圈黏住
runner_new_turn = FakeRunner(history())
asyncio.run(collect(cls.step(runner_new_turn)))
print(f"  新一圈（新 runner）第一轮入口 = {spy_on.seen[2:3]}")
check("用例3c", spy_on.seen[2:3] == [0],
      "★标记跨圈黏住了：新一圈没重新剥（历史思考会一直留着）")

# =====================================================================
#  用例 4：监控出问题不能拖垮对话（fail-open）
# =====================================================================
print("\n===== 用例 4：剥离抛异常时，agent 必须照常跑 =====")
unpatch(cls)


class ExplodingRunner:
    """取 run_context 就炸 —— 模拟监控代码在"别人的数据"上失手。

    ★ 不继承 FakeRunner：那个 __init__ 要给 run_context 赋值，
      而这里它是会抛异常的只读 property，继承会先炸在构造上。
    """

    @property
    def run_context(self):
        raise RuntimeError("模拟取消息列表就炸")


plugin_bad = make_plugin(False)
ran = []


async def boom_step():
    ran.append(1)
    yield "resp"


# 先把原方法换成哨兵，包装才会把哨兵当成 original 捕获
cls.step = lambda self_, *a, **kw: boom_step()  # type: ignore[assignment]
unpatch(cls)
plugin_bad._patch_runner()
plugin_main._ACTIVE = plugin_bad
res = asyncio.run(collect(cls.step(ExplodingRunner())))
check("用例4", ran == [1], "★监控抛异常把 agent 的 step 挡住了（真的会挂对话）")
check("用例4", res == ["resp"], "异常路径下 yield 的内容没透传出去")

# 复原，免得影响同一个进程里的后续
unpatch(cls)
plugin_main._ACTIVE = None

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 思考剥离的剥离面 / 开关 / 策略A（只剥历史、保住轮内）/ fail-open 全部正确")
