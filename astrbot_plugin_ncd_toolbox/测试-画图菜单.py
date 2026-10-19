# -*- coding: utf-8 -*-
"""回归护栏：画图二级目录（draw_tools）+ AI 自选画幅。

为什么要有这个文件：
    2026-10-18 用户说"生图设置给 ai 展现的很多，单独把生图划进一个画图工具目录，
    细节工具部分展开二级目录"。改动看起来只是加了个工具，但真正容易坏的是：
      · draw_tools 只解开了"自己那个会话"，却把别的会话也一起放开了
      · 清单里没有提示词规则/画风/底模 → 模型拿到工具却不会用
      · size 解析失败时【静默用默认尺寸】→ 模型以为指定了，实际画的是另一个比例
    这几条都在这里钉住。

本测试覆盖：
  1. draw_tools 返回的内容：提示词四条规则 / 画风清单 / 底模清单 / 当前参数
  2. 重复调用只回一句短提醒（省上下文）
  3. 只解锁本会话（另一个会话仍然看不到画图工具）
  4. generate_image(size=...) → payload 带 width/height；留空 → 不带（用配置默认）
  5. size 写错 → 直接返回人话，不去出图

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-画图菜单.py
"""

import asyncio
import json
import pathlib
import struct
import sys
import tempfile
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import lora_catalog as lc  # noqa: E402
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


# ---------------- 造一个画风目录 + 底模目录 ----------------
tmp = pathlib.Path(tempfile.mkdtemp())


def write_st(path: pathlib.Path, keys, meta) -> None:
    head = {k: {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]} for k in keys}
    head["__metadata__"] = meta
    blob = json.dumps(head).encode()
    path.write_bytes(struct.pack("<Q", len(blob)) + blob + b"\0\0")


write_st(
    tmp / "SFW_cunnyfunky.safetensors",
    ["lora_te1_a.lora_down.weight"],
    {"ss_network_module": "networks.lora",
     "modelspec.architecture": "stable-diffusion-xl-v1-base/lora"},
)
(tmp / "waiIllustriousSDXL_v170.safetensors").write_bytes(b"\0" * 5000)
LORAS = lc.scan(tmp)

CONF = {
    "sd_lora_dir": str(tmp),
    "sd_model_dir": str(tmp),
    "sd_model_min_gb": 0.000004,     # 5000 字节的文件算底模，几百字节的 LoRA 不算
    "sd_checkpoint": str(tmp / "waiIllustriousSDXL_v170.safetensors"),
    "sd_allow_nsfw_lora": True,
    "sd_lora_scale": 0.8,
    "sd_width": 720, "sd_height": 1024, "sd_steps": 25, "sd_cfg": 5.5,
    "sd_sampler": "euler",
    "img2img_strength": 0.7,
    "sd_negative": "lowres",
    "sd_progress_in_chat": False,
}


class FakeClient:
    def __init__(self):
        self.calls = []

    async def txt2img(self, payload):
        self.calls.append(("txt2img", payload))
        return b"\x89PNG" + b"0" * 100, 4242

    async def img2img(self, payload):  # pragma: no cover - 本测试用不到
        self.calls.append(("img2img", payload))
        return b"\x89PNG" + b"1" * 100, 4243


def make_plugin():
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = dict(CONF)
    inst._revealed = {}
    inst._draw_revealed = {}
    inst._pending_images = {}
    inst._render_lock = asyncio.Lock()
    inst._last_image = None
    inst._sd = None
    inst._lora_list = lambda: LORAS
    client = FakeClient()
    inst._sd_client = lambda: client

    class FakeCtx:
        async def send_message(self, umo, chain):
            return None

    inst.context = FakeCtx()
    return inst, client


def make_event(umo="t:GroupMessage:1"):
    return types.SimpleNamespace(
        unified_msg_origin=umo,
        get_messages=lambda: [types.SimpleNamespace()],
        get_sender_id=lambda: "1", get_self_id=lambda: "2",
        get_sender_name=lambda: "x", get_group_id=lambda: "3",
    )


# =====================================================================
print("===== ① draw_tools 返回的内容 =====")
inst, client = make_plugin()
out = asyncio.run(inst.draw_tools(make_event()))
print(out[:400].replace("\n", " | "))
for token, label in (
    ("generate_image", "没给出 generate_image"),
    ("switch_model", "没给出 switch_model"),
    ("masterpiece", "没给提示词画质前缀"),
    ("77 token", "没给 77 token 上限"),
    ("cunnyfunky", "没给画风清单"),
    ("waiIllustriousSDXL_v170", "没给底模清单"),
    ("采样器", "没给当前采样器"),
    ("832x1216", "没给画幅写法"),
):
    check("①", token in out, label)

check("①", inst._draw_revealed.get("t:GroupMessage:1"), "调用后没记下「已展开」")
check("①", "720×1024" in out or "720x1024" in out, "没给出当前默认尺寸")

# =====================================================================
print("\n===== ② 重复调用只回一句提醒 =====")
again = asyncio.run(inst.draw_tools(make_event()))
print(f"  第二次：{again[:80]}…")
check("②", len(again) < len(out), "第二次不该再给一遍长清单")
check("②", "已经展开" in again, "第二次没提示「已经展开」")
check("②", "cunnyfunky" not in again, "第二次仍在塞画风清单")

# =====================================================================
print("\n===== ③ 只解锁本会话 =====")
inst2, _ = make_plugin()
asyncio.run(inst2.draw_tools(make_event("A:GroupMessage:1")))
check("③", "A:GroupMessage:1" in inst2._draw_revealed, "A 会话没记下")
check("③", "B:GroupMessage:2" not in inst2._draw_revealed, "★ B 会话被一起放开了")


class FakeToolSet:
    def __init__(self, names):
        self.tools = [types.SimpleNamespace(name=n) for n in names]

    def remove_tool(self, name):
        self.tools = [t for t in self.tools if t.name != name]

    def names(self):
        return [t.name for t in self.tools]


ts_a = FakeToolSet(list(plugin_main.TOOL_NAMES))
ts_b = FakeToolSet(list(plugin_main.TOOL_NAMES))
inst2._apply_draw_gate(types.SimpleNamespace(func_tool=ts_a), make_event("A:GroupMessage:1"))
inst2._apply_draw_gate(types.SimpleNamespace(func_tool=ts_b), make_event("B:GroupMessage:2"))
print(f"  A 看得到：{sorted(set(ts_a.names()) & set(plugin_main.DRAW_TOOLS))}")
print(f"  B 看得到：{sorted(set(ts_b.names()) & set(plugin_main.DRAW_TOOLS))}")
check("③", set(plugin_main.DRAW_TOOLS) <= set(ts_a.names()), "A 会话应该能看到画图工具")
check("③", not (set(plugin_main.DRAW_TOOLS) & set(ts_b.names())),
      "★ B 会话不该看到画图工具")

# =====================================================================
print("\n===== ④ 画幅真的进了 payload =====")
inst, client = make_plugin()
asyncio.run(inst.generate_image(make_event(), "1girl, forest", "", "", "832x1216"))
kind, payload = client.calls[-1]
print(f"  size=832x1216 → {payload.get('width')}x{payload.get('height')}")
check("④", (payload.get("width"), payload.get("height")) == (832, 1216),
      f"画幅没传对：{payload.get('width')}x{payload.get('height')}")
check("④", payload.get("sampler") == "euler", f"payload 里的采样器不对：{payload.get('sampler')}")

inst, client = make_plugin()
asyncio.run(inst.generate_image(make_event(), "1girl, forest"))
payload = client.calls[-1][1]
print(f"  不给 size → {payload.get('width')}x{payload.get('height')}（应等于配置 720x1024）")
check("④", (payload.get("width"), payload.get("height")) == (720, 1024),
      f"留空时应落回配置里的默认尺寸，实际 {payload.get('width')}x{payload.get('height')}")

inst, client = make_plugin()
out = asyncio.run(inst.generate_image(make_event(), "1girl, forest", "", "", "3000x3000"))
print(f"  size=3000x3000 → {out[:60]}")
check("④", out.startswith("画图失败"), "画幅越界应直接返回失败")
check("④", not client.calls, "画幅不合法时不该去加载/出图")

# =====================================================================
print("\n===== ④-2 大画幅：先警告，用户同意后才画 =====")
PRIVATE = "t:FriendMessage:1"
# ★ 群聊里 >1440 一律拒绝（严令禁止），所以这里要用【私聊】会话测警告/放行
inst, client = make_plugin()
warn = asyncio.run(inst.generate_image(make_event(PRIVATE), "1girl, forest", "", "", "1600x1600"))
print(f"  私聊 1600x1600 不带 force → {warn[:70]}…")
check("④-2", warn.startswith("⚠️"), f"应先返回警告，实际 {warn[:30]}")
check("④-2", "问" in warn and "force" in warn, "警告里没让模型去问用户 / 没说要带 force")
check("④-2", "群聊内严令禁止" in warn, "★ 警告里没写「群聊内严令禁止 >1440」")
check("④-2", not client.calls, "★ 只是警告，不该真的去出图")

inst, client = make_plugin()
done = asyncio.run(inst.generate_image(make_event(PRIVATE), "1girl, forest", "", "", "1600x1600", True))
payload = client.calls[-1][1] if client.calls else {}
print(f"  私聊 1600x1600 带 force → {done[:30]}…  画幅={payload.get('width')}x{payload.get('height')}")
check("④-2", client.calls, "★ 用户同意（force=True）之后还是没画")
check("④-2", (payload.get("width"), payload.get("height")) == (1600, 1600),
      f"画幅不对：{payload.get('width')}x{payload.get('height')}")

# 群聊：>1440 就算带 force 也拒绝
inst, client = make_plugin()
grp = asyncio.run(inst.generate_image(make_event(), "1girl, forest", "", "", "1600x1600", True))
print(f"  群聊 1600x1600 带 force → {grp[:50]}")
check("④-2", grp.startswith("画图失败") and "群聊" in grp, "★ 群聊里应该直接拒绝")
check("④-2", not client.calls, "群聊被拒时不该去出图")

# 边界：正好 1440 不算"超过"，不用问直接画
inst, client = make_plugin()
asyncio.run(inst.generate_image(make_event(), "1girl, forest", "", "", "1440x1440"))
print(f"  群聊 1440x1440（未超过）→ 画了 {bool(client.calls)}")
check("④-2", client.calls, "1440 整不该触发警告")

# 1920 及以上直接拒绝（实测会爆显存）
inst, client = make_plugin()
out = asyncio.run(inst.generate_image(make_event(PRIVATE), "1girl, forest", "", "", "1920x1920", True))
print(f"  私聊 1920x1920 带 force → {out[:60]}")
check("④-2", out.startswith("画图失败"), "★ 1920×1920 就算带 force 也必须拒绝")
check("④-2", not client.calls, "1920 不该去加载/出图")

# 最大可用画布 1920×1440：私聊要先问，同意后要能画
inst, client = make_plugin()
warn2 = asyncio.run(inst.generate_image(make_event(PRIVATE), "1girl, forest", "", "", "1920x1440"))
print(f"  私聊 1920x1440 不带 force → 警告={warn2.startswith('⚠️')}")
check("④-2", warn2.startswith("⚠️"), "1920×1440 单边超 1440，应先警告")
inst, client = make_plugin()
asyncio.run(inst.generate_image(make_event(PRIVATE), "1girl, forest", "", "", "1920x1440", True))
payload = client.calls[-1][1] if client.calls else {}
print(f"  私聊 1920x1440 带 force → 画了 {bool(client.calls)}  画幅="
      f"{payload.get('width')}x{payload.get('height')}")
check("④-2", (payload.get("width"), payload.get("height")) == (1920, 1440),
      "★ 1920×1440 是最大画布，force 之后必须能画")

# =====================================================================
print("\n===== ④-3 上限可配置（换大显存卡不用改代码） =====")
inst_big, _ = make_plugin()
inst_big._conf = dict(CONF, sd_max_side=1024, sd_max_mp=1.0)
cap, px, mp, warn = inst_big._size_limits()
print(f"  sd_max_side=1024 / sd_max_mp=1.0 → 单边 {cap}、面积 {px} px（{mp} MP）、警告线 {warn}")
check("④-3", (cap, mp) == (1024, 1.0), f"配置没生效：{cap} / {mp}")
check("④-3", warn == 768, f"警告线应是上限的 3/4（768），实际 {warn}")

for bad, why in (("1600x1200", "单边超上限"), ("1024x1024", "面积超上限")):
    out = asyncio.run(inst_big.generate_image(make_event(PRIVATE), "1girl", "", "", bad))
    print(f"  {bad} → {out[:50]}")
    check("④-3", out.startswith("画图失败"), f"{bad} 应被拒绝（{why}）")

# 同一台机器上默认值仍能画 1920×1440（说明改的是配置，不是能力）
inst_def, _ = make_plugin()
_dcap, _dpx, _dmp, _dwarn = inst_def._size_limits()
print(f"  默认 limits = 单边 {_dcap}、面积 {_dpx} px（{_dmp} MP）、警告线 {_dwarn}")
check("④-3", _dcap == 1920 and _dpx >= 1920 * 1440,
      f"★ 默认上限不够画 1920×1440：{_dcap} / {_dpx} px")

# =====================================================================
print("\n===== ⑤ 画图工具在 TOOL_NAMES / 菜单里 =====")
check("⑤", "draw_tools" in plugin_main.TOOL_NAMES, "draw_tools 不在 TOOL_NAMES")
check("⑤", plugin_main.DRAW_TOOLS == ("generate_image", "switch_model"),
      f"DRAW_TOOLS 变了：{plugin_main.DRAW_TOOLS}")
check("⑤", "draw_tools" in plugin_main.TOOL_SUMMARY, "TOOL_SUMMARY 漏了 draw_tools")

# =====================================================================
print("\n===== ⑥ 没展开时要在提示词里点明「先调 draw_tools」 =====")
# 事故（2026-10-19）：重载插件后 _draw_revealed 清空，模型照旧直接调
# generate_image，runner 只记一条「未找到指定的工具」，模型无从纠正、连续空转。
inst6, _ = make_plugin()


class FakeReq:
    def __init__(self):
        self.system_prompt = "你是丝瓜。"


req6 = FakeReq()
inst6._inject_draw_gate_hint(req6, make_event(PRIVATE))
print(f"  注入后尾部：{req6.system_prompt[-70:]}")
check("⑥", plugin_main.DRAW_GATE_MARK in req6.system_prompt,
      "★ 没告诉模型要先调 draw_tools")
check("⑥", "draw_tools" in req6.system_prompt, "提示里没提 draw_tools")
check("⑥", req6.system_prompt.startswith("你是丝瓜。"), "不该破坏原系统提示词")

before = len(req6.system_prompt)
inst6._inject_draw_gate_hint(req6, make_event(PRIVATE))
print(f"  再注入一次：长度 {before} → {len(req6.system_prompt)}")
check("⑥", len(req6.system_prompt) == before, "★ 不幂等（每轮都会变长）")

inst6._draw_revealed[PRIVATE] = 1.0
req6b = FakeReq()
inst6._inject_draw_gate_hint(req6b, make_event(PRIVATE))
print(f"  已展开的会话：{req6b.system_prompt!r}")
check("⑥", plugin_main.DRAW_GATE_MARK not in req6b.system_prompt,
      "已展开的会话不该再注入这段")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 二级目录内容齐全 / 重复调用省上下文 / 按会话隔离 / 画幅进 payload / 越界报错")
