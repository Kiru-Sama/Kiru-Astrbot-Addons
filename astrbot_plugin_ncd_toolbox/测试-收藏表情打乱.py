"""自测：收藏表情的打乱顺序。

不联网、不需要 SnowLuma —— 用一个假的 OneBot 后端喂造好的收藏列表，
但仍然调**真的工具方法**（list_stickers / preview 用不到的 send_sticker），
所以验的是插件的实际行为，不是把逻辑重抄一遍。

核心要证明的三件事：
  1. 同一条消息内，翻页 / 发送用的必须是同一份顺序（否则序号会指向别的表情）；
  2. 换了新消息（新 message_id）才换一份新排列，且仍是同一批、不重不漏；
  3. 关闭开关时，一切回到接口给的原始顺序。

用法：<AstrBot>\\.venv\\Scripts\\python.exe 测试-收藏表情打乱.py
"""

import asyncio
import pathlib
import re
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

import main as plugin_main  # noqa: E402
import stickers as stickers_mod  # noqa: E402
from stickers import collection_key, shuffled  # noqa: E402

FAILURES: list[str] = []


class _SilentLogger:
    """这个测试要一口气「发」上百张表情，stickers 的每张一条日志会把结果冲掉。"""

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


stickers_mod.logger = _SilentLogger()


def check(ok: bool, label: str, detail: str = "") -> None:
    if ok:
        print(f"  OK  {label}")
    else:
        print(f"  ✗   {label} {detail}")
        FAILURES.append(f"{label} {detail}".strip())


def make_items(count: int = 55, reverse: bool = False) -> list[dict]:
    # emoji_id 的形态照抄真机（<UIN>_0_0_0_<MD5>_0_0），但 UIN 用通用示例值
    items = [
        {
            "emoji_id": f"100000000_0_0_0_{i:032X}_0_0",
            "md5": f"{i:032X}",
            "url": "",
            "desc": f"表情{i}",
        }
        for i in range(1, count + 1)
    ]
    return list(reversed(items)) if reverse else items


class FakeBot:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.sent: list[dict] = []

    async def call_action(self, action: str, **params):
        if action == "fetch_custom_face_detail":
            return list(self.items)
        if action == "send_custom_face":
            self.sent.append(params)
            return {"message_id": 12345}
        raise AssertionError(f"意外的 action: {action}")


class FakeEvent:
    def __init__(self, bot: FakeBot, message_id: str, umo: str = "ncd:f-1:1", group: str = "100000001") -> None:
        self.bot = bot
        self._message_id = message_id
        self.umo = umo
        self.unified_msg_origin = umo
        self._group = group
        self.message_obj = type("Obj", (), {"message_id": message_id})()

    def get_group_id(self) -> str:
        return self._group

    def get_sender_id(self) -> str:
        return "100000002"


def new_plugin(shuffle: bool, page_size: int = 15, **overrides):
    """绕过 __init__（它需要 AstrBot 的运行时 context），只装本测试用到的状态。"""
    plugin = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    plugin._conf = {"sticker_shuffle": shuffle, "sticker_page_size": page_size, **overrides}
    plugin._sticker_orders = {}
    return plugin


async def order_by_sending(plugin, event: FakeEvent, count: int) -> list[str]:
    """黑盒取顺序：按序号 1..count 逐个「发送」，收集实际发出去的 emoji_id。

    这样拿到的顺序就是模型视角看到的那一份 —— 如果插件内部换了排列，
    这里立刻就能看出来。
    """
    event.bot.sent.clear()
    for index in range(1, count + 1):
        await plugin.send_sticker(event, index=index)
    return [params["emoji_id"] for params in event.bot.sent]


def page_indexes(rendered: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"^\[(\d+)\]", rendered, re.M)]


def id_at(items: list[dict], index: int) -> str:
    return str(items[index - 1]["emoji_id"])


# ----------------------------------------------------------------------


async def case_shuffle_off() -> None:
    print("\n--- 关闭开关：必须就是接口给的原始顺序")
    raw = make_items()
    event = FakeEvent(FakeBot(raw), message_id="AAA")
    plugin = new_plugin(shuffle=False)

    listed = await plugin.list_stickers(event, page=1)
    check(page_indexes(listed) == list(range(1, 16)), "第 1 页序号是 1..15", listed[:80])

    sent = await order_by_sending(plugin, event, len(raw))
    check(sent == [item["emoji_id"] for item in raw], "序号映射 = 原始顺序")


async def case_same_message_stable() -> None:
    print("\n--- 打开开关 + 同一条消息：翻页与发送必须是同一份顺序")
    raw = make_items()
    event = FakeEvent(FakeBot(raw), message_id="AAA")
    plugin = new_plugin(shuffle=True)

    page1 = await plugin.list_stickers(event, page=1)
    page2 = await plugin.list_stickers(event, page=2)
    idx1, idx2 = page_indexes(page1), page_indexes(page2)

    check(idx1 == list(range(1, 16)), "第 1 页序号 1..15", str(idx1))
    check(idx2 == list(range(16, 31)), "第 2 页序号 16..30", str(idx2))

    sent = await order_by_sending(plugin, event, len(raw))
    check(len(sent) == len(set(sent)), "55 张不重不漏", f"unique={len(set(sent))}")
    check(set(sent) == {item["emoji_id"] for item in raw}, "打乱后仍是同一批表情")
    check(sent != [item["emoji_id"] for item in raw], "顺序确实被打乱了")

    # 翻页拿到的序号，指的必须就是打乱后的那个位置
    check(sent[0] != raw[0]["emoji_id"], "1 号已经不是原来那张")
    check(sent[14] != raw[14]["emoji_id"], "15 号已经不是原来那张")

    # 再列一次第 1 页，顺序不能变（这是「翻页不是重新打乱」的直接证据）
    again = await plugin.list_stickers(event, page=1)
    check(page_indexes(again) == idx1, "重复列第 1 页，序号不变")

    # 全量翻页覆盖：4 页（55 张 / 每页 15）合起来正好是 1..55
    merged: list[int] = []
    for page in range(1, 5):
        merged += page_indexes(await plugin.list_stickers(event, page=page))
    check(merged == list(range(1, 56)), "4 页合起来覆盖 1..55 无重复", str(merged))


async def case_reopen_reshuffles() -> None:
    print("\n--- 重新从第 1 页打开：换一份新排列")
    raw = make_items()
    bot = FakeBot(raw)
    plugin = new_plugin(shuffle=True)

    first = FakeEvent(bot, message_id="AAA")
    second = FakeEvent(bot, message_id="BBB")

    # ★ 必须先 list_stickers —— 换排列是它的职责；send 只沿用（下面单独验这一点）
    await plugin.list_stickers(first, page=1)
    order_a = await order_by_sending(plugin, first, len(raw))

    await plugin.list_stickers(second, page=1)
    order_b = await order_by_sending(plugin, second, len(raw))

    check(order_a != order_b, "两次重新打开的排列不同")
    check(set(order_a) == set(order_b), "换排列后表情集合不变")

    # 回到同一条消息 ID：排列应当能原样复现（种子决定排列，没有随机残留）
    again = FakeEvent(bot, message_id="AAA")
    await plugin.list_stickers(again, page=1)
    check(await order_by_sending(plugin, again, len(raw)) == order_a, "同一条消息 ID 复现同一排列")


async def case_paging_across_messages_keeps_order() -> None:
    """★ 这条守的是「翻页不影响」：换消息只是接着翻页时，绝不能重排。

    重排的后果不是报错，而是第 1 页和第 2 页来自两份排列 ——
    互相重叠又互相漏，模型记着的序号也会指向别的表情。
    """
    print("\n--- 换了消息但只是翻页：必须沿用，不能重排")
    raw = make_items()
    bot = FakeBot(raw)
    plugin = new_plugin(shuffle=True)

    first = FakeEvent(bot, message_id="AAA")
    await plugin.list_stickers(first, page=1)
    order_a = await order_by_sending(plugin, first, len(raw))

    later = FakeEvent(bot, message_id="BBB")  # 新消息，但只是要看第 2 页
    page2 = await plugin.list_stickers(later, page=2)
    check(page_indexes(page2) == list(range(16, 31)), "第 2 页序号仍是 16..30", str(page_indexes(page2)))
    check(
        await order_by_sending(plugin, later, len(raw)) == order_a,
        "跨消息翻页后顺序没变（两页来自同一份排列）",
    )

    # 同一份排列下，第 1、2 页拼起来必须正好是 1..30 且不重复
    page1_again = await plugin.list_stickers(later, page=1)
    merged = page_indexes(page1_again) + page_indexes(page2)
    check(merged == list(range(1, 31)), "第 1+2 页拼起来 = 1..30 无重复", str(merged))

    # 但在新消息里重新从第 1 页看，就该换一批
    reopened = FakeEvent(bot, message_id="CCC")
    await plugin.list_stickers(reopened, page=1)
    check(
        await order_by_sending(plugin, reopened, len(raw)) != order_a,
        "新消息里重新从第 1 页看 → 换了一批",
    )


async def case_preview_send_never_reshuffle() -> None:
    print("\n--- preview/send 即使换了消息 ID，也必须沿用模型看过的那份")
    raw = make_items()
    bot = FakeBot(raw)
    plugin = new_plugin(shuffle=True)

    listed = FakeEvent(bot, message_id="AAA")
    await plugin.list_stickers(listed, page=1)
    listed_order = await order_by_sending(plugin, listed, len(raw))

    # 模拟「模型在下一轮里发送」：事件的消息 ID 变了，但没重新 list
    later = FakeEvent(bot, message_id="BBB")
    later_order = await order_by_sending(plugin, later, len(raw))
    check(later_order == listed_order, "未先 list 就发送时，沿用上一份顺序而不是新排列")


async def case_base_order_independent() -> None:
    print("\n--- 接口返回顺序变了，打乱结果必须不变（否则序号会错位）")
    forward = make_items()
    backward = make_items(reverse=True)

    plugin_a = new_plugin(shuffle=True)
    plugin_b = new_plugin(shuffle=True)
    event_a = FakeEvent(FakeBot(forward), message_id="AAA")
    event_b = FakeEvent(FakeBot(backward), message_id="AAA")

    order_a = await order_by_sending(plugin_a, event_a, len(forward))
    order_b = await order_by_sending(plugin_b, event_b, len(backward))
    check(order_a == order_b, "接口给正序/倒序，算出的排列一致")
    check(
        collection_key(forward) == collection_key(backward),
        "收藏集指纹与顺序无关",
    )


async def case_collection_changed() -> None:
    print("\n--- 收藏集变了：重建排列，不报错")
    raw = make_items(10)
    bot = FakeBot(raw)
    plugin = new_plugin(shuffle=True)
    event = FakeEvent(bot, message_id="AAA")

    await plugin.list_stickers(event, page=1)

    bot.items = make_items(12)  # 新收藏了两张
    listed = await plugin.list_stickers(event, page=1)
    check(page_indexes(listed) == list(range(1, 13)), "多两张后第 1 页变成 1..12", str(page_indexes(listed)))

    order = await order_by_sending(plugin, event, 12)
    check(set(order) == {item["emoji_id"] for item in bot.items}, "新收藏的也进了排列")
    check(len(order) == 12, "12 张全部可取", str(len(order)))


async def case_empty_and_no_message_id() -> None:
    print("\n--- 边界：空收藏 / 拿不到消息 ID")
    empty_plugin = new_plugin(shuffle=True)
    empty_event = FakeEvent(FakeBot([]), message_id="AAA")
    text = await empty_plugin.list_stickers(empty_event, page=1)
    check("空" in text, "空收藏返回可读说明", text)

    raw = make_items()
    plugin = new_plugin(shuffle=True)
    event = FakeEvent(FakeBot(raw), message_id="")
    listed = await plugin.list_stickers(event, page=1)
    check(page_indexes(listed) == list(range(1, 16)), "没有消息 ID 时退化为原始顺序")

    sent = await order_by_sending(plugin, event, 5)
    check(sent == [item["emoji_id"] for item in raw[:5]], "没有消息 ID 时序号 = 原始顺序")


async def case_order_cache_eviction() -> None:
    """★ 顺序缓存溢出时，只能踢【最旧】的那份，绝不能清空。

    clear() 的后果不是"效率低一点"，而是：别的会话正在翻的页会被连根拔掉，
    它下一次翻页拿到的是「新排列的第 N 页」—— 第 1 页和第 2 页来自两份排列。
    触发者还是【另一个会话】，本会话完全无辜。
    """
    print("\n--- 顺序缓存溢出：踢最旧，不清空")
    raw = make_items(20)
    bot = FakeBot(raw)
    plugin = new_plugin(shuffle=True)
    limit = plugin_main.MAX_STICKER_ORDERS

    sessions = []
    for i in range(limit):
        ev = FakeEvent(bot, message_id=f"M{i}", umo=f"umo-{i}")
        await plugin.list_stickers(ev, page=1)
        sessions.append(ev)
    check(
        len(plugin._sticker_orders) == limit,
        f"填满 {limit} 个会话",
        str(len(plugin._sticker_orders)),
    )

    recent = sessions[-1]
    await plugin.list_stickers(recent, page=1)      # 再用一次 → 排到队尾
    oldest = sessions[0]

    extra = FakeEvent(bot, message_id="NEW", umo="umo-new")
    await plugin.list_stickers(extra, page=1)       # 溢出，触发淘汰

    check(len(plugin._sticker_orders) <= limit, "上限仍被守住", str(len(plugin._sticker_orders)))
    check(oldest.umo not in plugin._sticker_orders, "最旧的那份被淘汰")
    check(
        recent.umo in plugin._sticker_orders,
        "★最近用过的会话仍然在 —— 这正是 clear() 会毁掉的东西",
    )

    # 负面对照：同样场景换成旧实现（clear），最近用过的也会没掉。
    # 没有这条，上面那句断言就有可能是空转。
    plugin2 = new_plugin(shuffle=True)
    for i in range(limit):
        await plugin2.list_stickers(
            FakeEvent(bot, message_id=f"N{i}", umo=f"umo2-{i}"), page=1
        )
    recent2 = FakeEvent(bot, message_id="N-recent", umo="umo2-recent")
    await plugin2.list_stickers(recent2, page=1)
    plugin2._sticker_orders.clear()                       # ← 旧实现
    plugin2._sticker_orders["umo2-new"] = ("seed", "fp", [])
    check(
        recent2.umo not in plugin2._sticker_orders,
        "负面对照：clear() 确实会把最近用过的也清掉（证明上一条不是空转）",
    )


async def case_hint_is_truthful() -> None:
    print("\n--- 给模型的那句「顺序是打乱的」必须只在真打乱时说")
    raw = make_items()
    on = new_plugin(shuffle=True)
    off = new_plugin(shuffle=False)
    ev_on = FakeEvent(FakeBot(raw), message_id="AAA")
    ev_off = FakeEvent(FakeBot(raw), message_id="AAA")

    text_on = await on.list_stickers(ev_on, page=1)
    text_off = await off.list_stickers(ev_off, page=1)

    check("打乱" in text_on and "翻页不会重排" in text_on, "开了打乱：列表里明确告诉模型")
    header_off = text_off.splitlines()[0]
    check("打乱" not in text_off, "没开打乱：一个字都不能提", header_off)

    # 真实性：既然说了是打乱的，序号映射就必须真的不等于原始顺序
    sent_on = await order_by_sending(on, ev_on, len(raw))
    check(
        sent_on != [item["emoji_id"] for item in raw],
        "说了打乱时，序号映射确实变了（不是嘴上说说）",
    )

    # 退化场景同样不能说：拿不到消息 ID、空收藏，都退回原始顺序
    no_id = new_plugin(shuffle=True)
    text_no_id = await no_id.list_stickers(FakeEvent(FakeBot(raw), message_id=""), page=1)
    check("打乱" not in text_no_id, "拿不到消息 ID（实际没打乱）时也不能说")

    empty = new_plugin(shuffle=True)
    text_empty = await empty.list_stickers(FakeEvent(FakeBot([]), message_id="AAA"), page=1)
    check("打乱" not in text_empty, "空收藏时也不能说")


async def case_seed_purity() -> None:
    print("\n--- 纯函数层：同种子同结果、异种子异结果")
    items = make_items(20)
    check(
        [i["emoji_id"] for i in shuffled(items, "x")] == [i["emoji_id"] for i in shuffled(items, "x")],
        "同种子两次调用结果一致",
    )
    check(
        [i["emoji_id"] for i in shuffled(items, "x")] != [i["emoji_id"] for i in shuffled(items, "y")],
        "不同种子结果不同",
    )
    check(len(shuffled(items, "x")) == 20, "打乱不丢条目")
    # 负面对照：如果 shuffled 其实是「原样返回」，上面「顺序确实被打乱了」必须失败
    check(
        [i["emoji_id"] for i in shuffled(items, "x")] != [i["emoji_id"] for i in items],
        "打乱确实改变了顺序（否则前面的断言都是空转）",
    )


async def main() -> int:
    await case_shuffle_off()
    await case_same_message_stable()
    await case_reopen_reshuffles()
    await case_paging_across_messages_keeps_order()
    await case_preview_send_never_reshuffle()
    await case_base_order_independent()
    await case_collection_changed()
    await case_empty_and_no_message_id()
    await case_order_cache_eviction()
    await case_hint_is_truthful()
    await case_seed_purity()

    if FAILURES:
        print("\n失败项:")
        for item in FAILURES:
            print(f"  ✗ {item}")
        return 1
    print("\nPASS: 打乱顺序的稳定性、翻页一致性、换消息重排、边界与退化全部正常")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
