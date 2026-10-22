# -*- coding: utf-8 -*-
"""回归护栏：四个预设工作流的解析、参数注入、LoRA 插入、闸门、空闲两档。

为什么要有这个文件：
    出图链路错一处，用户看到的就是"等了 1 分钟，出来一张废图"或者
    "报了个看不懂的错"。工作流的节点连线、参数注入、LoRA 插在哪儿，
    全是纯逻辑，不碰 GPU 也能测 —— 先用假客户端把线接对，再上真卡。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-接线.py
"""

import asyncio
import json
import pathlib
import sys
import tempfile
import time
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402
import model_paths  # noqa: E402
import presets  # noqa: E402
import sizing  # noqa: E402
from comfy_backend import ComfyClient  # noqa: E402

plugin_main.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    debug=lambda *a, **k: None,
)

failures: list[str] = []


def check(label: str, cond, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


# =====================================================================
print("===== ① 四个工作流都能解析 =====")
EXPECT = {
    "sdxl": {"size": (720, 960), "steps": 30, "cfg": 5.5, "sampler": "euler",
             "model": "SDXL\\novaAnimeXL_ilV140.safetensors", "loader": "CheckpointLoaderSimple"},
    "krea2": {"size": (720, 960), "steps": 10, "cfg": 1.0, "sampler": "euler",
              "model": "Krea2\\Krea2-MuseByStable_v30Turbo_fp8.safetensors", "loader": "UNETLoader"},
    "anima": {"size": (720, 960), "steps": 22, "cfg": 5.5, "sampler": "euler",
              "model": "anima\\novaAnimeAM_v6029B.safetensors", "loader": "UNETLoader"},
    "moss": {"size": (720, 960), "steps": 50, "cfg": 4.5, "sampler": "er_sde",
             "model": "MOSS\\mossImage_sft_pruned_int8.safetensors", "loader": "UNETLoader"},
}
graphs = {}
for name, want in EXPECT.items():
    g = presets.load_graph(name)
    graphs[name] = g
    info = presets.summarize(g)
    roles = presets.analyze(g)
    print(f"  {name}: {info['width']}×{info['height']} {info['steps']}步 CFG {info['cfg']:g} "
          f"{info['sampler_name']} 底模={info['model']}")
    check("①", (info["width"], info["height"]) == want["size"], f"{name} 画幅不对：{info['width']}×{info['height']}")
    check("①", info["steps"] == want["steps"], f"{name} 步数不对：{info['steps']}")
    check("①", abs(info["cfg"] - want["cfg"]) < 1e-9, f"{name} CFG 不对：{info['cfg']}")
    check("①", info["sampler_name"] == want["sampler"], f"{name} 采样器不对：{info['sampler_name']}")
    check("①", info["model"] == want["model"], f"{name} 底模不对：{info['model']}")
    check("①", g[roles["model_loader"]]["class_type"] == want["loader"],
          f"{name} 加载节点类型不对")
    check("①", g[roles["positive"]]["class_type"] == "CLIPTextEncode", f"{name} 正面节点不对")
    check("①", g[roles["negative"]]["class_type"] == "CLIPTextEncode", f"{name} 负面节点不对")
    check("①", g[roles["sampler"]]["class_type"] == "KSampler", f"{name} 采样节点不对")
    check("①", roles["outputs"], f"{name} 找不到输出节点")

print("\n===== ② 参数注入只改该改的 =====")
g = presets.apply_params(
    graphs["sdxl"], prompt="1girl, solo", negative="bad hands",
    width=832, height=1216, steps=20, cfg=6.5, seed=1234,
)
check("②", g["5"]["inputs"]["text"] == "1girl, solo", "正面词没写进去")
check("②", g["10"]["inputs"]["text"] == "bad hands", "负面词没写进去")
check("②", g["13"]["inputs"]["width"] == 832 and g["13"]["inputs"]["height"] == 1216, "画幅没写进去")
check("②", g["4"]["inputs"]["steps"] == 20 and g["4"]["inputs"]["cfg"] == 6.5, "步数/CFG 没写进去")
check("②", g["4"]["inputs"]["seed"] == 1234, "种子没写进去")
check("②", g["4"]["inputs"]["sampler_name"] == "euler", "采样器被动了（不该动）")
check("②", g["11"]["inputs"]["ckpt_name"] == EXPECT["sdxl"]["model"], "底模被动了（不该动）")
check("②", set(g) == set(graphs["sdxl"]), f"节点数变了：{set(g) - set(graphs['sdxl'])}")

print("\n===== ③ 换底模写在正确的字段上 =====")
g = presets.apply_params(graphs["sdxl"], model="SDXL\\other.safetensors")
check("③", g["11"]["inputs"]["ckpt_name"] == "SDXL\\other.safetensors", "SDXL 底模没换成 ckpt_name")
g = presets.apply_params(graphs["moss"], model="MOSS\\other.safetensors")
check("③", g["23"]["inputs"]["unet_name"] == "MOSS\\other.safetensors", "MOSS 底模没换成 unet_name")
check("③", g["2"]["inputs"]["clip_name"] == "mossImage_sft_txt_int8.safetensors",
      "换底模把文本编码器也改了（不该改）")

print("\n===== ④ LoRA 动态插入 =====")
g = presets.apply_params(
    graphs["moss"],
    loras=[{"name": "Moss\\a.safetensors", "strength": 0.8},
           {"name": "Moss\\b.safetensors", "strength": 0.5}],
)
check("④", "lora_1" in g and "lora_2" in g, "没插入 LoRA 节点")
check("④", g["lora_1"]["class_type"] == "LoraLoader", "插入的不是 LoraLoader")
# MOSS 原来 KSampler.model 接的是 SkipLayer(#30)，LoRA 应接在它后面
check("④", g["lora_1"]["inputs"]["model"] == ["30", 0],
      f"LoRA 没接在 SkipLayer 后面：{g['lora_1']['inputs']['model']}")
check("④", g["lora_1"]["inputs"]["clip"] == ["2", 0],
      f"第一条 LoRA 的 clip 没接文本编码器：{g['lora_1']['inputs']['clip']}")
check("④", g["lora_2"]["inputs"]["model"] == ["lora_1", 0], "第二条 LoRA 没串在第一条后面")
check("④", g["lora_2"]["inputs"]["clip"] == ["lora_1", 1], "CLIP 链没串起来")
check("④", g["3"]["inputs"]["model"] == ["lora_2", 0], "★ KSampler 没接最后一条 LoRA")
check("④", g["21"]["inputs"]["clip"] == ["lora_2", 1], "★ 正面编码器没接最后一条 LoRA")
check("④", g["31"]["inputs"]["clip"] == ["lora_2", 1], "★ 负面编码器没接最后一条 LoRA")
check("④", g["lora_1"]["inputs"]["lora_name"] == "Moss\\a.safetensors", "LoRA 名字不对")
check("④", abs(g["lora_1"]["inputs"]["strength_model"] - 0.8) < 1e-9, "LoRA 强度不对")
check("④", graphs["moss"]["3"]["inputs"]["model"] == ["30", 0], "★ 原图被改了（应该只在副本上动）")

print("\n===== ⑤ 画幅解析 / 夹取 =====")
check("⑤", sizing.parse_size("720x960", 1920, 1920 * 1440) == (720, 960), "720x960 解析错")
check("⑤", sizing.parse_size("竖", 1920, 1920 * 1440) == (720, 960), "竖 解析错")
check("⑤", sizing.parse_size("", 1920, 1920 * 1440) is None, "空串应返回 None")
# 2.7648 MP 正好是 1920×1440：不能因为它比自己小一点点就误拒
check("⑤", sizing.parse_size("1920x1440", 1920, int(2.7648 * 1e6)) == (1920, 1440),
      "★ 1920x1440 被误拒了（上限设成了 2.76）")
check("⑤", sizing.warn_side(1920) == 1440, f"提醒线应为 1440，实际 {sizing.warn_side(1920)}")
check("⑤", sizing.fit_within(3840, 2160, 1920, 1920 * 1440)[0] <= 1920, "4K 参考图没缩到上限内")
for bad in ("abc", "100x100", "3000x3000", "1920x1920"):
    try:
        sizing.parse_size(bad, 1920, 1920 * 1440)
        check("⑤", False, f"{bad!r} 应该被拒绝")
    except ValueError:
        pass
check("⑤", sizing.resolve_size("", 0, 0, (720, 960), 1920, 1920 * 1440) == (720, 960), "默认为空时没回预设值")
check("⑤", sizing.resolve_size("", 832, 0, (720, 960), 1920, 1920 * 1440) == (832, 960),
      "只给宽时另一半没回默认")

print("\n===== ⑥ 建插件（假后端 + 临时模型目录）=====")
tmp = pathlib.Path(tempfile.mkdtemp())
models_root = tmp / "models"
for rel, size in (
    ("checkpoints/SDXL/novaAnimeXL_ilV140.safetensors", 2_000_000),
    ("checkpoints/SDXL/other-ckpt.safetensors", 2_000_000),
    ("diffusion_models/MOSS/alt-unet.safetensors", 2_000_000),
    ("loras/sdxl/cunnyfunky.safetensors", 1_000_000),
    ("loras/Moss/style-moss.safetensors", 1_000_000),
):
    p = models_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)
yaml_path = tmp / "extra_model_paths.yaml"
yaml_path.write_text(
    "mods:\n"
    f"    base_path: {models_root}\n"
    "    checkpoints: checkpoints\n"
    "    diffusion_models: diffusion_models\n"
    "    loras: loras\n",
    encoding="utf-8",
)

CONF = {
    "comfy_dir": str(tmp),
    "comfy_extra_paths": str(yaml_path),
    "comfy_preset": "sdxl",
    "comfy_min_model_gb": 0.0005,
    "comfy_width": 0, "comfy_height": 0,
    "comfy_steps": 0, "comfy_cfg": 0.0,
    "comfy_lora_strength": 1.0,
    "comfy_max_side": 1920, "comfy_max_mp": 2.76,
    "comfy_notify": True,
    "comfy_unload_timeout": 300, "comfy_idle_timeout": 1800,
}


class FakeClient:
    def __init__(self):
        self.graphs = []
        self.uploads = []
        self.sent = 0
        self.port = 8199
        self.comfy_dir = tmp

    def healthy_sync(self):
        return {"system": {"comfyui_version": "0.42.0"},
                "devices": [{"name": "cuda:0", "vram_total": 12 * 1024**3,
                             "vram_free": 9 * 1024**3}]}

    async def ensure_server(self):
        return self.healthy_sync()

    async def upload_image(self, data, filename="reference.png"):
        self.uploads.append((len(data), filename))
        return f"ref_{len(self.uploads)}.png"

    async def generate(self, graph):
        self.graphs.append(graph)
        return {"images": [{"filename": "x_00001_.png", "subfolder": "", "type": "output"}],
                "seconds": 3.2}

    async def fetch_image(self, image):
        return b"\x89PNG\r\n\x1a\n" + b"x" * 64

    def owned(self):
        return True

    def model_loaded(self):
        return True

    async def queue_status(self):
        return {"running": 0, "pending": 0}


def make_plugin(revealed=True, conf=None):
    inst = plugin_main.NcdComfyPlugin.__new__(plugin_main.NcdComfyPlugin)
    inst._conf = dict(conf or CONF)
    inst._revealed = {"t:FriendMessage:1": 1.0} if revealed else {}
    inst._render_lock = asyncio.Lock()
    inst._gen_waiting = 0
    inst._last_preset = None
    inst._last_model = None
    inst._scan_cache = {}
    inst._client_obj = None
    inst._client_key = None
    client = FakeClient()

    class FakeCtx:
        def __init__(self):
            self.messages = []

        async def send_message(self, umo, chain):
            self.messages.append((umo, chain))

    inst.context = FakeCtx()
    inst._client = lambda: client
    return inst, client


def make_event(umo="t:FriendMessage:1"):
    return types.SimpleNamespace(
        unified_msg_origin=umo,
        get_messages=lambda: [types.SimpleNamespace()],
    )


print("\n===== ⑦ 闸门：没调入口就出图 =====")
inst0, client0 = make_plugin(revealed=False)
out = asyncio.run(inst0.comfy_generate(make_event(), "1girl"))
check("⑦", "comfy_tools" in out, "没提示先去调 comfy_tools")
check("⑦", not client0.graphs, "★ 闸门没挡住，还是提交了")
for tool in ("comfy_models", "comfy_status"):
    out = asyncio.run(getattr(inst0, tool)(make_event()))
    check("⑦", "comfy_tools" in out, f"{tool} 没被闸门挡住")

print("\n===== ⑧ 正常出图：参数一路接到工作流 =====")
inst, client = make_plugin()
out = asyncio.run(
    inst.comfy_generate(
        make_event(), "1girl, solo", negative="bad hands", preset="sdxl",
        size="832x1216", steps=20, cfg=6.5, seed=777, model="other-ckpt",
    )
)
print("  " + out.splitlines()[0])
check("⑧", len(client.graphs) == 1, f"应提交 1 次，实际 {len(client.graphs)}")
g = client.graphs[0]
check("⑧", g["5"]["inputs"]["text"] == "1girl, solo", "正面词没接上")
check("⑧", g["10"]["inputs"]["text"] == "bad hands", "负面词没接上")
check("⑧", g["13"]["inputs"]["width"] == 832 and g["13"]["inputs"]["height"] == 1216, "画幅没接上")
check("⑧", g["4"]["inputs"]["steps"] == 20 and g["4"]["inputs"]["cfg"] == 6.5, "步数/CFG 没接上")
check("⑧", g["4"]["inputs"]["seed"] == 777, "种子没接上")
check("⑧", g["11"]["inputs"]["ckpt_name"] == "SDXL\\other-ckpt.safetensors", "底模没解析对")
check("⑧", "画好" in out and len(inst.context.messages) >= 1, "图没发到会话")

print("\n===== ⑨ LoRA 参数解析 =====")
inst, client = make_plugin()
out = asyncio.run(
    inst.comfy_generate(make_event(), "1girl", preset="moss",
                        lora="cunnyfunky:0.7, style-moss")
)
check("⑨", len(client.graphs) == 1, "LoRA 那次没提交")
g = client.graphs[0]
check("⑨", "lora_1" in g and "lora_2" in g, "两个 LoRA 没都插进去")
check("⑨", g["lora_1"]["inputs"]["lora_name"] == "sdxl\\cunnyfunky.safetensors",
      f"LoRA 名没解析对：{g['lora_1']['inputs']['lora_name']}")
check("⑨", abs(g["lora_1"]["inputs"]["strength_model"] - 0.7) < 1e-9, "显式强度没生效")
check("⑨", abs(g["lora_2"]["inputs"]["strength_model"] - 1.0) < 1e-9, "默认强度不对")
check("⑨", g["3"]["inputs"]["model"] == ["lora_2", 0], "KSampler 没接最后一条 LoRA")

print("\n===== ⑩ 参数越界 / 名字写错 =====")
inst, client = make_plugin()
out = asyncio.run(inst.comfy_generate(make_event(), "1girl", size="3000x3000"))
check("⑩", "出图失败" in out and not client.graphs, "超大画幅没被拒绝")
out = asyncio.run(inst.comfy_generate(make_event(), "1girl", preset="nope"))
check("⑩", "没有工作流" in out, "不存在的 preset 没报错")
out = asyncio.run(inst.comfy_generate(make_event(), "1girl", model="不存在"))
check("⑩", "没有这个底模" in out and not client.graphs, "不存在的底模没报错")
out = asyncio.run(inst.comfy_generate(make_event(), "1girl", lora="不存在:0.5"))
check("⑩", "没有这个LoRA" in out, "不存在的 LoRA 没报错")
out = asyncio.run(inst.comfy_generate(make_event(), ""))
check("⑩", "prompt 是空的" in out, "空 prompt 没被拒绝")
# 提醒线：超过 3/4 先问一句，带 force 才画
client.graphs.clear()
out = asyncio.run(inst.comfy_generate(make_event(), "1girl", size="1600x1200"))
check("⑩", "提醒线" in out, f"超提醒线没先问一句：{out[:60]}")
check("⑩", not client.graphs, "★ 没确认就画了")
out = asyncio.run(inst.comfy_generate(make_event(), "1girl", size="1600x1200", force=True))
check("⑩", len(client.graphs) == 1, "带 force=True 应该能画")
out = asyncio.run(inst.comfy_generate(make_event(), "1girl", steps=999, cfg=99))
g = client.graphs[-1]
check("⑩", g["4"]["inputs"]["steps"] == 150, f"步数上限没夹住：{g['4']['inputs']['steps']}")
check("⑩", g["4"]["inputs"]["cfg"] == 30, f"CFG 上限没夹住：{g['4']['inputs']['cfg']}")

print("\n===== ⑪ 底模 / LoRA 清单 =====")
inst, _ = make_plugin()
out = asyncio.run(inst.comfy_models(make_event()))
check("⑪", "novaAnimeXL" in out and "other-ckpt" in out, "SDXL 底模清单不对")
check("⑪", "cunnyfunky" not in out, "★ LoRA 混进底模清单了")
out = asyncio.run(inst.comfy_models(make_event(), preset="moss"))
check("⑪", "alt-unet" in out, "MOSS 应该列 diffusion_models 里的底模")
out = asyncio.run(inst.comfy_models(make_event(), kind="lora"))
check("⑪", "cunnyfunky" in out and "style-moss" in out, "LoRA 清单不对")

print("\n===== ⑫ extra_model_paths.yaml 解析 =====")
parsed = model_paths.parse_yaml(yaml_path)
check("⑫", "checkpoints" in parsed and parsed["checkpoints"][0].name == "checkpoints",
      f"没解析出 checkpoints：{parsed.get('checkpoints')}")
check("⑫", "diffusion_models" in parsed, "没解析出 diffusion_models")
check("⑫", "loras" in parsed, "没解析出 loras")

print("\n===== ⑬ 空闲两档：5 分钟卸模型 / 30 分钟关进程 =====")


class FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 1


def idle_client():
    c = ComfyClient(comfy_dir=str(tmp), port=8199,
                    unload_timeout=300, idle_timeout=1800)
    c._proc = FakeProc()
    c._owned = True
    c._model_loaded = True
    return c


c = idle_client()
c._last_used = time.time()
check("⑬", not c._should_stop_idle() and not c._should_unload_models(), "★ 刚起来就被回收")
c._last_used = 0
check("⑬", not c._should_stop_idle() and not c._should_unload_models(), "★ _last_used=0 被当成空闲")
c._last_used = time.time() - 400          # 6 分钟
check("⑬", c._should_unload_models(), "6 分钟了还不卸模型")
check("⑬", not c._should_stop_idle(), "★ 6 分钟就把进程关了（应等到 30 分钟）")
c._last_used = time.time() - 1900         # 31 分钟
check("⑬", c._should_stop_idle(), "31 分钟了还不关进程")
c._model_loaded = False
check("⑬", not c._should_unload_models(), "模型已经卸了还在重复卸")
c._busy = True
check("⑬", not c._should_stop_idle() and not c._should_unload_models(), "★ 出图途中被回收")
c._busy = False
c._starting = True
check("⑬", not c._should_stop_idle(), "★ 冷启动途中被回收")
c._starting = False
c._owned = False
check("⑬", not c._should_stop_idle(), "★ 别人开的 ComfyUI 被杀了")

print("\n===== ⑭ 配置 schema 能被 AstrBot 真的解析 =====")
from astrbot.core.config.astrbot_config import AstrBotConfig  # noqa: E402

schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
cfg = AstrBotConfig(config_path=str(tmp / "plugin_cfg.json"), schema=schema)
check("⑭", len(cfg) == len(schema), f"配置项数量不对：{len(cfg)} vs {len(schema)}")
for key in schema:
    check("⑭", key in cfg, f"schema 里的 {key} 没进默认配置")
check("⑭", cfg.get("comfy_idle_timeout") == 1800, "默认 30 分钟关进程没解析出来")
check("⑭", cfg.get("comfy_unload_timeout") == 300, "默认 5 分钟卸模型没解析出来")
check("⑭", cfg.get("comfy_preset") == "sdxl", "默认工作流没解析出来")

print("\n===== ⑮ 图生图：动态插节点（不动原工作流）=====")
g = presets.apply_params(
    graphs["sdxl"], source_image="ref_test.png", denoise=0.55, width=640, height=480
)
check("⑮", "13" not in g, "★ 空 latent 节点没被替掉")
loads = [nid for nid, n in g.items() if n["class_type"] == "LoadImage"]
encodes = [nid for nid, n in g.items() if n["class_type"] == "VAEEncode"]
scales = [nid for nid, n in g.items() if n["class_type"] == "ImageScale"]
check("⑮", len(loads) == 1 and len(encodes) == 1 and len(scales) == 1,
      f"插入的节点数量不对：LoadImage {loads} / VAEEncode {encodes} / ImageScale {scales}")
check("⑮", g[loads[0]]["inputs"]["image"] == "ref_test.png", "LoadImage 文件名不对")
check("⑮", g[scales[0]]["inputs"]["width"] == 640 and g[scales[0]]["inputs"]["height"] == 480,
      "ImageScale 尺寸不对")
check("⑮", g[encodes[0]]["inputs"]["vae"] == ["11", 2],
      f"VAEEncode 没复用工作流的 VAE：{g[encodes[0]]['inputs']['vae']}")
check("⑮", g["4"]["inputs"]["latent_image"] == [encodes[0], 0], "KSampler 没接编码后的 latent")
check("⑮", abs(g["4"]["inputs"]["denoise"] - 0.55) < 1e-9, "denoise 没写进去")
check("⑮", graphs["sdxl"]["4"]["inputs"]["latent_image"] == ["13", 0], "★ 原图被改了")
# 文生图路径不能受任何影响
g2 = presets.apply_params(graphs["sdxl"], prompt="x")
check("⑮", not any(n["class_type"] in ("LoadImage", "VAEEncode", "ImageScale")
                   for n in g2.values()), "★ 文生图被塞进了图生图节点")
check("⑮", g2["4"]["inputs"]["denoise"] == 1, "★ 文生图的 denoise 被改了")

print("\n===== ⑯ 插件级图生图（reference=last / message）=====")
import struct  # noqa: E402

ref_png = (
    b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    + struct.pack(">II", 640, 480) + b"\x08\x06\x00\x00\x00" + b"\0\0\0\0"
)
check("⑯", sizing.image_size(ref_png) == (640, 480), "PNG 宽高没解析出来")

inst, client = make_plugin()
inst._last_image_bytes = ref_png
out = asyncio.run(inst.comfy_generate(make_event(), "1girl", reference="last", denoise=0.45))
check("⑯", len(client.uploads) == 1, f"参考图没上传（uploads={client.uploads}）")
check("⑯", len(client.graphs) == 1, "图生图那次没提交")
g = client.graphs[0]
loads = [nid for nid, n in g.items() if n["class_type"] == "LoadImage"]
check("⑯", loads and g[loads[0]]["inputs"]["image"] == "ref_1.png", "LoadImage 没指向上传的图")
check("⑯", abs(g["4"]["inputs"]["denoise"] - 0.45) < 1e-9, "denoise 没接上")
check("⑯", "13" not in g, "空 latent 没被替掉")
scales = [nid for nid, n in g.items() if n["class_type"] == "ImageScale"]
check("⑯", g[scales[0]]["inputs"]["width"] == 640 and g[scales[0]]["inputs"]["height"] == 480,
      f"没按参考图尺寸：{g[scales[0]]['inputs']['width']}×{g[scales[0]]['inputs']['height']}")
check("⑯", "图生图" in out, "返回里没说明是图生图")

# reference=last 但没有上一张 → 明确报错，不静默退回文生图
inst2, client2 = make_plugin()
inst2._last_image_bytes = None
out2 = asyncio.run(inst2.comfy_generate(make_event(), "1girl", reference="last"))
check("⑯", "没找到" in out2 and not client2.graphs, "★ 没有上一张却静默改成文生图了")

# reference=message：图在消息里
inst3, client3 = make_plugin()
img_file = tmp / "incoming.png"
img_file.write_bytes(ref_png)
ev = types.SimpleNamespace(
    unified_msg_origin="t:FriendMessage:1",
    get_messages=lambda: [plugin_main.Comp.Image(file=str(img_file))],
)
out3 = asyncio.run(inst3.comfy_generate(ev, "1girl", reference="message"))
check("⑯", len(client3.uploads) == 1, "消息里的图没被用上")
check("⑯", not client3.graphs == [], "消息图那次没提交")

print("\n===== ⑰ 生图才排队：第二张会告诉用户排第几 =====")


def msg_texts(inst):
    texts = []
    for _umo, chain in inst.context.messages:
        for comp in getattr(chain, "chain", []):
            t = getattr(comp, "text", None)
            if t:
                texts.append(str(t))
    return texts


inst, client = make_plugin()
inst._revealed.update({"t:A": 1.0, "t:B": 1.0})


async def two_at_once():
    orig = client.generate

    async def slow(graph):
        await asyncio.sleep(0.35)
        return await orig(graph)

    client.generate = slow
    t1 = asyncio.create_task(inst.comfy_generate(make_event("t:A"), "1girl"))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(inst.comfy_generate(make_event("t:B"), "1girl"))
    return await asyncio.gather(t1, t2)


r1, r2 = asyncio.run(two_at_once())
texts = msg_texts(inst)
print("  提示条数:", len(texts), "|", [t[:28] for t in texts])
check("⑰", len(client.graphs) == 2, f"两张都该出图，实际 {len(client.graphs)}")
check("⑰", any("前面还有 1 张" in t for t in texts), "★ 第二张没收到排队提示")
check("⑰", inst._gen_waiting == 0, f"排队计数没归零：{inst._gen_waiting}")

# =====================================================================
print("\n" + "=" * 60)
if failures:
    print(f"❌ {len(failures)} 项不通过：")
    for f in failures:
        print("  · " + f)
    sys.exit(1)
print("✅ 全部通过：预设解析、参数注入、LoRA 插入、图生图、闸门、模型清单、空闲两档、配置解析")
print("=" * 60)
