# -*- coding: utf-8 -*-
"""回归护栏：两个会话交错时，「请求 ↔ 响应」不能配错人。

真实症状（2026-10-12 17:36 实测）：
    面板上那条群里的【主动回复】没有「主动」标记、来源只剩群号没有群名，
    耗时还显示 10.1 秒。而 10.1 秒正好等于
        「A 的响应时刻 − B 的请求时刻」
    —— 说明它配到了另一个会话的请求记录。

根因：`_pending` 原本是【单槽】。两个会话交错时后一条覆盖前一条：

    17:35:45  群 A 请求 → _pending = A（群名、主动=True）
    17:35:54  私聊 B 请求 → _pending = B          ← A 的信息被覆盖
    17:36:04  A 的响应 → p = B（别人的）→ 群名丢了、「主动」丢了、耗时算成 10.1s
    17:35:54  B 的响应 → p = None → 另一条字段全空

修法：`_pending` 改成按 umo 分桶，并用 `_CURRENT_UMO` 这个 ContextVar
让 Provider 包装也知道"这次调用属于哪个会话"。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-会话交错配对.py
"""
import asyncio
import importlib.util
import os
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")

UMO_G = "demo-bot:GroupMessage:200000001"
UMO_P = "demo-bot:FriendMessage:100000001"
SELF_ID = "100000000"


class StubContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


class FakeEvent:
    def __init__(self, umo, nickname, user_id, raw=None):
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = False
        self.message_obj = SimpleNamespace(
            raw_message=raw if raw is not None else {"message": []},
            sender=SimpleNamespace(nickname=nickname, user_id=user_id),
            self_id=SELF_ID,
            group_id=umo.rsplit(":", 1)[-1] if "GroupMessage" in umo else "",
        )

    def get_messages(self):
        return []

    def get_self_id(self):
        return SELF_ID

    def get_message_str(self):
        return "测试消息"


class FakeReq:
    def __init__(self, prompt="测试消息"):
        self.contexts = [{"role": "user", "content": prompt}]
        self.system_prompt = "测试人设"
        self.prompt = prompt
        self.func_tool = None


class FakeUsage:
    input = 1234
    output = 56
    input_cached = 0


class FakeResp:
    completion_text = "测试回复"
    reasoning_content = None
    tools_call_name = None
    usage = FakeUsage()


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_interleave_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_interleave_probe"] = mod
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
    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    inst = cls(StubContext())
    # 绕过身份库：直接塞群名
    inst._group_names = {"200000001": "示例群聊"}
    inst._group_names_loaded = True

    bad: list[str] = []

    ev_g = FakeEvent(UMO_G, "示例群友A", "300000001")
    ev_p = FakeEvent(UMO_P, "示例用户", "100000001")

    # ══════════ 交错：先群（主动回复），再私聊 ══════════
    print("===== 两个会话交错发请求 =====")
    await inst.on_llm_request(ev_g, FakeReq("群里没人叫我但我插话"))
    await inst.on_llm_request(ev_p, FakeReq("私聊"))
    print(f"  待配对记录 = {list(inst._pending)}")
    if set(inst._pending) != {UMO_G, UMO_P}:
        bad.append(f"两条请求应该各留一条，实际 {list(inst._pending)}")

    # ★ 负对照：旧逻辑取的是"最后写入的那条"，此刻它是【私聊】那条。
    #   这一条断言保证测试真的能失败 —— 不是恒真的摆设。
    if len(inst._pending) >= 2:
        old_logic = list(inst._pending.values())[-1]
        print("\n----- 负对照：旧逻辑（取最后写入那条）会配成谁 -----")
        print(f"  旧逻辑拿到 umo       = {old_logic['umo']}")
        print(f"  旧逻辑拿到 群名      = {old_logic['group_name']!r}")
        print(f"  旧逻辑拿到 主动标记  = {old_logic['is_active_reply']}")
        if old_logic["umo"] == UMO_G:
            bad.append("负对照失效：旧逻辑居然也能拿对，说明这条测试测不出东西")
        if old_logic["group_name"] or old_logic["is_active_reply"]:
            bad.append("负对照失效：旧逻辑那条不该有群名/主动标记")

    # ══════════ 把两条的时间改成可区分，验证耗时不会算成别人的 ══════════
    now = time.time()
    inst._pending[UMO_G]["time"] = now - 20.0   # 群这条已经发了 20 秒
    inst._pending[UMO_P]["time"] = now - 5.0    # 私聊这条才 5 秒

    # ══════════ 群的响应先回来 ══════════
    print("\n===== 群的响应回来 =====")
    await inst.on_llm_response(ev_g, FakeResp())
    rec_g = inst.llm_calls[-1]
    print(f"  umo            = {rec_g['umo']}")
    print(f"  group_name     = {rec_g['group_name']!r}")
    print(f"  group_id       = {rec_g['group_id']!r}")
    print(f"  is_active_reply= {rec_g['is_active_reply']}")
    print(f"  latency_ms     = {rec_g['latency_ms']}")
    if rec_g["umo"] != UMO_G:
        bad.append(f"群记录 umo 错了：{rec_g['umo']}")
    if rec_g["group_name"] != "示例群聊":
        bad.append(f"群记录丢了群名：{rec_g['group_name']!r}")
    if rec_g["group_id"] != "200000001":
        bad.append(f"群记录 group_id 错了：{rec_g['group_id']!r}")
    if rec_g["is_active_reply"] is not True:
        bad.append(f"群记录丢了「主动」标记：{rec_g['is_active_reply']}")
    if not rec_g["latency_ms"] or rec_g["latency_ms"] < 15000:
        bad.append(f"群记录耗时算成了别人的：{rec_g['latency_ms']}ms（应约 20000）")

    # ══════════ 私聊的响应再回来 ══════════
    print("\n===== 私聊的响应回来 =====")
    await inst.on_llm_response(ev_p, FakeResp())
    rec_p = inst.llm_calls[-1]
    print(f"  umo            = {rec_p['umo']}")
    print(f"  is_active_reply= {rec_p['is_active_reply']}")
    print(f"  latency_ms     = {rec_p['latency_ms']}")
    if rec_p["umo"] != UMO_P:
        bad.append(f"私聊记录 umo 错了：{rec_p['umo']}")
    if rec_p["is_active_reply"] is not False:
        bad.append("私聊记录不该是主动回复")
    if rec_p["group_name"]:
        bad.append(f"私聊记录不该有群名：{rec_p['group_name']!r}")
    if not rec_p["latency_ms"] or rec_p["latency_ms"] >= 15000:
        bad.append(f"私聊记录耗时不对：{rec_p['latency_ms']}ms（应约 5000）")

    print(f"\n  两条记录都落盘 = {len(inst.llm_calls) == 2}")
    print(f"  待配对已清空   = {not inst._pending}")
    if len(inst.llm_calls) != 2:
        bad.append("应该产出两条记录")
    if inst._pending:
        bad.append(f"配对完应该清空，还剩 {list(inst._pending)}")

    # ══════════ 请求发了但响应没回来 → 不能无限堆积 ══════════
    print("\n===== 过期记录会被清掉 =====")
    await inst.on_llm_request(ev_g, FakeReq("没人回我"))
    inst._pending[UMO_G]["time"] = time.time() - 3600
    await inst.on_llm_request(ev_p, FakeReq("新请求"))
    print(f"  再过一轮后待配对 = {list(inst._pending)}")
    if UMO_G in inst._pending:
        bad.append("超时的待配对记录没有被清掉（会无限堆积）")

    if bad:
        print("\n失败项:")
        for item in bad:
            print(f"  ✗ {item}")
        return 1
    print("\nPASS: 交错会话各配各的，群名/主动标记/耗时都归属正确")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
