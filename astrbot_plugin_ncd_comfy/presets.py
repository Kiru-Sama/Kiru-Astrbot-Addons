# -*- coding: utf-8 -*-
"""预设工作流：直接用你调好的 API 格式工作流，只改里面的参数。

为什么不再自己画节点图：
  · 工作流是你调过的（模型、LoRA、SkipLayerGuidance 都是试出来的），
    插件再拼一遍等于把调好的东西丢掉
  · 所以这里只做三件事：读 JSON、认出「哪个节点是干什么的」、按参数改inputs

角色识别（自动，不写死节点 id）：
  KSampler ← positive/negative/latent_image/model 四条连线往回找
  于是四个文件节点 id 各不相同也没关系。

LoRA 特别注意：
  你导出的四个工作流里【已经没有 LoraLoader 节点】了（导出前应该是静音/删掉了）。
  AI 要加 LoRA 时，这里在 model 链上动态插 LoraLoader，并且把 CLIP 也串进去 ——
  只改内存里的图，磁盘上的工作流文件一个字节都不动。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

PRESET_DIR = Path(__file__).resolve().parent / "workflows"

#: 预设 → 文件 / 显示名 / 大致耗时（秒）
PRESETS: dict[str, dict] = {
    "sdxl": {
        "file": "sdxl.json",
        "label": "SDXL",
        "eta": 25,
        "model_kind": "checkpoint",
        "desc": "SDXL 单文件底模，最快",
    },
    "krea2": {
        "file": "krea2.json",
        "label": "KREA2",
        "eta": 30,
        "model_kind": "unet",
        "desc": "KREA2（UNET + qwen3vl 文本编码器）",
    },
    "anima": {
        "file": "anima.json",
        "label": "ANIMA",
        "eta": 40,
        "model_kind": "unet",
        "desc": "Anima（UNET + Qwen3-0.6B 文本编码器）",
    },
    "moss": {
        "file": "moss.json",
        "label": "MOSS",
        "eta": 60,
        "model_kind": "unet",
        "desc": "MOSS-Image（50 步，最慢但细节最好）",
    },
}

DEFAULT_PRESET = "sdxl"

#: 能当「底模」的加载节点类
LOADER_CLASSES = (
    "CheckpointLoaderSimple",
    "CheckpointLoader",
    "UNETLoader",
    "UnetLoaderGGUF",
    "UNETLoaderGGUF",
)

#: 输出节点（SaveImage 落 output，PreviewImage 落 temp）
OUTPUT_CLASSES = ("SaveImage", "PreviewImage")


class WorkflowError(Exception):
    """工作流本身的问题（文件缺了、结构不认识）。"""


def preset_names() -> list[str]:
    return list(PRESETS.keys())


def load_graph(preset: str) -> dict:
    """读预设工作流（API 格式）。"""
    meta = PRESETS.get(preset)
    if not meta:
        raise WorkflowError(
            f"没有这个工作流预设：{preset}。可选：{'、'.join(PRESETS)}"
        )
    path = PRESET_DIR / meta["file"]
    if not path.exists():
        raise WorkflowError(f"预设文件不存在：{path}")
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"{path.name} 不是合法 JSON：{exc}") from exc
    if not isinstance(graph, dict) or not graph:
        raise WorkflowError(f"{path.name} 不像 API 格式工作流（顶层不是节点字典）")
    return graph


def _upstream(graph: dict, link) -> str:
    """连线 [节点id, 输出序号] → 节点 id。"""
    if not isinstance(link, (list, tuple)) or not link:
        raise WorkflowError(f"连线格式不对：{link!r}")
    return str(link[0])


def analyze(graph: dict) -> dict:
    """认出图里各个角色。返回节点 id / 连线，给注入参数用。"""
    samplers = [nid for nid, n in graph.items() if n.get("class_type") == "KSampler"]
    if len(samplers) != 1:
        raise WorkflowError(
            f"工作流里应当只有一个 KSampler，实际 {len(samplers)} 个 —— 认不出该改哪个"
        )
    sampler_id = samplers[0]
    ins = graph[sampler_id].get("inputs") or {}
    for key in ("positive", "negative", "latent_image", "model"):
        if key not in ins:
            raise WorkflowError(f"KSampler 缺 {key} 输入，这个工作流结构不支持")

    positive_id = _upstream(graph, ins["positive"])
    negative_id = _upstream(graph, ins["negative"])
    latent_id = _upstream(graph, ins["latent_image"])
    model_src = ins["model"]
    clip_src = (graph.get(positive_id, {}).get("inputs") or {}).get("clip")
    if not clip_src:
        raise WorkflowError("正面提示词节点没有 clip 输入，认不出文本编码器")

    # 顺着 model 链往回找加载节点（MOSS 中间还夹着 SkipLayerGuidanceDiTSimple）
    loader_id, name_input = _find_model_loader(graph, _upstream(graph, model_src))

    outputs = [
        nid
        for nid, n in graph.items()
        if n.get("class_type") in OUTPUT_CLASSES
    ]

    # 解码节点的 vae 输入 —— 图生图要用同一个 VAE 把参考图编码成 latent
    decoders = [
        nid for nid, n in graph.items()
        if n.get("class_type") in ("VAEDecode", "VAEDecodeTiled")
    ]
    vae_link = None
    if decoders:
        vae_link = (graph[decoders[0]].get("inputs") or {}).get("vae")

    return {
        "sampler": sampler_id,
        "positive": positive_id,
        "negative": negative_id,
        "latent": latent_id,
        "model_loader": loader_id,
        "model_input": name_input,
        "model_link": list(model_src),
        "clip_link": list(clip_src),
        "vae_link": list(vae_link) if vae_link else None,
        "outputs": outputs,
    }


def _find_model_loader(graph: dict, node_id: str, depth: int = 0) -> tuple[str, str]:
    """从 model 线往上走，直到找到加载节点，并返回它写模型名的输入键。"""
    if depth > 20:
        raise WorkflowError("model 链太深（可能有环），认不出底模加载节点")
    node = graph.get(node_id)
    if node is None:
        raise WorkflowError(f"model 链指向了不存在的节点 {node_id}")
    cls = node.get("class_type")
    if cls in LOADER_CLASSES:
        inputs = node.get("inputs") or {}
        for key in ("ckpt_name", "unet_name"):
            if key in inputs:
                return node_id, key
        # 兜底：第一个以 _name 结尾的输入
        for key in inputs:
            if key.endswith("_name"):
                return node_id, key
        raise WorkflowError(f"{cls} 里找不到模型名输入（ckpt_name / unet_name）")
    ins = node.get("inputs") or {}
    if "model" not in ins:
        raise WorkflowError(f"节点 {node_id}({cls}) 既不加载模型也没有 model 输入")
    return _find_model_loader(graph, _upstream(graph, ins["model"]), depth + 1)


def summarize(graph: dict) -> dict:
    """当前图里的关键参数（给 comfy_tools 显示用）。"""
    roles = analyze(graph)
    sampler = graph[roles["sampler"]]["inputs"]
    latent = (graph.get(roles["latent"]) or {}).get("inputs") or {}
    model_node = graph[roles["model_loader"]]["inputs"]
    return {
        "model": str(model_node.get(roles["model_input"]) or ""),
        "width": int(latent.get("width") or 0),
        "height": int(latent.get("height") or 0),
        "steps": int(sampler.get("steps") or 0),
        "cfg": float(sampler.get("cfg") or 0),
        "sampler_name": str(sampler.get("sampler_name") or ""),
        "scheduler": str(sampler.get("scheduler") or ""),
        "positive": str((graph[roles["positive"]]["inputs"] or {}).get("text") or ""),
        "negative": str((graph[roles["negative"]]["inputs"] or {}).get("text") or ""),
    }


def apply_params(
    graph: dict,
    *,
    prompt: str | None = None,
    negative: str | None = None,
    width: int | None = None,
    height: int | None = None,
    steps: int | None = None,
    cfg: float | None = None,
    seed: int | None = None,
    model: str | None = None,
    loras: list[dict] | None = None,
    filename_prefix: str | None = None,
    source_image: str | None = None,
    denoise: float | None = None,
) -> dict:
    """按参数改一份新图（不动传入的 dict）。

    source_image 给的是 ComfyUI input 目录里的文件名（先 /upload/image 传上去）：
      · 不给 → 就是原来的文生图，一个节点都不增删
      · 给了 → 动态插 LoadImage → ImageScale → VAEEncode，替掉空 latent，
              并把 KSampler.denoise 调成 denoise（默认 0.6）
    """
    g = copy.deepcopy(graph)
    roles = analyze(g)

    pos = g[roles["positive"]]["inputs"]
    neg = g[roles["negative"]]["inputs"]
    if prompt is not None:
        pos["text"] = str(prompt)
    if negative is not None:
        neg["text"] = str(negative)

    if source_image:
        real_w, real_h = _insert_img2img(g, roles, source_image, width, height)
        g[roles["sampler"]]["inputs"]["denoise"] = float(
            0.6 if denoise is None else max(0.05, min(1.0, denoise))
        )
        width, height = real_w, real_h
    else:
        latent = g[roles["latent"]]["inputs"]
        if width is not None:
            latent["width"] = int(width)
        if height is not None:
            latent["height"] = int(height)

    sampler = g[roles["sampler"]]["inputs"]
    if steps is not None:
        sampler["steps"] = int(steps)
    if cfg is not None:
        sampler["cfg"] = float(cfg)
    if seed is not None:
        sampler["seed"] = int(seed)

    if model:
        g[roles["model_loader"]]["inputs"][roles["model_input"]] = str(model)

    if filename_prefix:
        for nid in roles["outputs"]:
            if "filename_prefix" in (g[nid].get("inputs") or {}):
                g[nid]["inputs"]["filename_prefix"] = str(filename_prefix)

    if loras:
        g = _insert_loras(g, roles, loras)

    return g


def _insert_img2img(
    graph: dict,
    roles: dict,
    image_name: str,
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    """把纯文生图的工作流改成图生图（不改磁盘上的文件，只在内存里改）。

    EmptyLatent → (删掉)
    LoadImage → ImageScale → VAEEncode → KSampler.latent_image
    VAE 复用工作流里解码节点那个，所以和模型本来就配套。
    """
    if not roles.get("vae_link"):
        raise WorkflowError("工作流里找不到 VAEDecode，没法复用 VAE 做图生图")

    latent_id = roles["latent"]
    latent_inputs = graph.get(latent_id, {}).get("inputs") or {}
    w = int(width or latent_inputs.get("width") or 512)
    h = int(height or latent_inputs.get("height") or 512)

    def fresh(base: str) -> str:
        nid = base
        i = 1
        while nid in graph:
            i += 1
            nid = f"{base}_{i}"
        return nid

    load_id = fresh("ref_image")
    scale_id = fresh("ref_scale")
    encode_id = fresh("ref_encode")
    graph[load_id] = {
        "class_type": "LoadImage",
        "inputs": {"image": str(image_name)},
    }
    graph[scale_id] = {
        "class_type": "ImageScale",
        "inputs": {
            "image": [load_id, 0],
            "upscale_method": "lanczos",
            "width": w,
            "height": h,
            "crop": "disabled",
        },
    }
    graph[encode_id] = {
        "class_type": "VAEEncode",
        "inputs": {"pixels": [scale_id, 0], "vae": list(roles["vae_link"])},
    }
    graph[roles["sampler"]]["inputs"]["latent_image"] = [encode_id, 0]

    # 空 latent 已经没人引用了，删掉 —— 免得图里留个占位节点看不懂
    if latent_id in graph:
        del graph[latent_id]
    return w, h


def _insert_loras(graph: dict, roles: dict, loras: list[dict]) -> dict:
    """在 model / clip 链上串 LoraLoader。

    ★ 必须同时改三处引用：KSampler.model、正面.clip、负面.clip，
      否则模型加了 LoRA 而文本编码器没加（或反过来），风格会和预期不一样。
    """
    prev_model = list(roles["model_link"])
    prev_clip = list(roles["clip_link"])
    for i, lora in enumerate(loras, 1):
        name = str(lora.get("name") or "").strip()
        if not name:
            continue
        strength = float(lora.get("strength", 1.0))
        node_id = f"lora_{i}"
        # 防止和原图节点 id 撞车
        while node_id in graph:
            node_id += "_x"
        graph[node_id] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": prev_model,
                "clip": prev_clip,
                "lora_name": name,
                "strength_model": strength,
                "strength_clip": strength,
            },
        }
        prev_model = [node_id, 0]
        prev_clip = [node_id, 1]

    graph[roles["sampler"]]["inputs"]["model"] = prev_model
    graph[roles["positive"]]["inputs"]["clip"] = prev_clip
    graph[roles["negative"]]["inputs"]["clip"] = prev_clip
    return graph
