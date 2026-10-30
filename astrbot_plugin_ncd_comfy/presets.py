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
        "model_dir": "SDXL",
        "lora_dirs": ("sdxl",),
        "desc": "SDXL 单文件底模，最快",
    },
    "krea2": {
        "file": "krea2.json",
        "label": "KREA2",
        "eta": 30,
        "model_kind": "unet",
        "model_dir": "Krea2",
        "lora_dirs": ("Krea2-风格", "Krea2-功能"),
        "desc": "KREA2（UNET + qwen3vl 文本编码器）",
    },
    "anima": {
        "file": "anima.json",
        "label": "ANIMA",
        "eta": 40,
        "model_kind": "unet",
        "model_dir": "anima",
        "lora_dirs": ("anima", "anima-bsk"),
        "desc": "Anima（UNET + Qwen3-0.6B 文本编码器）",
    },
    "moss": {
        "file": "moss.json",
        "label": "MOSS",
        "eta": 60,
        "model_kind": "unet",
        "model_dir": "MOSS",
        "lora_dirs": ("Moss",),
        "desc": "MOSS-Image（50 步，最慢但细节最好）",
    },
}

DEFAULT_PRESET = "sdxl"

# ---- ControlNet / 控制适配器 -------------------------------------------------
# SDXL：一个 union 模型覆盖多种类型（ComfyUI 的 SetUnionControlNetType 选类型）
SDXL_CONTROLNET = "controlnet-union-sdxl-1.0.safetensors"
SDXL_UNION_TYPES = {
    "auto": "auto",
    "pose": "openpose",
    "openpose": "openpose",
    "depth": "depth",
    "scribble": "hed/pidi/scribble/ted",
    "hed": "hed/pidi/scribble/ted",
    "canny": "canny/lineart/anime_lineart/mlsd",
    "lineart": "canny/lineart/anime_lineart/mlsd",
    "anime_lineart": "canny/lineart/anime_lineart/mlsd",
    "mlsd": "canny/lineart/anime_lineart/mlsd",
    "normal": "normal",
    "segment": "segment",
    "tile": "tile",
    "repaint": "repaint",
}

# Anima：LLLite 模型补丁，每种控制类型一个文件（Comfy-Org/Anima-LLLite）
ANIMA_LLLITE_TYPES = {
    "depth": "anima-lllite-depth-1.safetensors",
    "lineart": "anima-lllite-lineart-1.safetensors",
    "pose": "anima-lllite-pose-1.safetensors",
    "scribble": "anima-lllite-scribble-1.safetensors",
    "inpainting": "anima-lllite-inpainting-v2.safetensors",
    "any": "anima-lllite-any-test-like-v2.safetensors",
    "any-test-like": "anima-lllite-any-test-like-v2.safetensors",
}

# MOSS：color/depth 控制适配器（LoRA 形式），接 MOSSImageControl
MOSS_CONTROL_TYPES = {
    "depth": "depth_map",
    "color": "color_map",
}
MOSS_CONTROL_LORA = "Moss\\moss_image_color_depth_control.safetensors"

# Krea2：Control LoRA（第三方节点）；depth 与社区 pose 权重
KREA2_CONTROL_TYPES = {
    "depth": "Krea2-功能\\depth-control-lora.safetensors",
    "pose": "Krea2-功能\\krea2-pose-control-mix025.safetensors",
    "openpose": "Krea2-功能\\krea2-pose-control-mix025.safetensors",
}

#: 已经接线控制能力的预设。
CONTROL_PRESETS = ("sdxl", "anima", "moss", "krea2")

# 自动预处理：普通照片 → 控制图（交给 comfyui_controlnet_aux）
# 不用预处理的情况（tile / repaint / auto / inpainting / any）不在表里。
CONTROL_PREPROCESSORS = {
    "sdxl": {
        "depth": ("DepthAnythingV2Preprocessor", {"ckpt_name": "depth_anything_v2_vitl.pth", "resolution": 1024}),
        "pose": ("OpenposePreprocessor", {"detect_hand": "enable", "detect_body": "enable", "detect_face": "enable", "resolution": 1024}),
        "openpose": ("OpenposePreprocessor", {"detect_hand": "enable", "detect_body": "enable", "detect_face": "enable", "resolution": 1024}),
        "canny": ("CannyEdgePreprocessor", {"low_threshold": 100, "high_threshold": 200, "resolution": 1024}),
        "lineart": ("LineArtPreprocessor", {"coarse": "disable", "resolution": 1024}),
        "anime_lineart": ("AnimeLineArtPreprocessor", {"resolution": 1024}),
        "scribble": ("ScribblePreprocessor", {"resolution": 1024}),
        "normal": ("BAE-NormalMapPreprocessor", {"resolution": 1024}),
        "segment": ("UniFormer-SemSegPreprocessor", {"resolution": 1024}),
    },
    "anima": {
        "depth": ("DepthAnythingV2Preprocessor", {"ckpt_name": "depth_anything_v2_vitl.pth", "resolution": 1024}),
        "pose": ("OpenposePreprocessor", {"detect_hand": "enable", "detect_body": "enable", "detect_face": "enable", "resolution": 1024}),
        "lineart": ("AnimeLineArtPreprocessor", {"resolution": 1024}),
        "scribble": ("ScribblePreprocessor", {"resolution": 1024}),
    },
    "moss": {
        "depth": ("DepthAnythingV2Preprocessor", {"ckpt_name": "depth_anything_v2_vitl.pth", "resolution": 1024}),
        "color": ("ColorPreprocessor", {"resolution": 1024}),
    },
    "krea2": {
        "depth": ("DepthAnythingV2Preprocessor", {"ckpt_name": "depth_anything_v2_vitl.pth", "resolution": 1024}),
        "pose": ("OpenposePreprocessor", {"detect_hand": "enable", "detect_body": "enable", "detect_face": "enable", "resolution": 1024}),
        "openpose": ("OpenposePreprocessor", {"detect_hand": "enable", "detect_body": "enable", "detect_face": "enable", "resolution": 1024}),
    },
}




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
    preset: str | None = None,
    control_image: str | None = None,
    control_type: str | None = None,
    control_strength: float = 0.8,
    control_start: float = 0.0,
    control_end: float = 1.0,
    control_preprocess: bool = True,
    upscale_model: str | None = None,
    upscale_factor: float = 0.0,
    upscale_method: str = "lanczos",
    upscale_max_side: int = 0,
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

    if control_image:
        g = _insert_controlnet(
            g,
            roles,
            preset=str(preset or ""),
            image_name=str(control_image),
            control_type=str(control_type or ""),
            strength=float(control_strength),
            start=float(control_start),
            end=float(control_end),
            width=width,
            height=height,
            preprocess=bool(control_preprocess),
        )

    # ★ 2026-10-30：可选放大（超分）。四个工作流本身是精简版、没有超分节点，
    #   这里在 VAEDecode 之后、输出之前动态接：
    #     [放大模型 UpscaleModelLoader+ImageUpscaleWithModel] → ImageScale(精确目标尺寸)
    #   upscale_factor <= 1 表示不放大，一个节点都不加。
    if upscale_factor and float(upscale_factor) > 1.0:
        g = _insert_upscale(
            g,
            roles,
            width=int(width or 0),
            height=int(height or 0),
            factor=float(upscale_factor),
            model_name=str(upscale_model or ""),
            method=str(upscale_method or "lanczos"),
            max_side=int(upscale_max_side or 0),
        )

    return g


def _insert_upscale(
    graph: dict,
    roles: dict,
    *,
    width: int,
    height: int,
    factor: float,
    model_name: str = "",
    method: str = "lanczos",
    max_side: int = 0,
) -> dict:
    """在 VAEDecode 之后接一段放大。

    两种用法：
      · 只有 factor（比如 2.0）→ 直接 ImageScale 用 lanczos 放大到目标尺寸
      · 给了 model_name（如 4x-UltraSharp.pth）→ 先模型放大，再用 ImageScale
        精确缩到目标尺寸（4x 模型想要 2x 就靠这一步降下来）

    目标尺寸按 base × factor 算，单边不超过 max_side（<=0 表示不限），
    并对齐到 8 的倍数（ComfyUI 的常规要求）。
    """
    if width <= 0 or height <= 0:
        return graph

    decode_id = next(
        (nid for nid, n in graph.items() if isinstance(n, dict)
         and n.get("class_type") == "VAEDecode"),
        None,
    )
    if not decode_id:
        return graph

    outs = [
        nid for nid, n in graph.items()
        if isinstance(n, dict)
        and n.get("class_type") in ("SaveImage", "PreviewImage")
        and any(
            isinstance(v, list) and v and v[0] == decode_id
            for v in (n.get("inputs") or {}).values()
        )
    ]
    if not outs:
        return graph

    prev = [decode_id, 0]
    if model_name:
        graph["upscale_model"] = {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": model_name},
        }
        graph["upscale_by_model"] = {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {"upscale_model": ["upscale_model", 0], "image": prev},
        }
        prev = ["upscale_by_model", 0]

    tw = int(round(width * factor))
    th = int(round(height * factor))
    if max_side and max(int(tw), int(th)) > int(max_side):
        k = float(max_side) / max(tw, th)
        tw, th = int(round(tw * k)), int(round(th * k))
    tw = max(64, (tw // 8) * 8)
    th = max(64, (th // 8) * 8)

    graph["upscale_resize"] = {
        "class_type": "ImageScale",
        "inputs": {
            "image": prev,
            "upscale_method": method,
            "width": tw,
            "height": th,
            "crop": "disabled",
        },
    }
    prev = ["upscale_resize", 0]

    for oid in outs:
        for k, v in (graph[oid].get("inputs") or {}).items():
            if isinstance(v, list) and v and v[0] == decode_id:
                graph[oid]["inputs"][k] = prev
    return graph


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


def _insert_controlnet(
    graph: dict,
    roles: dict,
    *,
    preset: str,
    image_name: str,
    control_type: str,
    strength: float,
    start: float,
    end: float,
    width: int | None,
    height: int | None,
    preprocess: bool = True,
) -> dict:
    """插入 ControlNet / LLLite 控制链（只改内存图）。

    sdxl  ：ControlNetLoader → SetUnionControlNetType → ControlNetApplyAdvanced
    anima ：ModelPatchLoader → AnimaLLLiteApply（挂在 model 链末端）
    """
    if preset not in CONTROL_PRESETS:
        raise WorkflowError(
            f"{preset or '当前预设'} 的 ControlNet 还没接线（当前支持："
            + "、".join(CONTROL_PRESETS)
            + "）"
        )
    if not image_name:
        raise WorkflowError("control 图没有上传成功")

    def fresh(base: str) -> str:
        nid = base
        i = 1
        while nid in graph:
            i += 1
            nid = f"{base}_{i}"
        return nid

    # 控制图统一缩放到出图尺寸（ControlNet 与 latent 对齐）
    ctype = str(control_type or "").strip().lower()
    latent_inputs = (graph.get(roles.get("latent", "")) or {}).get("inputs") or {}
    w = int(width or latent_inputs.get("width") or 0)
    h = int(height or latent_inputs.get("height") or 0)
    load_id = fresh("ctrl_image")
    graph[load_id] = {
        "class_type": "LoadImage",
        "inputs": {"image": str(image_name)},
    }
    image_link: list = [load_id, 0]
    # 普通照片自动预处理成深度/线稿/骨架等（controlnet_aux）
    if preprocess:
        spec = (CONTROL_PREPROCESSORS.get(preset) or {}).get(ctype)
        if spec:
            pre_cls, pre_params = spec
            pre_id = fresh("ctrl_pre")
            pre_inputs = {"image": image_link}
            pre_inputs.update(pre_params)
            graph[pre_id] = {"class_type": pre_cls, "inputs": pre_inputs}
            image_link = [pre_id, 0]
    if w > 0 and h > 0:
        scale_id = fresh("ctrl_scale")
        graph[scale_id] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": image_link,
                "upscale_method": "lanczos",
                "width": w,
                "height": h,
                "crop": "center",
            },
        }
        image_link = [scale_id, 0]

    if preset == "sdxl":
        union_type = SDXL_UNION_TYPES.get(ctype or "auto")
        if not union_type:
            raise WorkflowError(
                f"SDXL 不支持控制类型「{control_type}」。可用："
                + "、".join(sorted(SDXL_UNION_TYPES))
            )
        net_id = fresh("ctrl_net")
        graph[net_id] = {
            "class_type": "ControlNetLoader",
            "inputs": {"control_net_name": SDXL_CONTROLNET},
        }
        type_id = fresh("ctrl_type")
        graph[type_id] = {
            "class_type": "SetUnionControlNetType",
            "inputs": {"control_net": [net_id, 0], "type": union_type},
        }
        apply_id = fresh("ctrl_apply")
        inputs = {
            "positive": [roles["positive"], 0],
            "negative": [roles["negative"], 0],
            "control_net": [type_id, 0],
            "image": image_link,
            "strength": float(strength),
            "start_percent": float(start),
            "end_percent": float(end),
        }
        if roles.get("vae_link"):
            inputs["vae"] = list(roles["vae_link"])
        graph[apply_id] = {"class_type": "ControlNetApplyAdvanced", "inputs": inputs}
        graph[roles["sampler"]]["inputs"]["positive"] = [apply_id, 0]
        graph[roles["sampler"]]["inputs"]["negative"] = [apply_id, 1]
        return graph

    if preset == "moss":
        map_key = MOSS_CONTROL_TYPES.get(ctype)
        if not map_key:
            raise WorkflowError(
                f"MOSS 不支持控制类型「{control_type}」。可用："
                + "、".join(sorted(MOSS_CONTROL_TYPES))
            )
        if not roles.get("vae_link"):
            raise WorkflowError("MOSS 工作流里找不到 VAE，没法做 color/depth 控制")
        adapter_id = fresh("ctrl_adapter")
        graph[adapter_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": list(graph[roles["sampler"]]["inputs"]["model"]),
                "lora_name": MOSS_CONTROL_LORA,
                "strength_model": 1.0,
            },
        }
        graph[roles["sampler"]]["inputs"]["model"] = [adapter_id, 0]
        apply_id = fresh("ctrl_apply")
        inputs = {
            "positive": [roles["positive"], 0],
            "negative": [roles["negative"], 0],
            "vae": list(roles["vae_link"]),
            "strength": float(strength),
            "start_percent": float(start),
            "end_percent": float(end),
            map_key: image_link,
        }
        graph[apply_id] = {"class_type": "MOSSImageControl", "inputs": inputs}
        graph[roles["sampler"]]["inputs"]["positive"] = [apply_id, 0]
        graph[roles["sampler"]]["inputs"]["negative"] = [apply_id, 1]
        return graph

    if preset == "krea2":
        lora_name = KREA2_CONTROL_TYPES.get(ctype or "depth")
        if not lora_name:
            raise WorkflowError(
                f"Krea2 不支持控制类型「{control_type}」。可用："
                + "、".join(sorted(KREA2_CONTROL_TYPES))
            )
        if not roles.get("vae_link"):
            raise WorkflowError("Krea2 工作流里找不到 VAE，没法编码控制图")
        lora_id = fresh("ctrl_lora")
        graph[lora_id] = {
            "class_type": "Krea2ControlLoRALoader",
            "inputs": {
                "model": list(graph[roles["sampler"]]["inputs"]["model"]),
                "lora_name": lora_name,
                "strength": 1.0,
            },
        }
        enc_id = fresh("ctrl_encode")
        enc_inputs = {
            "control_image": image_link,
            "vae": list(roles["vae_link"]),
            "resize": "match_latent_size",
            "upscale_method": "lanczos",
            "crop": "center",
            "channel_mode": "grayscale" if (ctype or "depth") == "depth" else "rgb",
            "normalize": "per_image_minmax" if (ctype or "depth") == "depth" else "none",
            "invert": False,
            "batch_mode": "independent_images",
        }
        latent_link = graph[roles["sampler"]]["inputs"].get("latent_image")
        if latent_link:
            enc_inputs["latent"] = list(latent_link)
        graph[enc_id] = {
            "class_type": "Krea2ControlImageEncode",
            "inputs": enc_inputs,
        }
        apply_id = fresh("ctrl_apply")
        graph[apply_id] = {
            "class_type": "Krea2ControlApply",
            "inputs": {
                "model": [lora_id, 0],
                "control_latent": [enc_id, 0],
            },
        }
        graph[roles["sampler"]]["inputs"]["model"] = [apply_id, 0]
        return graph

    # anima
    lllite = ANIMA_LLLITE_TYPES.get(ctype)
    if not lllite:
        raise WorkflowError(
            f"Anima 不支持控制类型「{control_type}」。可用："
            + "、".join(sorted(ANIMA_LLLITE_TYPES))
        )
    patch_id = fresh("ctrl_patch")
    graph[patch_id] = {
        "class_type": "ModelPatchLoader",
        "inputs": {"name": lllite},
    }
    apply_id = fresh("ctrl_apply")
    graph[apply_id] = {
        "class_type": "AnimaLLLiteApply",
        "inputs": {
            "model": list(graph[roles["sampler"]]["inputs"]["model"]),
            "model_patch": [patch_id, 0],
            "image": image_link,
            "strength": float(strength),
            "start_percent": float(start),
            "end_percent": float(end),
        },
    }
    graph[roles["sampler"]]["inputs"]["model"] = [apply_id, 0]
    return graph
