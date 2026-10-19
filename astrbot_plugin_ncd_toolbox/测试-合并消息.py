# -*- coding: utf-8 -*-
"""回归护栏：消息合并（同一用户 3 秒内连发 → 合成一条、只回一次）。

用户 2026-10-19 的需求，原话：
    「正常对话期间 3 秒内同一用户发的所有信息，插件拦截储存，
      等第一个消息的 3 秒过后合并发送给模型，发送完又收到这个用户信息开始新的 3 秒计时」
    并且「排队期间告诉用户正在排队，请稍等」。

本测试用假 event / 假 req 直接驱动 on_llm_request 钩子（不改 AstrBot 源码）：
  1. 第一条（队长）会等满窗口，然后带着合并后的文本交给模型
  2. 窗口内的第二条被拦下（stop_event），不单独回，并提示一次「排队」
  3. 提示只发一次（第三条不再刷）
  4. 窗口结束后再来一条 → 新的一轮，不带上上一轮的内容
  5. 指令（/ 开头）和带图消息不参与合并
  6. 不同用户互不干扰

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-合并消息.py
"""

import asyncio
import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
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
    def __init__(self, umo="t:GroupMessage:1", uid="1001"):
        self.unified_msg_origin = umo
        self._uid = uid
        self.stopped = False

    def get_sender_id(self):
        return self._uid

    def stop_event(self):
        self.stopped = True


class FakeReq:
    def __init__(self, prompt, images=None):
        self.prompt = prompt
        self.image_urls = images or []


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
print("===== ① 队长等窗口，窗口内的第二条被合并 =====")


async def case1():
    inst = make_plugin()
    ev_a, req_a = FakeEvent("g:1", "1001"), FakeReq("第一句")
    ev_b, req_b = FakeEvent("g:1", "1001"), FakeReq("第二句")

    task_a = asyncio.create_task(inst._merge_incoming(ev_a, req_a))
    await asyncio.sleep(0.15)          # 让 A 先当上队长并开始睡
    await inst._merge_incoming(ev_b, req_b)
    await task_a
    return inst, ev_a, req_a, ev_b, req_b


inst, ev_a, req_a, ev_b, req_b = asyncio.run(case1())
print(f"  队长 prompt = {req_a.prompt!r}")
print(f"  第二条 stopped={ev_b.stopped}  提示={inst.context.sent}")
check("①", req_a.prompt == "第一句\n第二句", f"应合并成两句，实际 {req_a.prompt!r}")
check("①", ev_b.stopped, "★ 窗口内的第二条没有被拦下（会单独回一条）")
check("①", req_b.prompt == "第二句", "被拦下的那条不该被改动")
check("①", inst.context.sent == ["g:1"], f"应提示一次排队，实际 {inst.context.sent}")
check("①", not inst._merge_buf, "合并完应释放缓冲")

# =====================================================================
print("\n===== ② 提示只发一次 =====")


async def case2():
    inst = make_plugin()
    ev1, req1 = FakeEvent("g:2", "1001"), FakeReq("A")
    task = asyncio.create_task(inst._merge_incoming(ev1, req1))
    await asyncio.sleep(0.1)
    for text in ("B", "C", "D"):
        await inst._merge_incoming(FakeEvent("g:2", "1001"), FakeReq(text))
    await task
    return inst, req1


inst, req1 = asyncio.run(case2())
print(f"  合并结果 = {req1.prompt!r}  提示次数={len(inst.context.sent)}")
check("②", req1.prompt == "A\nB\nC\nD", f"应合并四条，实际 {req1.prompt!r}")
check("②", len(inst.context.sent) == 1, f"★ 提示发了 {len(inst.context.sent)} 次（只该一次）")

# =====================================================================
print("\n===== ③ 窗口结束后重新计时（不串到上一轮）=====")


async def case3():
    inst = make_plugin()
    ev1, req1 = FakeEvent("g:3", "1001"), FakeReq("第一轮")
    await inst._merge_incoming(ev1, req1)
    ev2, req2 = FakeEvent("g:3", "1001"), FakeReq("第二轮")
    await inst._merge_incoming(ev2, req2)   # 此时缓冲已清空 → 自己当队长
    return req1, req2


req1, req2 = asyncio.run(case3())
print(f"  第一轮={req1.prompt!r}  第二轮={req2.prompt!r}")
check("③", req1.prompt == "第一轮", f"第一轮不该被改：{req1.prompt!r}")
check("③", req2.prompt == "第二轮", f"第二轮不该带上上一轮：{req2.prompt!r}")

# =====================================================================
print("\n===== ④ 指令 / 带图消息不参与合并 =====")


async def case4():
    inst = make_plugin()
    ev_cmd, req_cmd = FakeEvent("g:4", "1001"), FakeReq("/reset")
    await inst._merge_incoming(ev_cmd, req_cmd)
    ev_img, req_img = FakeEvent("g:4", "1001"), FakeReq("看图", images=["a.png"])
    await inst._merge_incoming(ev_img, req_img)
    return inst, req_cmd, req_img


inst, req_cmd, req_img = asyncio.run(case4())
print(f"  指令 prompt={req_cmd.prompt!r}  带图 prompt={req_img.prompt!r}  缓冲={inst._merge_buf}")
check("④", req_cmd.prompt == "/reset" and req_img.prompt == "看图", "指令/带图被改动了")
check("④", not inst._merge_buf, "指令/带图不该进合并缓冲")

# =====================================================================
print("\n===== ⑤ 不同用户互不干扰 =====")


async def case5():
    inst = make_plugin()
    ev_a, req_a = FakeEvent("g:5", "1001"), FakeReq("甲")
    ev_b, req_b = FakeEvent("g:5", "2002"), FakeReq("乙")
    await asyncio.gather(
        inst._merge_incoming(ev_a, req_a),
        inst._merge_incoming(ev_b, req_b),
    )
    return req_a, req_b


req_a, req_b = asyncio.run(case5())
print(f"  甲={req_a.prompt!r}  乙={req_b.prompt!r}")
check("⑤", req_a.prompt == "甲" and req_b.prompt == "乙", "★ 不同用户被合并到一起了")

# =====================================================================
print("\n===== ⑥ 关掉开关就完全不介入 =====")


async def case6():
    inst = make_plugin(merge_enabled=False)
    ev1, req1 = FakeEvent("g:6", "1001"), FakeReq("A")
    ev2, req2 = FakeEvent("g:6", "1001"), FakeReq("B")
    await inst._merge_incoming(ev1, req1)
    await inst._merge_incoming(ev2, req2)
    return inst, req1, req2, ev2


inst, req1, req2, ev2 = asyncio.run(case6())
print(f"  A={req1.prompt!r} B={req2.prompt!r}  stopped={ev2.stopped}")
check("⑥", req1.prompt == "A" and req2.prompt == "B", "关掉后不该合并")
check("⑥", not ev2.stopped, "★ 关掉后仍在拦消息")
check("⑥", not inst.context.sent, "关掉后不该发排队提示")

# =====================================================================
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 合并两条 / 拦下后不单独回 / 只提示一次 / 重新计时 / 指令与带图不合并 / 用户隔离 / 可关闭")
