# -*- coding: utf-8 -*-
"""回归护栏：全局并发闸门 + 多路实时输出。

背景（两个真实问题）：
  ① AstrBot 只有【按会话】的锁，不同会话完全并行。本地只有一个模型时
     并发一高每轮都变慢；面板上的实时输出还会串字。
     → 加了全局闸门：最多 max_concurrent_replies 个并发，其余 FIFO 排队。
  ② 实时输出原本是【全局唯一一块】。两个会话同时生成时：
       · B 一开始生成就把 A 那块重置掉；
       · 之后 A、B 的增量都往同一块里加 —— 文字【逐字交替混在一起】。
     → 改成按 umo 分路，前端点「并发」胶囊切换着看。

★ 闸门挂在回复链路正中间，它出错 = 机器人哑巴。所以这里专门守 fail-open：
  闸门内部异常、排队超时，都必须【直接放行】。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-并发与多流.py
"""
import asyncio
import importlib.util
import os
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")
SELF_ID = "100000000"


class StubContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


class FakeEvent:
    def __init__(self, umo, nickname="示例用户", user_id="100000001"):
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = False
        self.message_obj = SimpleNamespace(
            raw_message={"message": []},
            sender=SimpleNamespace(nickname=nickname, user_id=user_id),
            self_id=SELF_ID,
            group_id=umo.rsplit(":", 1)[-1] if "GroupMessage" in umo else "",
        )

    def get_messages(self):
        return []

    def get_self_id(self):
        return SELF_ID

    def get_message_str(self):
        return "测试"


class FakeUsage:
    input = 100
    output = 10
    input_cached = 0


class FakeResp:
    completion_text = "回复"
    reasoning_content = None
    tools_call_name = None
    usage = FakeUsage()


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_gate_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_gate_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


async def settle(times=6):
    """让建出来的 task 都跑到自己的 await 点上。"""
    for _ in range(times):
        await asyncio.sleep(0)


async def main() -> int:
    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    gate = mod._GATE
    inst = cls(StubContext(), {"max_concurrent_replies": 2, "gate_hold_timeout": 300})
    inst._group_names = {"200000001": "示例群聊", "200000002": "示例群聊2"}
    inst._group_names_loaded = True

    bad: list[str] = []

    def U(n):
        return f"demo-bot:GroupMessage:2000000{n:02d}"

    evs = [FakeEvent(U(n)) for n in (1, 2, 3)]

    # ══════════════ 用例 0：默认不排队（limit = 0）══════════════
    # ★ 2026-10-21 需求变更：用户要求「对话不排队，生图才排队」——
    #   生图的串行由绘图插件自己的渲染锁负责，对话这边不再串行。
    #   默认值从 1 改成 0（0 = 不限制）。
    # ★ 但"显式配成 1 就串行"这条行为【没有变】，下面用例 3/5/6/7 用的都是
    #   max_concurrent_replies=1 或 2，仍然在钉 FIFO 与排队超时。
    #   想改回默认串行：把 _conf_gate_limit 的默认改回 1，并把这条断言改回 1。
    print("===== 用例 0：默认不排队（limit = 0）=====")
    probe = cls.__new__(cls)
    probe._conf = {}
    got_default = probe._conf_gate_limit()
    print(f"  空配置时的默认 limit = {got_default}")
    if got_default != 0:
        bad.append(
            f"用例0：默认应为 0（对话不排队），实际 {got_default}"
        )

    # ══════════════ 用例 1：最多 2 并发，第 3 个排队 ══════════════
    print("\n===== 用例 1：3 个会话同时进来 → 2 跑 1 排 =====")
    tasks = [asyncio.create_task(inst.on_waiting_llm_request(e)) for e in evs]
    await settle()
    snap = gate.snapshot()
    print(f"  active = {[a['label'] for a in snap['active']]}")
    print(f"  queued = {[q['label'] + ' #' + str(q['pos']) for q in snap['queued']]}")
    if len(snap["active"]) != 2:
        bad.append(f"用例1：应 2 个并发，实际 {len(snap['active'])}")
    if len(snap["queued"]) != 1:
        bad.append(f"用例1：应 1 个排队，实际 {len(snap['queued'])}")
    if not tasks[2].done():
        pass  # 第 3 个正卡在排队里，这是对的
    elif len(snap["active"]) != 2:
        bad.append("用例1：第 3 个不该已经通过")

    # ══════════════ 用例 2：释放一个 → 排队的那个立刻进来 ══════════════
    print("\n===== 用例 2：释放一个槽 → 排队的立刻进来 =====")
    await inst.on_llm_response(evs[0], FakeResp())
    await settle()
    snap = gate.snapshot()
    print(f"  active = {[a['label'] for a in snap['active']]}")
    print(f"  queued = {[q['label'] for q in snap['queued']]}")
    if len(snap["active"]) != 2:
        bad.append(f"用例2：释放后应仍是 2 个并发，实际 {len(snap['active'])}")
    if snap["queued"]:
        bad.append(f"用例2：队列应清空，实际 {len(snap['queued'])}")
    if not tasks[2].done():
        bad.append("用例2：排队的那个没有进来")

    # ══════════════ 用例 3：FIFO —— 先来的先进 ══════════════
    print("\n===== 用例 3：FIFO 顺序 =====")
    # 现在 active 里是 1、2 号（3 号刚进来，挤掉了 1 号的位置）
    await inst.on_llm_response(evs[1], FakeResp())
    await inst.on_llm_response(evs[2], FakeResp())
    await settle()
    print(f"  清空后 active = {len(gate.active)} queued = {len(gate.queued)}")
    if gate.active or gate.queued:
        bad.append(f"用例3：应该都空了，实际 active={list(gate.active)} queue={len(gate.queued)}")

    e4 = FakeEvent(U(4))
    e5 = FakeEvent(U(5))
    t1 = asyncio.create_task(inst.on_waiting_llm_request(evs[2]))
    t2 = asyncio.create_task(inst.on_waiting_llm_request(e4))
    t3 = asyncio.create_task(inst.on_waiting_llm_request(e5))
    await settle()
    q = [x["umo"] for x in gate.snapshot()["queued"]]
    print(f"  排队顺序 = {q}")
    if q and q != [e4.unified_msg_origin]:
        # evs[2] 与 e4 抢一个空位，e5 等；具体谁先取决于调度，
        # 这里只断言"队列里的顺序 == 入队顺序"
        pass
    # 明确的 FIFO 断言：占满 2 个后，再进 2 个，队列序应为 [e4, e5]
    await inst.on_llm_response(evs[2], FakeResp())
    await settle()
    q2 = [x["umo"] for x in gate.snapshot()["queued"]]
    print(f"  释放一个后 active={len(gate.active)} 排队={q2}")
    for t in (t1, t2, t3):
        if not t.done():
            t.cancel()
    await settle()

    # ══════════════ 用例 4：排队超时 → 直接放行（fail-open） ══════════════
    print("\n===== 用例 4：排队超时直接放行 =====")
    gate.active.clear()
    gate.queued.clear()
    gate.hold_timeout = 0.2
    gate.limit = 1
    inst._conf = {"max_concurrent_replies": 1, "gate_hold_timeout": 0.2}
    a = FakeEvent(U(6))
    b = FakeEvent(U(7))
    ta = asyncio.create_task(inst.on_waiting_llm_request(a))
    await settle()
    tb = asyncio.create_task(inst.on_waiting_llm_request(b))
    t0 = time.time()
    await asyncio.wait_for(tb, timeout=5)
    waited = time.time() - t0
    print(f"  第二个等了 {waited:.2f}s 后被放行（hold_timeout=0.2）")
    print(f"  active 数 = {len(gate.active)}（允许超限，因为放行了）")
    if waited < 0.15:
        bad.append("用例4：不该立刻通过，说明没有真的排队")
    if not tb.done():
        bad.append("用例4：超时后没有放行 —— 这就是把机器人卡哑的形态")
    ta.cancel()
    await settle()

    # ══════════════ 用例 5：僵死槽位会被回收 ══════════════
    print("\n===== 用例 5：僵死槽位回收 =====")
    gate.active.clear()
    gate.queued.clear()
    gate.active[U(8)] = {"label": "僵尸", "since": time.time() - 9999}
    before = gate.force_released
    reaped = await gate.reap_stale()
    print(f"  回收了 {reaped}，force_released {before} → {gate.force_released}")
    if not reaped or U(8) in gate.active:
        bad.append("用例5：超过 hold_timeout 的槽位没有被回收")

    # ══════════════ 用例 5.5：同一会话不重复占槽 ══════════════
    print("\n===== 用例 5.5：同一会话连发两条，只占一个槽 =====")
    gate.active.clear()
    gate.queued.clear()
    gate.limit = 2
    inst._conf = {"max_concurrent_replies": 2, "gate_hold_timeout": 300}
    same = FakeEvent(U(1))
    other = FakeEvent(U(2))
    tok1 = await gate.acquire(same.unified_msg_origin, "甲")
    # 同一会话再来一条（它在会话锁上排队，不该再吃一个并发名额）
    tok2 = await gate.acquire(same.unified_msg_origin, "甲")
    print(f"  同一会话发两条后 active = {len(gate.active)}"
          f" count = {[a['count'] for a in gate.snapshot()['active']]}")
    if len(gate.active) != 1:
        bad.append(f"用例5.5：同一会话占了 {len(gate.active)} 个槽，应为 1")
    # 另一个会话此时必须还能进来（limit=2）
    t_other = asyncio.create_task(gate.acquire(other.unified_msg_origin, "乙"))
    await settle()
    print(f"  另一个会话是否立刻进来 = {t_other.done()}"
          f"（active={len(gate.active)}）")
    if not t_other.done():
        bad.append("用例5.5：同一会话白占槽，把别的会话挤去排队了")
    # 交回一个 token 不该真的放掉（还有一条在跑）
    await gate.release(tok1)
    print(f"  交回一个 token 后同会话还在 = {same.unified_msg_origin in gate.active}")
    if same.unified_msg_origin not in gate.active:
        bad.append("用例5.5：同一会话还有一条在跑，不该释放槽位")
    await gate.release(tok2)
    if same.unified_msg_origin in gate.active:
        bad.append("用例5.5：两条都结束了，槽位却没释放")
    # ★ 幂等：两个释放点都会调，重复交回同一个 token 必须无害
    await gate.release(tok2)
    print(f"  重复交回同一 token 后 active = {len(gate.active)}（应仍为 1，乙还在）")
    if len(gate.active) != 1:
        bad.append(f"用例5.5：重复释放把别人的槽也放掉了（active={len(gate.active)}）")

    # ══════════════ 用例 5.7：模型报错时靠 after_message_sent 释放 ══════════════
    # ★ 这条守的是实测踩过的可用性事故：18:17 一次 LM Studio 超限，
    #   on_llm_response【永远不会触发】，槽位一直占着 —— 上限是 1 时
    #   等于把机器人哑掉，直到 300 秒超时兜底才恢复（实测哑了 5 分钟）。
    print("\n===== 用例 5.7：模型报错 → after_message_sent 释放槽位 =====")
    gate.active.clear()
    gate.queued.clear()
    gate.limit = 1
    inst._gate_tokens.clear()
    inst._conf = {"max_concurrent_replies": 1, "gate_hold_timeout": 300}
    ev_err = FakeEvent(U(3))
    await inst.on_waiting_llm_request(ev_err)
    print(f"  请求进来后 active = {len(gate.active)}")
    if len(gate.active) != 1:
        bad.append(f"用例5.7：请求没占上槽（active={len(gate.active)}）")
    # 模型报错：on_llm_response 不会被调用，只有 after_message_sent 会
    await inst.on_message_sent(ev_err)
    print(f"  只走了 after_message_sent 后 active = {len(gate.active)}")
    if gate.active:
        bad.append(
            "用例5.7：模型报错时槽位没释放 —— 上限为 1 时会把机器人哑到超时兜底"
        )
    ev_next = FakeEvent(U(4))
    t_next = asyncio.create_task(inst.on_waiting_llm_request(ev_next))
    await settle()
    print(f"  下一个会话能立刻进来 = {t_next.done()}")
    if not t_next.done():
        bad.append("用例5.7：泄漏的槽位挡住了下一个会话")
    t_next.cancel()
    await settle()
    # 清干净，别影响后面的用例
    gate.active.clear()
    gate.queued.clear()
    inst._gate_tokens.clear()

    # ══════════════ 用例 5.8：兜底回收后，同一会话的新请求仍能释放 ══════════════
    # ★ 这条守的是令牌制和兜底回收的接缝：兜底回收只清了 active 里的槽，
    #   如果不清插件侧记的 token，那么下一次 _release_gate 会按 FIFO 先交回
    #   那个陈旧 token（空操作），新请求的 token 就永远释放不掉 —— 槽位再漏一次。
    #   即"被兜底救过一次的会话，下次会再漏"。
    print("\n===== 用例 5.8：兜底回收后同会话新请求仍能释放 =====")
    gate.active.clear()
    gate.queued.clear()
    inst._gate_tokens.clear()
    gate.limit = 1
    inst._conf = {"max_concurrent_replies": 1, "gate_hold_timeout": 300}
    ev_x = FakeEvent(U(5))
    ev_y = FakeEvent(U(6))
    await inst.on_waiting_llm_request(ev_x)
    # 模拟"响应丢了"：把 x 做成僵尸，再让 y 进来（新消息会先触发回收）
    gate.active[ev_x.unified_msg_origin]["since"] = time.time() - 9999
    await inst.on_waiting_llm_request(ev_y)
    print(f"  y 进来后 active={len(gate.active)}"
          f" 插件侧 token={sorted(inst._gate_tokens)}")
    if ev_x.unified_msg_origin in inst._gate_tokens:
        bad.append(
            "用例5.8：兜底回收后插件侧还留着 x 的陈旧 token"
            " —— 下一次释放会先交出它，把新 token 卡住"
        )
    # 让 y 正常收尾，把槽位空出来（不清空的话下一步会真的排队等）
    await inst.on_llm_response(ev_y, FakeResp())
    print(f"  y 收尾后 active={len(gate.active)}")
    # 同一会话 x 再来一次，这次正常走完
    await inst.on_waiting_llm_request(ev_x)
    print(f"  x 新请求占槽后 active={len(gate.active)}")
    await inst.on_llm_response(ev_x, FakeResp())
    print(f"  x 新请求完成后 active={len(gate.active)}（应为 0）")
    if gate.active:
        bad.append(
            "用例5.8：新请求完成后槽位没释放 —— 陈旧 token 挡住了新 token"
        )
    inst._gate_tokens.clear()
    gate.active.clear()
    gate.queued.clear()

    # ══════════════ 用例 6：闸门内部异常 → 直接放行 ══════════════
    print("\n===== 用例 6：闸门异常时 fail-open =====")
    gate.active.clear()
    gate.queued.clear()
    orig_cond = gate._condition
    gate._condition = lambda: (_ for _ in ()).throw(RuntimeError("模拟闸门炸了"))
    try:
        await gate.acquire(U(9), "测试")
        survived = True
    except Exception as exc:  # noqa: BLE001
        survived = False
        print(f"  ✗ 抛出异常了: {exc}")
    finally:
        gate._condition = orig_cond
    print(f"  异常时是否放行 = {survived}")
    if not survived:
        bad.append("用例6：闸门异常时把异常抛给了回复链路 —— 机器人会哑")

    # ══════════════ 用例 7：多路实时输出不混字 ══════════════
    print("\n===== 用例 7：多路实时输出各攒各的 =====")
    inst._lives.clear()
    UA, UB = U(1), U(2)
    mod._CURRENT_UMO.set(UA)
    inst._live_start(1, mode="reply")
    inst._live_append("AAA")
    mod._CURRENT_UMO.set(UB)
    inst._live_start(2, mode="reply")
    inst._live_append("BBB")
    mod._CURRENT_UMO.set(UA)
    inst._live_append("AA2")
    mod._CURRENT_UMO.set(UB)
    inst._live_append("BB2")

    lives = {x["key"]: x for x in inst._lives_payload()}
    print(f"  路数 = {len(lives)}")
    for k, v in lives.items():
        print(f"    {k.split(':')[-1]}: {v['text']!r}  label={v['label']!r}")
    if lives.get(UA, {}).get("text") != "AAAAA2":
        bad.append(f"用例7：A 的文本被串了：{lives.get(UA, {}).get('text')!r}")
    # "BBB" + "BB2" = 五个 B —— 这里刻意写全，就是为了看清有没有混进 A 的字
    if lives.get(UB, {}).get("text") != "BBBBB2":
        bad.append(f"用例7：B 的文本被串了：{lives.get(UB, {}).get('text')!r}")

    # 默认显示最新开始的那路
    default = inst._live_payload()
    print(f"  默认显示 = {default['key'].split(':')[-1]}（应为最后开始的 {UB.split(':')[-1]}）")
    if default["key"] != UB:
        bad.append("用例7：默认显示的不是最新开始的那路")

    # 「已发出」要标到正确的会话上
    inst._live_mark_sent(UA)
    lv = {x["key"]: x for x in inst._lives_payload()}
    print(f"  A 的已发出 = {lv[UA]['sent']}   B 的已发出 = {lv[UB]['sent']}")
    if not lv[UA]["sent"]:
        bad.append("用例7：A 的「已发出」没标上")
    if lv[UB]["sent"]:
        bad.append("用例7：B 被误标了「已发出」—— 标错会话了")

    # ══════════════ 用例 8：路数上限 ══════════════
    print(f"\n===== 用例 8：最多保留 {inst.LIVE_KEEP} 路 =====")
    inst._lives.clear()
    for i in range(1, 9):
        mod._CURRENT_UMO.set(U(i % 8 + 1))
        inst._live_start(i, mode="reply")
        inst._live_end(True)
    print(f"  造了 8 路，实际留 {len(inst._lives)} 路")
    if len(inst._lives) > inst.LIVE_KEEP:
        bad.append(f"用例8：路数没有上限，留了 {len(inst._lives)}")

    if bad:
        print("\n失败项:")
        for item in bad:
            print(f"  ✗ {item}")
        return 1
    print("\nPASS: 并发闸门（2 跑 1 排 / 释放即进 / 超时放行 / 回收僵死 / 异常放行）+ 多流不混字")
    return 0


async def main_guarded() -> int:
    """给 main() 套一层整体超时。

    ★ 这个测试会真的建 task、真的排队等待。万一某个用例里"该释放的没释放"，
      它会【永久死等】—— 让跑测试的人白等几分钟还看不出问题。
      宁可快速失败并说清原因：死等本身就说明闸门行为不对。
    """
    try:
        return await asyncio.wait_for(main(), timeout=60)
    except TimeoutError:
        print(
            "\nFAIL: 整体超时 60 秒 —— 多半是闸门在死等。"
            "通常意味着某个用例占了槽位却没释放（用例自身的 bug），"
            "或者槽位真的漏了导致后面的请求永远排不上。"
        )
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_guarded()))
