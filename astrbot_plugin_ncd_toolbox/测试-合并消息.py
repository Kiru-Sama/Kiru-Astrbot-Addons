# -*- coding: utf-8 -*-
"""回归护栏：消息合并（同一用户 3 秒内连发 → 合成一条、只回一次）。

用户需求原话：
    「正常对话期间 3 秒内同一用户发的所有信息，插件拦截储存，
      等第一个消息的 3 秒过后合并发送给模型，发送完又收到这个用户信息开始新的 3 秒计时」
    「排队期间告诉用户正在排队，请稍等」

★ 2026-10-20 事故与修法（这段是本测试存在的原因）：
  旧实现挂在 OnLLMRequestEvent，但 AstrBot 在触发它之前就拿了**会话级锁**
  （internal.py: `async with session_lock_manager.acquire_lock(umo)`），
  第二条消息的整条管线会排队等第一条跑完（含出图几十秒），
  于是第二条永远进不了钩子 → 3 秒内连发只会各回各的。
  现在改挂 OnWaitingLLMRequestEvent（拿锁之前、可 stop_event），
  合并结果写回 event.message_str（build 请求时 req.prompt 读的就是它）。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-合并消息.py
"""

import asyncio
import sys
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

plugin_main.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    debug=lambda *a, **k: None,
)

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


class FakeEvent:
    def __init__(self, umo="t:GroupMessage:1", uid="1001", text="", images=None):
        self.unified_msg_origin = umo
        self._uid = uid
        self.message_str = text
        self._images = images or []
        self._extra: dict = {}
        self.stopped = False

    def get_sender_id(self):
        return self._uid

    def get_messages(self):
        return self._images

    def stop_event(self):
        self.stopped = True

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class FakeCtx:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append(umo)
        return None


def make_plugin(**conf):
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = dict(
        {
            "merge_enabled": True,
            "merge_window": 1,          # 测试用 1 秒，别真等 3 秒
            "merge_notice": True,
            "merge_notice_text": "排队中",
            "merge_max_messages": 10,
        },
        **conf,
    )
    inst._merge_buf = {}
    inst._merge_lock = asyncio.Lock()
    inst.context = FakeCtx()
    return inst


# =====================================================================
print("===== ① 队长等窗口，窗口内的第二条被合并进 message_str =====")


async def case1():
    inst = make_plugin()
    ev_a = FakeEvent("g:1", "1001", "第一句")
    ev_b = FakeEvent("g:1", "1001", "第二句")

    task_a = asyncio.create_task(inst._merge_waiting(ev_a))
    await asyncio.sleep(0.15)          # 让 A 先当上队长并开始睡
    await inst._merge_waiting(ev_b)
    await task_a
    return inst, ev_a, ev_b


inst, ev_a, ev_b = asyncio.run(case1())
print(f"  队长 message_str = {ev_a.message_str!r}")
print(f"  第二条 stopped={ev_b.stopped}  提示={inst.context.sent}")
check("①", ev_a.message_str == "第一句\n第二句", f"应合并成两句，实际 {ev_a.message_str!r}")
check("①", ev_b.stopped, "★ 窗口内的第二条没有被拦下（会单独回一条）")
check("①", ev_b.message_str == "第二句", "被拦下的那条不该被改动")
check("①", inst.context.sent == ["g:1"], f"应提示一次排队，实际 {inst.context.sent}")
check("①", not inst._merge_buf, "合并完应释放缓冲")
check("①", ev_a.get_extra("ncd_merged_text") == "第一句\n第二句", "没给 LLM 阶段留兜底标记")

# =====================================================================
print("\n===== ② 提示只发一次 =====")


async def case2():
    inst = make_plugin()
    ev1 = FakeEvent("g:2", "1001", "A")
    task = asyncio.create_task(inst._merge_waiting(ev1))
    await asyncio.sleep(0.1)
    for text in ("B", "C", "D"):
        await inst._merge_waiting(FakeEvent("g:2", "1001", text))
    await task
    return inst, ev1


inst, ev1 = asyncio.run(case2())
print(f"  合并结果 = {ev1.message_str!r}  提示次数={len(inst.context.sent)}")
check("②", ev1.message_str == "A\nB\nC\nD", f"应合并四条，实际 {ev1.message_str!r}")
check("②", len(inst.context.sent) == 1, f"★ 提示发了 {len(inst.context.sent)} 次（只该一次）")

# =====================================================================
print("\n===== ③ 窗口结束后重新计时（不串到上一轮）=====")


async def case3():
    inst = make_plugin()
    ev1 = FakeEvent("g:3", "1001", "第一轮")
    await inst._merge_waiting(ev1)
    ev2 = FakeEvent("g:3", "1001", "第二轮")
    await inst._merge_waiting(ev2)      # 缓冲已清空 → 自己当队长
    return ev1, ev2


ev1, ev2 = asyncio.run(case3())
print(f"  第一轮={ev1.message_str!r}  第二轮={ev2.message_str!r}")
check("③", ev1.message_str == "第一轮", f"第一轮不该被改：{ev1.message_str!r}")
check("③", ev2.message_str == "第二轮", f"第二轮不该带上上一轮：{ev2.message_str!r}")

# =====================================================================
print("\n===== ④ 指令 / 带图消息不参与合并 =====")


async def case4():
    inst = make_plugin()
    ev_cmd = FakeEvent("g:4", "1001", "/reset")
    await inst._merge_waiting(ev_cmd)
    ev_img = FakeEvent("g:4", "1001", "看图", images=[object()])
    await inst._merge_waiting(ev_img)
    ev_empty = FakeEvent("g:4", "1001", "   ")
    await inst._merge_waiting(ev_empty)
    return inst, ev_cmd, ev_img, ev_empty


inst, ev_cmd, ev_img, ev_empty = asyncio.run(case4())
print(f"  指令={ev_cmd.message_str!r} 带图={ev_img.message_str!r} 缓冲={inst._merge_buf}")
check("④", ev_cmd.message_str == "/reset" and ev_img.message_str == "看图", "指令/带图被改动了")
check("④", not inst._merge_buf, "指令/带图/空消息不该进合并缓冲")

# =====================================================================
print("\n===== ⑤ 不同用户互不干扰 =====")


async def case5():
    inst = make_plugin()
    ev_a = FakeEvent("g:5", "1001", "甲")
    ev_b = FakeEvent("g:5", "2002", "乙")
    await asyncio.gather(inst._merge_waiting(ev_a), inst._merge_waiting(ev_b))
    return ev_a, ev_b


ev_a, ev_b = asyncio.run(case5())
print(f"  甲={ev_a.message_str!r}  乙={ev_b.message_str!r}")
check("⑤", ev_a.message_str == "甲" and ev_b.message_str == "乙", "★ 不同用户被合并到一起了")

# =====================================================================
print("\n===== ⑥ 关掉开关就完全不介入 =====")


async def case6():
    inst = make_plugin(merge_enabled=False)
    ev1 = FakeEvent("g:6", "1001", "A")
    ev2 = FakeEvent("g:6", "1001", "B")
    await inst._merge_waiting(ev1)
    await inst._merge_waiting(ev2)
    return inst, ev1, ev2


inst, ev1, ev2 = asyncio.run(case6())
print(f"  A={ev1.message_str!r} B={ev2.message_str!r}  stopped={ev2.stopped}")
check("⑥", ev1.message_str == "A" and ev2.message_str == "B", "关掉后不该合并")
check("⑥", not ev2.stopped, "★ 关掉后仍在拦消息")
check("⑥", not inst.context.sent, "关掉后不该发排队提示")

# =====================================================================
print("\n===== ⑦ LLM 阶段兜底：req.prompt 被别处改掉也能补回 =====")


async def case7():
    inst = make_plugin()
    ev = FakeEvent("g:7", "1001", "合并后的文本")
    ev.set_extra("ncd_merged_text", "合并后的文本")
    req = types.SimpleNamespace(prompt="被改坏的文本", system_prompt="", func_tool=None)
    await inst._tune_request(ev, req)
    return req


req = asyncio.run(case7())
print(f"  req.prompt = {req.prompt!r}")
check("⑦", req.prompt == "合并后的文本", "兜底没把合并文本补回 req.prompt")

# =====================================================================
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 合并两条 / 拦下后不单独回 / 只提示一次 / 重新计时 / 指令与带图不合并 / 用户隔离 / 可关闭 / LLM 兜底")

