# -*- coding: utf-8 -*-
"""回归护栏：换底模（人和 AI 都能换）。

为什么要有这个文件：
    2026-10-18 加"换画图模型"：底模目录里混着 3 个底模和 4 个 LoRA，
    靠文件体积区分；名字解析要能容忍不带扩展名/大小写/一段子串；
    换失败时【绝不能】把"当前模型"改成没加载上的那个。

本测试验证：
  1. 列表只收 ≥ sd_model_min_gb 的 .safetensors/.ckpt（LoRA 被排除）
  2. 名字解析：全名 / 带扩展名 / 大小写 / 唯一子串；多个命中要报错而不是随便挑
  3. switch_model 工具：换成功改"当前模型"；留空只列清单；失败不动状态
  4. 换过之后，generate_image / 生图页都会把 checkpoint 带进 payload
  5. 页面 status 能列出底模并标出当前那个

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-换模型.py
"""

import asyncio
import json
import pathlib
import sys
import tempfile
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


# ---------------- 假底模目录 ----------------
# 体积阈值设成 4294 字节（≈0.000004 GB）：
#   5000 字节的当"底模"，100 字节的当"LoRA"。
tmp = pathlib.Path(tempfile.mkdtemp())
BIG = 5000
NAMES = ["NoobAI-XL-v1.1", "waiIllustriousSDXL_v170", "illustriousSnow25DHaremStyle_v10"]
for n in NAMES:
    (tmp / f"{n}.safetensors").write_bytes(b"\0" * BIG)
(tmp / "SFW_cunnyfunky.safetensors").write_bytes(b"\0" * 100)   # LoRA，不该进列表
(tmp / "notes.txt").write_bytes(b"\0" * BIG)                    # 扩展名不对

CONF = {
    "sd_checkpoint": str(tmp / "illustriousSnow25DHaremStyle_v10.safetensors"),
    "sd_model_dir": str(tmp),
    "sd_model_min_gb": 0.000004,
    "sd_width": 512, "sd_height": 768, "sd_steps": 10, "sd_cfg": 5.5,
    "img2img_strength": 0.55, "sd_negative": "lowres",
    "sd_progress_in_chat": False, "sd_allow_nsfw_lora": True, "sd_lora_scale": 0.8,
}


class FakeClient:
    def __init__(self, fail: bool = False):
        self.calls = []
        self.fail = fail

    async def switch_model(self, path):
        self.calls.append(("switch_model", path))
        if self.fail:
            raise plugin_main.SDBackendError("模拟加载失败")
        return {"ok": True, "checkpoint": path, "name": pathlib.Path(path).name,
                "switched": True, "seconds": 16.2}

    async def txt2img(self, payload):
        self.calls.append(("txt2img", payload))
        return b"\x89PNG" + b"0" * 100, 12345

    async def img2img(self, payload):
        self.calls.append(("img2img", payload))
        return b"\x89PNG" + b"1" * 100, 12346


def make_plugin(client=None, conf=None):
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = dict(CONF, **(conf or {}))
    inst._revealed = {}
    inst._pending_images = {}
    inst._render_lock = asyncio.Lock()
    inst._last_image = None
    inst._sd = None
    inst._lora_list = lambda: []
    client = client or FakeClient()
    inst._sd_client = lambda: client

    class FakeCtx:
        async def send_message(self, umo, chain):
            return None

    inst.context = FakeCtx()
    return inst, client


def make_event():
    return types.SimpleNamespace(
        unified_msg_origin="t:GroupMessage:1",
        get_messages=lambda: [types.SimpleNamespace()],
        get_sender_id=lambda: "1", get_self_id=lambda: "2",
        get_sender_name=lambda: "x", get_group_id=lambda: "3",
    )


# =====================================================================
print("===== ① 列表：只收底模（按体积排除 LoRA / 非 safetensors） =====")
inst, _ = make_plugin()
models = inst._model_list()
got = [m["name"] for m in models]
print(f"  {got}")
check("①", got == sorted(NAMES, key=str.lower), f"列表不对：{got}")
check("①", all("size_mb" in m and "path" in m for m in models), "size_mb / path 没填")
check("①", [m["current"] for m in models].count(True) == 1, "应正好标记 1 个当前底模")

# =====================================================================
print("\n===== ② 名字解析 =====")
cases = [
    ("NoobAI-XL-v1.1", "NoobAI-XL-v1.1"),
    ("noobai-xl-v1.1.safetensors", "NoobAI-XL-v1.1"),
    ("WAI", "waiIllustriousSDXL_v170"),
    # ★ 别用 "illustrious" 当唯一子串的例子：它同时命中
    #   illustriousSnow... 和 waiIllustrious...，本来就该判歧义（见下面）
    ("snow25d", "illustriousSnow25DHaremStyle_v10"),
]
for want, expect in cases:
    path = inst._resolve_model(want)
    print(f"  {want!r:34} → {pathlib.Path(path).stem}")
    check("②", pathlib.Path(path).stem == expect, f"{want!r} 解析成了 {pathlib.Path(path).stem}")

for bad in ("", "不存在的模型", "v1"):   # "v1" 太短，不做子串匹配
    try:
        inst._resolve_model(bad)
        failures.append(f"② {bad!r} 本该解析失败")
        print(f"  {bad!r:34} → 没报错（不应该）")
    except plugin_main.ModelError as exc:
        print(f"  {bad!r:34} → ModelError: {str(exc)[:50]}…")

# 歧义：三个模型名字里都有 "i"，明确命中多个
(tmp / "extra_illustrious_v2.safetensors").write_bytes(b"\0" * BIG)
try:
    inst._resolve_model("illustrious")
    failures.append("② 歧义子串应该报错（现在会随便挑一个）")
    print("  'illustrious' → 没报错（不应该）")
except plugin_main.ModelError as exc:
    print(f"  'illustrious' → 歧义报错：{str(exc)[:60]}…")
    check("②", "同时匹配" in str(exc), f"歧义提示不清楚：{exc}")
(tmp / "extra_illustrious_v2.safetensors").unlink()

# =====================================================================
print("\n===== ③ switch_model 工具 =====")
inst, client = make_plugin()
out = asyncio.run(inst.switch_model(make_event(), "noob"))
print(f"  换 noob → {out}")
check("③", out.startswith("已切换底模"), f"应返回成功：{out[:40]}")
check("③", client.calls and client.calls[0][0] == "switch_model", "没调到 client.switch_model")
check("③", pathlib.Path(client.calls[0][1]).stem == "NoobAI-XL-v1.1", "传下去的路径不对")
check("③", pathlib.Path(inst._model_override).stem == "NoobAI-XL-v1.1", "当前模型没更新")

out = asyncio.run(inst.switch_model(make_event(), ""))
print(f"  留空 → {out[:60]}…")
check("③", "可选底模" in out and "NoobAI-XL-v1.1" in out, "留空应列出清单")

inst, client = make_plugin(client=FakeClient(fail=True))
out = asyncio.run(inst.switch_model(make_event(), "wai"))
print(f"  加载失败 → {out}")
check("③", out.startswith("换模型失败"), f"应返回失败：{out[:40]}")
check("③", getattr(inst, "_model_override", None) is None,
      "★换失败后不能把当前模型改成没加载上的那个")

inst, client = make_plugin()
out = asyncio.run(inst.switch_model(make_event(), "根本不存在"))
print(f"  名字不存在 → {out[:60]}…")
check("③", out.startswith("换模型失败") and "NoobAI" in out, "应报错并列出可选项")
check("③", not client.calls, "名字解析失败时不该去加载")

# =====================================================================
print("\n===== ④ 换过之后，出图请求带上 checkpoint =====")
inst, client = make_plugin()
asyncio.run(inst.switch_model(make_event(), "wai"))
asyncio.run(inst.generate_image(make_event(), "1girl"))
kind, payload = client.calls[-1]
print(f"  {kind}  checkpoint={pathlib.Path(payload.get('checkpoint')).stem}")
check("④", kind == "txt2img", f"应走文生图：{kind}")
check("④", pathlib.Path(payload["checkpoint"]).stem == "waiIllustriousSDXL_v170",
      "换过的底模没带进 payload")

# 没换过 → 用配置里的默认底模
inst, client = make_plugin()
asyncio.run(inst.generate_image(make_event(), "1girl"))
payload = client.calls[-1][1]
print(f"  默认  checkpoint={pathlib.Path(payload.get('checkpoint')).stem}")
check("④", pathlib.Path(payload["checkpoint"]).stem == "illustriousSnow25DHaremStyle_v10",
      f"默认底模不对：{payload.get('checkpoint')}")

# =====================================================================
print("\n===== ⑤ 生图页：status 列底模 / generate 认 model =====")

inst, client = make_plugin()


async def fake_json(default=None):
    return {"prompt": "1girl", "model": "noob"}


async def fake_render(prompt, opts):
    inst._page_opts = dict(opts)
    return b"\x89PNG" + b"9" * 100, "x.png"


captured: dict = {}


def fake_response(obj, status_code=200):
    captured.clear()
    captured.update(obj)
    return captured


plugin_main.web_request.json = fake_json
plugin_main.json_response = fake_response
inst._render_image = fake_render
inst._save_generated = lambda data, name, tag: tmp / name
asyncio.run(inst.api_imggen_generate())
print(f"  页面选 noob → checkpoint={pathlib.Path(inst._page_opts.get('checkpoint')).stem}"
      f"  override={pathlib.Path(inst._model_override).stem}")
check("⑤", pathlib.Path(inst._page_opts["checkpoint"]).stem == "NoobAI-XL-v1.1",
      "页面选的底模没进 opts")
check("⑤", pathlib.Path(inst._model_override).stem == "NoobAI-XL-v1.1",
      "★页面换过之后，AI 下次出图又会切回默认（白等 16 秒）")

asyncio.run(inst.api_imggen_status())
status = dict(captured)
print(f"  status={json.dumps(status, ensure_ascii=False)[:220]}")
check("⑤", len(status["models"]) == 3, f"status 应列出 3 个底模：{status['models']}")
check("⑤", status["model"].endswith(".safetensors"), f"model 应带扩展名：{status['model']}")
check("⑤", sum(1 for m in status["models"] if m["current"]) == 1,
      "status 里应正好标 1 个当前底模")

# =====================================================================
print("\n===== ⑥ generate_image 参数没被塞第 4 个（清单工具是独立的） =====")
import inspect  # noqa: E402

params = list(inspect.signature(plugin_main.NcdToolboxPlugin.generate_image).parameters)
print(f"  generate_image{tuple(params)}")
check("⑥", params == ["self", "event", "prompt", "style", "reference", "size", "force"],
      f"generate_image 参数变了：{params}")
sw_params = list(inspect.signature(plugin_main.NcdToolboxPlugin.switch_model).parameters)
check("⑥", sw_params == ["self", "event", "model"], f"switch_model 参数不对：{sw_params}")
check("⑥", "switch_model" in plugin_main.TOOL_NAMES, "switch_model 不在 TOOL_NAMES 里")
check("⑥", "switch_model" in plugin_main.TOOL_SUMMARY, "TOOL_SUMMARY 漏了 switch_model")

# =====================================================================
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 列表按体积过滤 / 名字解析与歧义 / 换成功改状态 / 换失败不改状态 / "
      "payload 带 checkpoint / 页面能选并同步当前底模")
