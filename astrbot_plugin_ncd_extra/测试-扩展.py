# -*- coding: utf-8 -*-
"""回归护栏：链路扩展的四个命令（压缩 / 工具气泡 / token 用量 / 时间注入）。

为什么要有这个测试：
  · /compact 会真的改写对话历史数据库 —— 写错一次就是把用户上下文毁了，
    而它平时的调用路径（发消息 /compact）很难复现，必须离线钉住。
  · /tooldisplay 和 /get time 是「自己实现」而不是改 AstrBot 配置（原生值在
    pipeline 初始化时就被拷走了，改运行时配置不生效）。这条约束很容易被后人
    "顺手改回改配置"，所以这里同时钉住：默认关闭时一个字都不许发。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-扩展.py
"""

import asyncio
import json
import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    mark = "✅" if cond else "❌"
    print(f"{mark} {label}")
    if not cond:
        failures.append(f"{label}: {msg}")


# --------------------------------------------------------------------------- #
# 假件
# --------------------------------------------------------------------------- #
class FakeProvider:
    def __init__(self, text: str | None = "摘要正文：用户在做 AstrBot 插件。"):
        self.text = text
        self.calls: list[tuple] = []
        self.provider_config = {"model": "fake-model"}

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        self.calls.append((prompt, system_prompt))
        if self.text is None:
            raise RuntimeError("provider 炸了")
        return types.SimpleNamespace(completion_text=self.text)


class FakeConversation:
    def __init__(self, history, token_usage: int = 0):
        self.history = (
            json.dumps(history, ensure_ascii=False) if isinstance(history, list) else history
        )
        self.token_usage = token_usage


class FakeManager:
    def __init__(self, conversation):
        self.conversation = conversation
        self.written = None
        self.written_token_usage = "未调用"

    async def get_curr_conversation_id(self, umo):
        return "cid-abcdef12" if self.conversation else None

    async def get_conversation(self, umo, cid):
        return self.conversation

    async def update_conversation(
        self, umo, cid=None, history=None, token_usage=None, **kwargs
    ):
        self.written = history
        self.written_token_usage = token_usage


class FakeContext:
    def __init__(self, manager=None, provider=None, cfg=None, tz="Asia/Shanghai"):
        self.conversation_manager = manager
        self._provider = provider
        self._cfg = cfg if cfg is not None else {}
        self._cfg.setdefault("timezone", tz)
        self._cfg.setdefault("agent_runner", {"runner_type": "local"})

    def get_config(self, umo=None):
        return self._cfg

    def get_using_provider(self, umo=None):
        return self._provider

    def get_db(self):
        return None


class FakeEvent:
    def __init__(self, umo="test:GroupMessage:1"):
        self.unified_msg_origin = umo
        self.sent: list = []
        self.replies: list[str] = []

    def plain_result(self, text):
        self.replies.append(text)
        return text

    async def send(self, chain):
        self.sent.append(chain)


def make_plugin(context, conf=None):
    inst = plugin_main.NcdExtraPlugin.__new__(plugin_main.NcdExtraPlugin)
    inst.context = context
    inst._conf = conf if conf is not None else {}
    return inst


async def drain(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def history_of(rounds: int) -> list[dict]:
    """造 rounds 轮对话，每轮 user + assistant。"""
    out: list[dict] = []
    for i in range(rounds):
        out.append({"role": "user", "content": f"第{i}个问题"})
        out.append({"role": "assistant", "content": f"第{i}个回答"})
    return out


def segments_len(rounds) -> int:
    return sum(len(rnd) for rnd in rounds)


# --------------------------------------------------------------------------- #
# ① 纯函数
# --------------------------------------------------------------------------- #
def test_pure() -> None:
    print("\n===== ① 纯函数 =====")
    check("①1 as_bool 认中文开关", plugin_main.as_bool("开") is True, "开 != True")
    check("①2 as_bool 认 off", plugin_main.as_bool("off") is False, "off 没识别")
    check("①3 as_bool 空值走默认", plugin_main.as_bool(None, True) is True, "默认值没生效")
    check("①4 parse_onoff 认 on", plugin_main.parse_onoff("ON") is True, "ON 没识别")
    check("①5 parse_onoff 认乱输", plugin_main.parse_onoff("随便") is None, "乱输应为 None")

    zh = plugin_main.estimate_tokens("你好世界")
    en = plugin_main.estimate_tokens("hello world")
    check("①6 中文估算 4 字 ≈ 2", zh == 2, f"得到 {zh}")
    check("①7 英文估算 11 字 ≈ 3", en == 3, f"得到 {en}")

    history = history_of(3)
    rounds = plugin_main.split_rounds(history)
    check("①8 切轮：3 轮", len(rounds) == 3, f"得到 {len(rounds)}")
    check("①9 每轮从 user 起", all(r[0]["role"] == "user" for r in rounds), "有轮次不是 user 开头")

    summary = "要点一\n要点二"
    recent = rounds[-1:]
    built = plugin_main.build_compact_history(summary, recent)
    check(
        "①10 摘要成对 user/assistant",
        built[0]["role"] == "user" and built[1]["role"] == "assistant",
        "摘要后面没有 assistant 应答",
    )
    check("①11 摘要正文进历史", "要点一" in built[0]["content"], "摘要内容丢了")
    check("①12 保留轮原样追加", built[2:] == recent[0], f"得到 {built[2:]}")

    bubble = plugin_main.tool_bubble_text("draw", {"prompt": "x" * 300})
    check("①13 气泡带工具名", "🔨 调用工具: draw" in bubble, bubble)
    check("①14 参数截断", len(bubble) < 200, f"太长 {len(bubble)}")
    check("①15 关掉参数只剩名字", "\n" not in plugin_main.tool_bubble_text("draw", {"a": 1}, show_args=False), "还有换行")

    result = plugin_main.tool_result_text("draw", "y" * 500, limit=70)
    check("①16 结果截断到 70 字", result.count("y") <= 71, f"得到 {len(result)}")

    chunks = plugin_main.chunk_rounds(rounds * 5, 60)
    check("①17 分批非空", len(chunks) >= 2, f"只分了 {len(chunks)} 批")
    check(
        "①18 每批不超限（单轮除外）",
        all(len(plugin_main.rounds_text(c)) <= 60 for c in chunks if len(c) > 1),
        "有批次超限",
    )
    check("①19 分批不丢轮次", segments_len([s for c in chunks for s in c]) == segments_len(rounds * 5), "轮次丢了")


# --------------------------------------------------------------------------- #
# ② /compact 正常路径
# --------------------------------------------------------------------------- #
def test_compact_ok() -> None:
    print("\n===== ② /compact 正常压缩 =====")
    old_history = history_of(10)
    provider = FakeProvider()
    manager = FakeManager(FakeConversation(old_history, token_usage=999))
    inst = make_plugin(FakeContext(manager=manager, provider=provider), {"compact_keep_rounds": 5})
    event = FakeEvent()

    asyncio.run(drain(inst.compact(event, "")))
    reply = event.replies[-1] if event.replies else ""
    check("②1 有回复", bool(reply), "没有输出")
    check("②2 报告写回成功", "已压缩" in reply, reply)
    check("②3 调了模型摘要", len(provider.calls) == 1, f"调了 {len(provider.calls)} 次")
    written = manager.written or []
    check("②4 历史写回数据库", written is not None, "没写回")
    check("②5 摘要 + 5 轮 = 12 条", len(written) == 2 + 10, f"得到 {len(written)}")
    check("②6 摘要在最前", "此前对话摘要" in written[0]["content"], str(written[0])[:80])
    check("②7 摘要内容来自模型", "摘要正文" in written[0]["content"], str(written[0])[:80])
    check("②8 最近一轮原样保留", written[-2:] == old_history[-2:], f"{written[-2:]}")
    check("②9 token_usage 归零", manager.written_token_usage == 0, f"得到 {manager.written_token_usage}")
    check("②10 早前 5 轮已丢弃", not any("第0个问题" in str(m) for m in written), "老内容还在")


def test_compact_guards() -> None:
    print("\n===== ③ 压缩的各种拦路 =====")
    # 轮数不够
    manager = FakeManager(FakeConversation(history_of(3)))
    inst = make_plugin(FakeContext(manager=manager, provider=FakeProvider()), {})
    event = FakeEvent()
    asyncio.run(drain(inst.compact(event, "")))
    check("③1 轮数不足直接拒", "不用压" in (event.replies[-1] if event.replies else ""), event.replies)
    check("③2 拦下时不写库", manager.written is None, "竟然写库了")

    # 收益太小（7 轮，默认 min 8 且只差 2 轮以内）
    manager = FakeManager(FakeConversation(history_of(7)))
    inst = make_plugin(FakeContext(manager=manager, provider=FakeProvider()), {})
    event = FakeEvent()
    asyncio.run(drain(inst.compact(event, "6")))
    check("③3 收益太小提示可强制", "收益太小" in (event.replies[-1] if event.replies else ""), event.replies)

    # 第三方 runner
    ctx = FakeContext(
        manager=FakeManager(FakeConversation(history_of(20))),
        provider=FakeProvider(),
        cfg={"agent_runner": {"runner_type": "dify"}},
    )
    inst = make_plugin(ctx, {})
    event = FakeEvent()
    asyncio.run(drain(inst.compact(event, "")))
    check("③4 第三方 runner 拒绝", "第三方" in (event.replies[-1] if event.replies else ""), event.replies)

    # 没有对话
    inst = make_plugin(FakeContext(manager=FakeManager(None), provider=FakeProvider()), {})
    event = FakeEvent()
    asyncio.run(drain(inst.compact(event, "")))
    check("③5 没对话时提示", "还没有对话" in (event.replies[-1] if event.replies else ""), event.replies)

    # 参数非法
    inst = make_plugin(
        FakeContext(manager=FakeManager(FakeConversation(history_of(20))), provider=FakeProvider()),
        {},
    )
    event = FakeEvent()
    asyncio.run(drain(inst.compact(event, "abc")))
    check("③6 非法轮数给用法", "用法" in (event.replies[-1] if event.replies else ""), event.replies)

    # 摘要失败 → 降级截断，不能卡死
    manager = FakeManager(FakeConversation(history_of(10)))
    inst = make_plugin(
        FakeContext(manager=manager, provider=FakeProvider(text=None)), {}
    )
    event = FakeEvent()
    asyncio.run(drain(inst.compact(event, "5")))
    reply = event.replies[-1] if event.replies else ""
    check("③7 摘要失败也写回", manager.written is not None, "降级没写回")
    check("③8 摘要失败有告警", "摘要生成失败" in reply, reply)
    check("③9 降级后仍是 2+10 条", len(manager.written or []) == 12, f"{len(manager.written or [])}")


def test_compact_chunked() -> None:
    print("\n===== ④ 超长历史分批摘要 =====")
    # 单批下限是 2000 字符（配置调得再小也不会低于它，防止配错把历史切碎），
    # 所以这里用 120 轮（约 6000 字符）来逼出分批。
    provider = FakeProvider("分批摘要")
    manager = FakeManager(FakeConversation(history_of(120)))
    inst = make_plugin(
        FakeContext(manager=manager, provider=provider),
        {"compact_chunk_chars": 2000, "compact_keep_rounds": 5},
    )
    event = FakeEvent()
    asyncio.run(drain(inst.compact(event, "")))
    check("④1 分批调了多次模型", len(provider.calls) > 2, f"只调了 {len(provider.calls)} 次")
    check("④2 仍然写回成功", manager.written is not None, "没写回")
    check("④3 摘要含分批内容", "分批摘要" in str(manager.written[0]["content"]), str(manager.written[0])[:100])
    check("④4 保留 5 轮 + 2 条摘要", len(manager.written or []) == 12, f"{len(manager.written or [])}")


# --------------------------------------------------------------------------- #
# ⑤ /tooldisplay
# --------------------------------------------------------------------------- #
def test_tooldisplay() -> None:
    print("\n===== ⑤ /tooldisplay =====")
    conf: dict = {}
    inst = make_plugin(FakeContext(manager=FakeManager(None), provider=FakeProvider()), conf)
    event = FakeEvent()

    # 默认关闭：hook 一个字都不发
    asyncio.run(inst._on_tool_bubble(event, types.SimpleNamespace(name="draw"), {"p": "1"}))
    check("⑤1 默认关闭不发气泡", event.sent == [], f"发了 {len(event.sent)} 条")

    asyncio.run(drain(inst.tooldisplay(event, "on")))
    check("⑤2 on 写入配置", plugin_main.as_bool(conf.get("tooldisplay")) is True, str(conf))
    asyncio.run(inst._on_tool_bubble(event, types.SimpleNamespace(name="draw"), {"p": "1"}))
    check("⑤3 开启后发一条", len(event.sent) == 1, f"发了 {len(event.sent)} 条")
    body = event.sent[0].chain[0].text if event.sent else ""
    check("⑤4 气泡带工具名", "draw" in body, body)
    check("⑤5 气泡带参数", "参数" in body, body)

    # 显示返回模式：调用时不发，结果回来才发
    conf["tooldisplay_show_result"] = True
    event.sent.clear()
    asyncio.run(inst._on_tool_bubble(event, types.SimpleNamespace(name="draw"), {"p": "1"}))
    check("⑤6 结果模式下调用时不发", event.sent == [], f"发了 {len(event.sent)} 条")
    result = types.SimpleNamespace(content=[types.SimpleNamespace(text="图生成了")])
    asyncio.run(inst._on_tool_result_bubble(event, types.SimpleNamespace(name="draw"), {}, result))
    check("⑤7 结果回来发一条", len(event.sent) == 1, f"发了 {len(event.sent)} 条")
    check("⑤8 结果带正文", "图生成了" in event.sent[0].chain[0].text, event.sent[0].chain[0].text)

    # 原生已开 → 让路
    conf["tooldisplay_show_result"] = False
    ctx = FakeContext(cfg={"provider_settings": {"show_tool_use_status": True}})
    inst2 = make_plugin(ctx, {"tooldisplay": True})
    ev2 = FakeEvent()
    asyncio.run(inst2._on_tool_bubble(ev2, types.SimpleNamespace(name="draw"), {}))
    check("⑤9 原生开着时让路", ev2.sent == [], "重复发了")

    asyncio.run(drain(inst.tooldisplay(event, "off")))
    check("⑤10 off 写回配置", plugin_main.as_bool(conf.get("tooldisplay")) is False, str(conf))
    asyncio.run(drain(inst.tooldisplay(event, "status")))
    check("⑤11 status 有状态行", "工具调用气泡" in (event.replies[-1] if event.replies else ""), event.replies)


# --------------------------------------------------------------------------- #
# ⑥ /token consumption
# --------------------------------------------------------------------------- #
def test_token() -> None:
    print("\n===== ⑥ /token consumption =====")
    conversation = FakeConversation(history_of(4), token_usage=5231)
    manager = FakeManager(conversation)
    inst = make_plugin(FakeContext(manager=manager, provider=FakeProvider()), {})

    async def fake_stats(_cid):
        return {"calls": 7, "input": 12000, "cached": 3000, "output": 900}

    inst._stat_sums = fake_stats
    event = FakeEvent()
    asyncio.run(drain(inst.token_usage(event, "")))
    report = event.replies[-1] if event.replies else ""
    print(report)
    check("⑥1 有报告", "token 用量" in report, report)
    check("⑥2 轮数正确", "4 轮 / 8 条" in report, report)
    check("⑥3 上一次请求回报", "5,231" in report, report)
    check("⑥4 累计次数", "7 次请求" in report, report)
    check("⑥5 合计=输入+输出", "15,900" in report, report)
    check("⑥6 显示模型", "fake-model" in report, report)

    asyncio.run(drain(inst.token_usage(event, "consumption")))
    check("⑥7 带子命令也认", "token 用量" in (event.replies[-1] if event.replies else ""), event.replies)
    asyncio.run(drain(inst.token_usage(event, "乱写")))
    check("⑥8 乱写子命令给用法", "用法" in (event.replies[-1] if event.replies else ""), event.replies)


# --------------------------------------------------------------------------- #
# ⑦ /get time
# --------------------------------------------------------------------------- #
def test_time() -> None:
    print("\n===== ⑦ /get time =====")
    conf: dict = {}
    inst = make_plugin(FakeContext(provider=FakeProvider()), conf)

    # 默认关闭：不注入
    request = types.SimpleNamespace(system_prompt="你是助手")
    asyncio.run(inst._inject_time(FakeEvent(), request))
    check("⑦1 默认关闭不注入", request.system_prompt == "你是助手", request.system_prompt)

    event = FakeEvent()
    asyncio.run(drain(inst.get_cmd(event, "time on")))
    check("⑦2 on 写入配置", plugin_main.as_bool(conf.get("time_inject")) is True, str(conf))
    check("⑦3 回复带当前时间", "当前是" in (event.replies[-1] if event.replies else ""), event.replies)

    request = types.SimpleNamespace(system_prompt="你是助手")
    asyncio.run(inst._inject_time(FakeEvent(), request))
    check("⑦4 开启后注入", "当前时间" in request.system_prompt, request.system_prompt)
    check("⑦5 原提示词还在", "你是助手" in request.system_prompt, request.system_prompt)
    check("⑦6 带 system_reminder 标签", "<system_reminder>" in request.system_prompt, request.system_prompt)
    check("⑦7 带星期", "周" in request.system_prompt, request.system_prompt)

    # system_prompt 为空时也不能崩
    request = types.SimpleNamespace(system_prompt=None)
    asyncio.run(inst._inject_time(FakeEvent(), request))
    check("⑦8 空提示词也能注入", "当前时间" in (request.system_prompt or ""), str(request.system_prompt))

    # 原生已开 → 让路
    inst2 = make_plugin(
        FakeContext(provider=FakeProvider(), cfg={"provider_settings": {"datetime_system_prompt": True}}),
        {"time_inject": True},
    )
    request = types.SimpleNamespace(system_prompt="原样")
    asyncio.run(inst2._inject_time(FakeEvent(), request))
    check("⑦9 原生开着时让路", request.system_prompt == "原样", request.system_prompt)

    asyncio.run(drain(inst.get_cmd(event, "time")))
    check("⑦10 只看状态", "模型获取时间" in (event.replies[-1] if event.replies else ""), event.replies)
    asyncio.run(drain(inst.get_cmd(event, "hello")))
    check("⑦11 乱写给用法", "用法" in (event.replies[-1] if event.replies else ""), event.replies)
    asyncio.run(drain(inst.get_cmd(event, "time 乱写")))
    check("⑦12 非法开关给提示", "只能接 on / off" in (event.replies[-1] if event.replies else ""), event.replies)


# --------------------------------------------------------------------------- #
# ⑧ 健壮性
# --------------------------------------------------------------------------- #
def test_robust() -> None:
    print("\n===== ⑧ 健壮性 =====")
    # 没有 conversation_manager / get_db 都不许崩
    inst = make_plugin(types.SimpleNamespace(get_config=lambda umo=None: {}), {})

    async def no_stats(_cid):
        return None

    inst._stat_sums = no_stats
    event = FakeEvent()
    asyncio.run(drain(inst.token_usage(event, "")))
    check("⑧1 无管理器不崩", "拿不到" in (event.replies[-1] if event.replies else ""), event.replies)

    # 历史上不是合法 JSON
    manager = FakeManager(FakeConversation("这不是json"))
    inst = make_plugin(FakeContext(manager=manager, provider=FakeProvider()), {})
    event = FakeEvent()
    asyncio.run(drain(inst.compact(event, "")))
    check("⑧2 坏历史不崩", bool(event.replies), "没回复")

    # 配置是普通 dict（没有 save_config）也要能工作
    conf = {"tooldisplay": False}
    inst = make_plugin(FakeContext(), conf)
    event = FakeEvent()
    asyncio.run(drain(inst.tooldisplay(event, "on")))
    check("⑧3 无 save_config 也能切", plugin_main.as_bool(conf.get("tooldisplay")) is True, str(conf))

    # 时区配置乱写 → 退回本机时区，不许抛
    inst = make_plugin(FakeContext(tz="Mars/Olympus"), {})
    moment, tzname = inst._now("test:GroupMessage:1")
    check("⑧4 坏时区能退回", bool(moment) and bool(tzname), f"{moment} {tzname}")


class FakeCommandEvent:
    """只喂 CommandFilter 需要的那几个方法。"""

    def __init__(self, text: str):
        self._text = text
        self.extra: dict = {}
        self.is_at_or_wake_command = True

    def get_message_str(self) -> str:
        return self._text

    def set_extra(self, key, value) -> None:
        self.extra[key] = value

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)


def parse_command(func, command: str, text: str):
    """复刻 AstrBot 的指令解析路径：CommandFilter.filter → parsed_params。"""
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star_handler import EventType, StarHandlerMetadata

    md = StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name=f"main_{func.__name__}",
        handler_name=func.__name__,
        handler_module_path="main",
        handler=func,
        event_filters=[],
    )
    cmd_filter = CommandFilter(command, handler_md=md)
    event = FakeCommandEvent(text)
    matched = cmd_filter.filter(event, {})
    return matched, event.extra.get("parsed_params")


def test_command_parsing() -> None:
    """⑨ 指令真能被 AstrBot 解析出参数（GreedyStr 必须是最后一个参数）。"""
    print("\n===== ⑨ 指令解析 =====")
    # ★ 必须拿「未绑定」的函数（类属性）：AstrBot 注册的就是它，
    #   而 init_handler_md 会跳过前两个参数（self / event）。拿绑定方法的话
    #   self 已经没了，args 会被当成第 1 个参数直接跳过 → parsed_params 永远是 {}。
    ###
    cases = [
        ("compact", "compact", "/compact", ""),
        ("compact", "compact", "/compact 8", "8"),
        ("tooldisplay", "tooldisplay", "/tooldisplay on", "on"),
        ("tooldisplay", "tooldisplay", "/tooldisplay", ""),
        ("token_usage", "token", "/token consumption", "consumption"),
        ("token_usage", "token", "/token", ""),
        ("get_cmd", "get", "/get time on", "time on"),
        ("get_cmd", "get", "/get time", "time"),
    ]
    for index, (attr, command, label, expected) in enumerate(cases, 1):
        func = getattr(plugin_main.NcdExtraPlugin, attr)
        matched, params = parse_command(func, command, label.lstrip("/"))
        check(
            f"⑨{index} {label} → args={expected!r}",
            matched is True and params == {"args": expected},
            f"matched={matched} params={params}",
        )

    # 不相关 / 前缀相似的消息不能被抢走
    token_func = plugin_main.NcdExtraPlugin.token_usage
    for index, text in enumerate(["随便说说", "tokenizer abc", "你好"], 9):
        matched, _ = parse_command(token_func, "token", text)
        check(f"⑨{index} {text!r} 不该被 token 抢", matched is False, f"matched={matched}")


def test_conf_schema() -> None:
    """⑩ 配置 schema 能被 AstrBot 正常解析、落盘、读回。"""
    print("\n===== ⑩ 配置 schema =====")
    import os
    import tempfile

    from astrbot.core.config.astrbot_config import AstrBotConfig

    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    check(
        "⑩1 每项都有 type/description",
        all("type" in v and "description" in v for v in schema.values()),
        "有配置项缺字段",
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "conf.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        conf = AstrBotConfig(config_path=path, schema=schema)
        check("⑩2 生成默认值", conf.get("compact_keep_rounds") == 5, str(dict(conf))[:120])
        check("⑩3 气泡默认关", conf.get("tooldisplay") is False, str(conf.get("tooldisplay")))
        check("⑩4 时间默认关", conf.get("time_inject") is False, str(conf.get("time_inject")))
        conf["tooldisplay"] = True
        conf.save_config()
        again = AstrBotConfig(config_path=path, schema=schema)
        check("⑩5 落盘后能读回", again.get("tooldisplay") is True, str(again.get("tooldisplay")))


def main() -> int:
    test_pure()
    test_compact_ok()
    test_compact_guards()
    test_compact_chunked()
    test_tooldisplay()
    test_token()
    test_time()
    test_robust()
    test_command_parsing()
    test_conf_schema()
    print("\n" + "=" * 52)
    if failures:
        print(f"❌ {len(failures)} 个用例失败：")
        for item in failures:
            print("  -", item)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
