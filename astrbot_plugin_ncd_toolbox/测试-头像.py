# -*- coding: utf-8 -*-
"""回归护栏：头像工具。

要钉死的四件事：
  1. who 参数怎么解析（留空/me/group/QQ号/@QQ/垃圾输入）
  2. 缓存语义（命中不联网；ttl=0 强制重下）
  3. 只给模型看、**绝不发送** —— 工具里不许出现 send_message
  4. 待看图用完即清（否则每轮都塞一张，白花钱）

全程不联网：`_download` 被替换掉。
"""

import asyncio
import pathlib
import sys
import tempfile
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import avatar  # noqa: E402
import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


# =====================================================================
print("===== ① who 解析 =====")
cases = {
    "": ("user", ""),
    "me": ("user", "@self"),
    "group": ("group", "@group"),
    "10000001": ("user", "10000001"),
    "@10000002": ("user", "10000002"),
    " 123456 ": ("user", "123456"),
}
for raw, want in cases.items():
    got = avatar.parse_target(raw)
    print(f"  {raw!r:16} -> {got}")
    check("①", got == want, f"{raw!r} 应为 {want}，实际 {got}")

for bad in ("asdf", "123", "12位以上的号码abcdefg"):
    try:
        avatar.parse_target(bad)
        check("①", False, f"{bad!r} 应该报错")
    except avatar.AvatarError:
        print(f"  {bad!r:16} -> AvatarError ✓")

# =====================================================================
print("\n===== ② URL 与尺寸夹取 =====")
u = avatar.build_url("10000001", 640)
print(f"  {u}")
check("②", "nk=10000001" in u and "s=640" in u, "用户头像 URL 不对")
check("②", "s=640" in avatar.build_url("10000001", 9999), "大尺寸没被夹到 640")
check("②", "s=40" in avatar.build_url("10000001", 10), "小尺寸没被夹到 40")
g = avatar.build_group_url("10000003", 640)
print(f"  {g}")
check("②", "p.qlogo.cn/gh/10000003/10000003/640" in g, "群头像 URL 不对")
try:
    avatar.build_url("abc")
    check("②", False, "非数字 QQ 应该报错")
except avatar.AvatarError:
    pass

# 只允许 qlogo 域名
try:
    avatar._download("https://evil.example.com/x.png")
    check("②", False, "★非 qlogo 域名竟然放行了")
except avatar.AvatarError as exc:
    print(f"  非白名单域名 -> AvatarError: {exc}")

# =====================================================================
print("\n===== ③ 缓存语义（不联网）=====")
calls = {"n": 0}
FAKE = b"\x89PNG\r\n\x1a\n" + b"0" * 500


def fake_download(url: str) -> bytes:
    calls["n"] += 1
    return FAKE


real_download = avatar._download
avatar._download = fake_download
try:
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        p1, c1 = asyncio.run(avatar.fetch_avatar(kind="user", key="123456", cache_dir=d, ttl=3600))
        p2, c2 = asyncio.run(avatar.fetch_avatar(kind="user", key="123456", cache_dir=d, ttl=3600))
        print(f"  第一次 cached={c1}，第二次 cached={c2}，下载次数={calls['n']}")
        check("③", c1 is False, "第一次不该算缓存命中")
        check("③", c2 is True, "第二次应命中缓存")
        check("③", calls["n"] == 1, f"应只下载 1 次，实际 {calls['n']}")
        check("③", p1.read_bytes() == FAKE, "落盘内容不对")

        _, c3 = asyncio.run(avatar.fetch_avatar(kind="user", key="123456", cache_dir=d, ttl=0))
        print(f"  ttl=0 时 cached={c3}，下载次数={calls['n']}")
        check("③", c3 is False and calls["n"] == 2, "ttl=0 应该强制重下")
finally:
    avatar._download = real_download

# =====================================================================
print("\n===== ④ 只给模型看：工具里不许发送 =====")
src = (PLUGIN / "main.py").read_text(encoding="utf-8")
i = src.find('@filter.llm_tool(name="view_avatar")')
# 取到下一个「4 空格缩进的 @」为止 —— 那才是下一个方法/装饰器。
# ★ 别用「下一个 async def」：docstring 里也可能出现这几个字，会截断错。
k = src.find("\n    @", i + 10)
tool_body = src[i : k if k != -1 else i + 6000]
print(f"  工具代码 {len(tool_body)} 字符")
check("④", "send_message" not in tool_body,
      "★view_avatar 里出现了 send_message —— 头像会被发到聊天里")
check("④", "只给你自己看" in tool_body, "没告诉模型「不要发给用户」")
check("④", "self._pending_images" in tool_body, "没把图排进待看队列")

# =====================================================================
print("\n===== ⑤ 待看图用完即清 =====")
inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
inst._pending_images = {}
inst._conf = {"avatar_size": 640, "avatar_cache_ttl": 3600}

event = types.SimpleNamespace(
    unified_msg_origin="aiocqhttp:GroupMessage:10000003",
    get_sender_id=lambda: "10000002",
    get_self_id=lambda: "10000001",
    get_group_id=lambda: "10000003",
)


def fake_fetch(**kwargs):
    async def _run():
        p = pathlib.Path(tempfile.gettempdir()) / "fake_avatar.png"
        p.write_bytes(FAKE)
        return p, False
    return _run()


real_fetch = plugin_main.fetch_avatar
plugin_main.fetch_avatar = fake_fetch
try:
    out = asyncio.run(inst.view_avatar(event, ""))
    print(f"  工具返回: {out[:70]}…")
    check("⑤", "头像" in out, "返回里没提头像")
    bucket = inst._pending_images.get(event.unified_msg_origin) or []
    check("⑤", len(bucket) == 1, f"应排队 1 张，实际 {len(bucket)}")

    req = types.SimpleNamespace(image_urls=[])
    inst._attach_pending_images(req, event)
    print(f"  attach 后 req.image_urls = {req.image_urls}")
    check("⑤", len(req.image_urls) == 1, "没塞进请求")
    check("⑤", not inst._pending_images.get(event.unified_msg_origin),
          "★用完没清空，下一轮会重复塞图")

    req2 = types.SimpleNamespace(image_urls=[])
    inst._attach_pending_images(req2, event)
    check("⑤", req2.image_urls == [], "第二轮不该再带图")
finally:
    plugin_main.fetch_avatar = real_fetch

# =====================================================================
print("\n===== ⑥ who 的容错解析（填群名/昵称也不能失败）=====")
from astrbot.api import message_components as Comp  # noqa: E402


def make_event(*, sender_id="10000002", sender_name="小八", self_id="10000001",
               group_id="10000003", ats=()):
    msgs = [types.SimpleNamespace()]  # 一条普通文本占位
    for qq, name in ats:
        msgs.append(Comp.At(qq=qq, name=name))
    return types.SimpleNamespace(
        unified_msg_origin="aiocqhttp:GroupMessage:10000003",
        get_sender_id=lambda: sender_id,
        get_sender_name=lambda: sender_name,
        get_self_id=lambda: self_id,
        get_group_id=lambda: group_id,
        get_messages=lambda: msgs,
    )


inst2 = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)

cases2 = [
    ("", make_event(), "user", "10000002", ""),
    ("me", make_event(), "user", "10000001", ""),
    ("group", make_event(), "group", "10000003", ""),
    ("10000002", make_event(), "user", "10000002", ""),
    ("小八", make_event(), "user", "10000002", ""),
    ("老八说和做", make_event(), "user", "10000002", "没认出"),        # ← 事故现场：群名
    ("他", make_event(ats=[("10000005", "阿七")]), "user", "10000005", ""),
    ("阿七", make_event(ats=[("10000005", "阿七")]), "user", "10000005", ""),
    ("随便什么", make_event(ats=[("10000005", "阿七")]), "user", "10000005", "没认出"),
]
for who, ev, want_kind, want_key, want_note in cases2:
    kind, key, label, note = inst2._avatar_target(who, ev)
    print(f"  {who!r:12} -> {kind}={key} ({label})" + (f"  note={note}" if note else ""))
    check("⑥", kind == want_kind and key == want_key,
          f"{who!r} 应为 {want_kind}={want_key}，实际 {kind}={key}")
    if want_note:
        check("⑥", want_note in note,
              f"{who!r} 的说明里应含「{want_note}」，实际 {note!r}")

try:
    inst2._avatar_target("老八说和做", make_event())
    print("  '老八说和做' 不再抛异常 ✓")
except Exception as exc:  # noqa: BLE001
    check("⑥", False, f"★「老八说和做」仍然抛异常：{exc}")

kind, key, _, _ = inst2._avatar_target("group", make_event(group_id=""))
print(f"  私聊 + who='group' -> {kind}={key!r}（应回落到说话人）")
check("⑥", key == "10000002", "私聊里取群头像应回落成说话人，而不是空")

# =====================================================================
print("\n===== ⑦ 工具注册一致性 =====")
check("⑦", "view_avatar" in plugin_main.TOOL_NAMES, "TOOL_NAMES 里没有 view_avatar")
check("⑦", hasattr(plugin_main.NcdToolboxPlugin, "view_avatar"), "类上没有 view_avatar")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: who 解析 / URL 与尺寸 / 缓存 / 只给模型看 / 用完即清 / 注册一致")
