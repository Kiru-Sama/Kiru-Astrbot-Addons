# -*- coding: utf-8 -*-
"""验证「各会话上下文占用」的聚合逻辑（左栏那张进度条卡片的数据源）。

为什么要单独测：
    这张卡是新加的，数据靠 `_session_usage()` 从 `context_snapshots` 里
    【按 umo 聚合最近一次请求】。聚合逻辑最容易出的三个错：
      1. 取成了最早一次、或者把同一会话的快照累加（上下文是滚动窗口，累加无意义）
      2. 排序方向反了 —— 这张卡的用处就是"谁快爆了排最前"
      3. max_ctx 为 0 时除零
    这些光看页面是看不出来的（页面上只是数字不对），所以在这里钉死。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-会话占用.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""
import asyncio
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")

MAX_CTX = 32768


class StubContext:
    """顶掉 register_web_api，让插件能在没有 AstrBot 运行时的情况下实例化。"""

    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_session_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_session_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


def snap(
    umo,
    tokens,
    t,
    *,
    gid="",
    gname="",
    sender="",
    msgs=10,
    chars=1000,
    inject=0,
):
    return {
        "umo": umo,
        "group_id": gid,
        "group_name": gname,
        "sender_name": sender,
        "message_count": msgs,
        "estimated_tokens": tokens,
        "inject_chars": inject,
        "total_chars": chars,
        "time": t,
    }


async def main() -> int:
    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    inst = cls(StubContext())
    # ★ 必须清空：__init__ 现在会把 plugin_data 里的真实上下文快照读回来
    #   （重载不清空那个功能），本机跑过真实对话时这里会带着真实会话进来，
    #   断言就会数出多余的一条。测试要的是自造的数据。
    inst.context_snapshots.clear()
    bad = []

    GROUP = "demo-bot:GroupMessage:200000001"
    FRIEND = "demo-bot:FriendMessage:100000001"

    # 同一会话两条：早的 1000 token、晚的 9000 token
    #   → 必须取【晚的】那条（取早的就说明拿反了方向）
    inst.context_snapshots.append(snap(GROUP, 1000, 100.0, gid="200000001", msgs=5))
    inst.context_snapshots.append(snap(GROUP, 9000, 200.0, gid="200000001", msgs=18))
    # 私聊，3000 token
    inst.context_snapshots.append(
        snap(FRIEND, 3000, 150.0, sender="示例用户", msgs=6)
    )
    # 没识别出会话的（umo 缺失）—— 不能崩，也不能把别人并进来
    inst.context_snapshots.append(snap("", 100, 50.0, sender="?"))

    rows = inst._session_usage(MAX_CTX)

    if len(rows) != 3:
        bad.append(f"会话数应为 3（群 / 私聊 / 未识别），实际 {len(rows)}")
    by_umo = {r["umo"]: r for r in rows}

    g = by_umo.get(GROUP)
    if g is None:
        bad.append("群里那个会话没出现")
    else:
        if g["tokens"] != 9000:
            bad.append(f"应取最近一次快照的 9000 token，实际 {g['tokens']}（取成早的了？）")
        if g["messages"] != 18:
            bad.append(f"消息数应取最近一次的 18，实际 {g['messages']}")
        if g["requests"] != 2:
            bad.append(f"累计请求数应为 2，实际 {g['requests']}")
        if not g["is_group"]:
            bad.append("群会话的 is_group 应为 True")
        if g["group_id"] != "200000001":
            bad.append(f"group_id 解析错：{g['group_id']}")
        want = round(9000 / MAX_CTX * 100, 1)
        if abs(g["percent"] - want) > 0.05:
            bad.append(f"percent 应为 {want}，实际 {g['percent']}")

    f = by_umo.get(FRIEND)
    if f is None:
        bad.append("私聊那个会话没出现")
    else:
        if f["is_group"]:
            bad.append("私聊的 is_group 应为 False")
        if f["sender_name"] != "示例用户":
            bad.append(f"私聊 sender_name 错：{f['sender_name']}")

    # umo 认不出来的那条要单独成一档，别混进私聊
    u = by_umo.get("")
    if u is None:
        bad.append("umo 为空的那条没出现（不能被静默丢掉）")
    elif u["label"] != "未识别会话":
        bad.append(f"空 umo 的 label 应为「未识别会话」，实际 {u['label']!r}")

    # 排序：9000 > 3000 > 100
    if rows:
        got = [r["tokens"] for r in rows]
        if got != sorted(got, reverse=True):
            bad.append(f"应按占用降序，实际顺序 token={got}")

    # max_ctx=0 不能除零
    try:
        zero = inst._session_usage(0)
        if any(r["percent"] != 0 for r in zero):
            bad.append("max_ctx=0 时 percent 应全为 0")
    except ZeroDivisionError:
        bad.append("max_ctx=0 时除零了")

    # 注入块的字数要折进 token（除以 4），否则那张卡会比实际偏低
    inst.context_snapshots.clear()
    inst.context_snapshots.append(snap(GROUP, 1000, 10.0, gid="200000001", inject=4000))
    got = inst._session_usage(MAX_CTX)[0]["tokens"]
    if got != 1000 + 4000 // 4:
        bad.append(f"注入块没折进 token：期望 2000，实际 {got}")

    if bad:
        print("FAIL")
        for b in bad:
            print("  -", b)
        return 1

    print("PASS  会话占用聚合 6 项断言都过")
    for r in rows:
        print(
            f"  {r['label'][:18]:20s} {r['messages']:>3} 条 "
            f"{r['tokens']:>6} tok {r['percent']:>5}% "
            f"{'群' if r['is_group'] else '私聊'} requests={r['requests']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
