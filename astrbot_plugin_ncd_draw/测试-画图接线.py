# -*- coding: utf-8 -*-
"""回归护栏：generate_image 的接线（画风 + 参考图）。

为什么要有这个文件：
    2026-10-18 生图页报 `NameError: name 'style' is not defined` ——
    我把「解析画风 / 参考图」的逻辑误插进了 `_render_standalone`（那里没有
    style/reference 变量），而且参考图分支还会自己调自己（无限递归）。
    光看编译是发现不了的：语法合法，只有真正调用才炸。

本测试直接调工具方法，用假客户端把 payload 记下来，验证：
  1. style 能解析成 loras 并传下去
  2. reference="message" 走 img2img，"last" 用上一张图
  3. reference 留空走文生图
  4. 认不出的 style 给人话、不抛异常

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-画图接线.py
"""

import asyncio
import pathlib
import sys
import tempfile
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import lora_catalog as lc  # noqa: E402
import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


# ---------------- 造一个 LoRA 目录（头部合法的假文件） ----------------
import json  # noqa: E402
import struct  # noqa: E402


def write_st(path: pathlib.Path, keys, meta) -> None:
    head = {k: {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]} for k in keys}
    head["__metadata__"] = meta
    blob = json.dumps(head).encode()
    path.write_bytes(struct.pack("<Q", len(blob)) + blob + b"\0\0")


tmp = pathlib.Path(tempfile.mkdtemp())
write_st(
    tmp / "SFW_alpha.safetensors",
    ["lora_te1_a.lora_down.weight"],
    {"ss_network_module": "networks.lora",
     "modelspec.architecture": "stable-diffusion-xl-v1-base/lora"},
)
LORAS = lc.scan(tmp)


class FakeClient:
    def __init__(self):
        self.calls = []

    async def txt2img(self, payload):
        self.calls.append(("txt2img", payload))
        return b"\x89PNG" + b"0" * 200, 12345

    async def img2img(self, payload):
        self.calls.append(("img2img", payload))
        return b"\x89PNG" + b"1" * 200, 12346


def make_plugin(opts=None):
    inst = plugin_main.NcdDrawPlugin.__new__(plugin_main.NcdDrawPlugin)
    inst._conf = dict({
        "sd_lora_dir": str(tmp),
        "sd_allow_nsfw_lora": True,
        "sd_lora_scale": 0.8,
        "img2img_strength": 0.55,
        "sd_progress_in_chat": False,
        "sd_negative": "lowres",
        "sd_width": 512, "sd_height": 768, "sd_steps": 10, "sd_cfg": 5.5,
    }, **(opts or {}))
    inst._revealed = {}
    # 画图入口已开（避免每次调用都撞闸门；闸门本身另有测试）
    inst._draw_revealed = {u: 1.0 for u in ('t:GroupMessage:1', 't:FriendMessage:1', 'A:GroupMessage:1')}
    inst._pending_images = {}
    inst._render_lock = asyncio.Lock()
    inst._last_image = None
    client = FakeClient()
    inst._sd_client = lambda: client
    inst._image_backend = lambda: "standalone"
    inst._lora_list = lambda: LORAS

    # 工具出图后会 send_message 把图发出去；测试里给个空壳，避免无关 traceback 干扰输出
    class FakeCtx:
        async def send_message(self, umo, chain):
            return None

    inst.context = FakeCtx()
    return inst, client


def make_event(*, images=()):
    msgs = [types.SimpleNamespace()]
    for p in images:
        msgs.append(plugin_main.Comp.Image(file=str(p), path=str(p)))
    return types.SimpleNamespace(
        unified_msg_origin="t:GroupMessage:1",
        get_messages=lambda: msgs,
        get_sender_id=lambda: "1",
        get_self_id=lambda: "2",
        get_sender_name=lambda: "x",
        get_group_id=lambda: "3",
    )


# =====================================================================
print("===== ① 普通文生图：style 解析成 loras =====")
inst, client = make_plugin()
out = asyncio.run(inst.generate_image(make_event(), "1girl", "alpha:0.7"))
kind, payload = client.calls[-1]
print(f"  {kind}  loras={payload.get('loras')}")
check("①", kind == "txt2img", f"应走文生图，实际 {kind}")
check("①", payload.get("loras", [{}])[0].get("name") == "alpha", "画风没解析出来")
check("①", payload["loras"][0]["scale"] == 0.7, f"强度不对：{payload['loras']}")
check("①", "sd_" in out or "画好了" in out or "画" in out, f"返回太怪：{out[:40]}")

# =====================================================================
print("\n===== ①-2 负面词：AI 传的是【追加】，不能冲掉默认那套 =====")
inst, client = make_plugin()
asyncio.run(inst.generate_image(make_event(), "1girl", "", "", "", False, "extra arms, helmet"))
kind, payload = client.calls[-1]
neg = payload.get("negative") or ""
print(f"  {kind}  negative={neg[:80]}…")
check("①-2", "lowres" in neg, "★ 默认那套（sd_negative）被冲掉了")
check("①-2", "extra arms" in neg and "helmet" in neg, "AI 给的负面词没进 payload")

# =====================================================================
print("\n===== ② 不填 style → 不挂 LoRA =====")
inst, client = make_plugin()
asyncio.run(inst.generate_image(make_event(), "1girl"))
print(f"  loras={client.calls[-1][1].get('loras')}")
check("②", client.calls[-1][1].get("loras") == [], "不该有 loras")

# =====================================================================
print("\n===== ③ 认不出的 style → 给人话，不抛异常 =====")
inst, client = make_plugin()
out = asyncio.run(inst.generate_image(make_event(), "1girl", "不存在的画风"))
print(f"  返回: {out[:70]}")
check("③", out.startswith("画图失败"), f"应返回失败说明，实际 {out[:40]}")
check("③", not client.calls, "解析失败时不该真的去出图")

# =====================================================================
print("\n===== ④ reference=last → 用上一张走 img2img =====")
img = tmp / "last.png"
img.write_bytes(b"\x89PNG" + b"2" * 100)
inst, client = make_plugin()
inst._last_image = (img, "last.png")
asyncio.run(inst.generate_image(make_event(), "1girl", "", "last"))
kind, payload = client.calls[-1]
print(f"  {kind}  strength={payload.get('strength')}  image={bool(payload.get('image'))}")
check("④", kind == "img2img", f"应走 img2img，实际 {kind}")
check("④", payload.get("image"), "没传参考图")
check("④", payload.get("strength") == 0.55, f"强度应来自配置：{payload.get('strength')}")

# =====================================================================
print("\n===== ⑤ reference=message → 用消息里的图 =====")
msg_img = tmp / "msg.png"
msg_img.write_bytes(b"\x89PNG" + b"3" * 100)
inst, client = make_plugin()
asyncio.run(inst.generate_image(make_event(images=[msg_img]), "1girl", "", "message"))
kind, payload = client.calls[-1]
print(f"  {kind}  image_len={len(payload.get('image') or '')}")
check("⑤", kind == "img2img", f"应走 img2img，实际 {kind}")

# =====================================================================
print("\n===== ⑥ 消息里没图 + reference=message → 明确拒绝，不画文生图 =====")
# 2026-10-19 事故：用户引用一张图说"用图生图"，插件取不到被引用的图就静默画了
# 一张毫无关系的新图。现在必须报错并让模型去要图，而不是"画点什么交差"。
inst, client = make_plugin()
out6 = asyncio.run(inst.generate_image(make_event(), "1girl", "", "message"))
print(f"  返回：{out6[:80]}…")
check("⑥", out6.startswith("画图失败"), f"应明确失败，实际 {out6[:40]}")
check("⑥", not client.calls, "★ 没取到参考图却还是画了（会画出一张无关的新图）")

# =====================================================================
print("\n===== ⑥-2 被引用的消息里有图 → 走 img2img =====")
# AstrBot 的 aiocqhttp 适配器会把被引用消息解析进 Reply.chain，这里模拟那种结构。
reply_img = tmp / "quoted.png"
reply_img.write_bytes(b"\x89PNG" + b"4" * 120)


def make_reply_event(path):
    quoted = plugin_main.Comp.Reply(
        id="123456",
        chain=[plugin_main.Comp.Image(file=str(path), path=str(path))],
        sender_id=1,
        sender_nickname="某人",
    )
    return types.SimpleNamespace(
        unified_msg_origin="t:GroupMessage:1",
        get_messages=lambda: [quoted],
        get_sender_id=lambda: "1",
        get_self_id=lambda: "2",
        get_sender_name=lambda: "x",
        get_group_id=lambda: "3",
    )


inst, client = make_plugin()
asyncio.run(inst.generate_image(make_reply_event(reply_img), "1girl", "", "message"))
kind, payload = client.calls[-1]
print(f"  {kind}  image_len={len(payload.get('image') or '')}")
check("⑥-2", kind == "img2img", f"★ 引用的图没被取到，实际走了 {kind}")
check("⑥-2", payload.get("image"), "参考图字节没传下去")

# =====================================================================
print("\n===== ⑦ _render_standalone 不许再自己解析 style/reference =====")
src = (PLUGIN / "main.py").read_text(encoding="utf-8")
i = src.find("async def _render_standalone(")
j = src.find("async def _render_standalone_img2img")
seg = src[i:j]
# 去掉字符串/文档，只看代码行
code = "\n".join(
    line for line in seg.splitlines()
    if not line.strip().startswith(("#", '"""', "★", "*"))
)
check("⑦", "resolve_loras(" not in code, "★_render_standalone 里又在解析画风了")
check("⑦", "_reference_bytes(" not in code, "★_render_standalone 里又在解析参考图了")
check("⑦", "_render_image(" not in code, "★_render_standalone 里又自调 _render_image（无限递归）")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 画风解析 / 不挂 / 报错给人话 / last 参考图 / message 参考图 / 退回文生图 / 渲染层不越位")


