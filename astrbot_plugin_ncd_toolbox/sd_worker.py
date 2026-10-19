# -*- coding: utf-8 -*-
"""独立的 SDXL 文生图 worker（纯 diffusers，用一个自带 torch 的独立 python 环境）。

为什么是"常驻到空闲超时"而不是纯一次性：
  模型加载要 2~10 秒（冷/热盘差异很大），每张图都重载太浪费；
  但一直挂着又会白占 2 GB 显存、跟 LM Studio 抢。
  所以：由插件按需拉起 → 连续用的时候复用 → 空闲 N 秒（默认 300）自己退出。

接口（只监听 127.0.0.1，不做鉴权，所以千万别改 --host 成 0.0.0.0）：
    GET  /health
        → {"ok": true, "checkpoint": "...", "idle_timeout": 300}
    POST /txt2img  {"prompt","negative","width","height","steps","cfg","seed"}
        → image/png 原始字节；响应头 X-Seed 是本次实际用的种子

参数对照 QQbotAI 工作流：
    CheckpointLoaderSimple  → --checkpoint
    Positive / Negative     → prompt / negative
    EmptyLatentImage        → width / height
    KSampler dpmpp_2m karras → DPMSolverMultistepScheduler(use_karras_sigmas=True)
    steps 25 / cfg 7.0 / denoise 1.0 → 同名参数
    VAEDecode + SaveImage   → 直接回 PNG 字节

单独跑（调试用）：
    <独立环境>\\Scripts\\python.exe sd_worker.py ^
        --checkpoint "<你的 safetensors>" --port 8199 --idle-timeout 300
    然后  curl http://127.0.0.1:8199/health
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ==== 网络：必须在 import diffusers 之前设好 ====
# ★ SDXL 的 from_single_file 要拉"管线配置"（单文件里没有 config.json）。
#   本机直连 huggingface.co 会 SSL 证书校验失败（实测 CERTIFICATE_VERIFY_FAILED），
#   而 hf-mirror.com 用正常证书校验能通 —— 所以默认走镜像。
#   想直连/走代理的，自己设 HF_ENDPOINT 覆盖即可（这里用 setdefault，不覆盖已设的）。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

_state: dict = {
    "pipe": None,
    "checkpoint": "",
    "idle_timeout": 300.0,
    "last_hit": time.time(),
    "lock": threading.Lock(),
    "device": "cuda",
    # LoRA：加载成功才填，推理时按 lora_scale 缩放（不重新加载权重）
    "lora": "",
    "lora_scale": 1.0,
    # 架构与显存模式（load_pipeline 填，/health 报）
    "arch": "",
    "offload": False,
    # 加载进度（给 /health 与 /progress 用）
    "ready": False,
    "switching": False,
    "phase": "启动中",
    "phase_pct": 0,
    # 启动参数（切换底模时要用同一套：换模型不该悄悄换掉 offload 策略）
    "arch_mode": "auto",
    "offload_mode": "auto",
    # 采样器：euler（欧拉 + 普通，默认）/ dpmpp2m_karras
    "sampler": "euler",
    "sched_cfg": {},
    # 出图进度（给前端画进度条用）
    "busy": False,
    "step": 0,
    "total": 0,
    "started": 0.0,
}

DEFAULT_NEGATIVE = (
    "lowres, blurry, out of focus, jpeg artifacts, deformed, bad anatomy, "
    "extra limbs, mutated, watermark, text, logo, signature"
)
"""默认负面提示词（可被配置 sd_negative 覆盖）。

★ 2026-10-18 修：原来开头是 `color, colored` —— 那是给「黑白/线稿风格」
  LoRA 配的，放在全局默认里会系统性把出图往灰/低饱和推。
  不挂 LoRA、或挂了彩色风格 LoRA 时，这是净损失。想压颜色请自己配 sd_negative。
"""


def _align_side(value, default: int = 512, lo: int = 64, hi: int = 1920) -> int:
    """把宽/高对齐到 8 的倍数，并夹在 [lo, hi] 区间。

    ★ 为什么必须做：diffusers 要求 8 的倍数，否则直接抛
      "height and width have to be divisible by 8"；而 2048² 会直接 OOM。
      页面上这两个数是人手填的，填 500 / 2048 太容易了。
    ★ img2img 那条路早就按参考图自己对齐了，这里补上 txt2img。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    if n <= 0:          # 0 / 负数当成"没填"，回落到默认值（而不是被夹成最小边）
        n = default
    n = max(lo, min(hi, n))
    return n // 8 * 8


def set_phase(name: str, pct: int) -> None:
    """更新"加载到哪一步了"。只给 /health 与 /progress 读，不影响出图。"""
    _state["phase"] = name
    _state["phase_pct"] = max(0, min(100, int(pct)))
    print(f"[sd-worker] 加载进度 {_state['phase_pct']}% · {name}", flush=True)


SAMPLERS = {
    "euler": "欧拉 + 普通",
    "dpmpp2m_karras": "DPM++ 2M + Karras",
}
"""可选的采样器/调度器组合。

★ 为什么只有两档：能看懂/能解释的选择才有意义。多列一堆（euler_a、heun、
  dpmpp_3m、unipc…）对出图质量的差别远小于提示词和底模，反而让人纠结。
★ euler + 普通（线性 timestep）是 2026-10-18 新增的默认：它在 SDXL 上更"老实"
  —— 不预放大、不锐化，细节交给底模和提示词；Karras 那套会系统性地偏锐。
"""


def normalize_sampler(name: str) -> str:
    """把用户/模型写的名字收敛到 SAMPLERS 的键。认不出就回落到 euler。"""
    key = str(name or "").strip().lower().replace("+", "p").replace("-", "_").replace(" ", "")
    if key in SAMPLERS:
        return key
    if key in ("欧拉", "欧拉普通", "euler_normal", "eulernormal", "normal"):
        return "euler"
    if key in ("欧拉a", "euler_a", "eulera"):
        # 不做单独的 euler_a：它和"欧拉+普通"只差噪声调度，列出来只会让人纠结
        return "euler"
    if key in ("dpmpp2m", "dpmpp_2m", "dpm", "karras", "dpmpp2mkarras", "dpmpp2mk"):
        return "dpmpp2m_karras"
    return "euler"


def make_scheduler(name: str, base_cfg: dict):
    """按名字造调度器。base_cfg 是底模自带的 scheduler.config（beta 曲线等来自它）。

    ★ 必须显式传 use_karras_sigmas / timestep_spacing：DPMSolver 的 config 里
      带着 use_karras_sigmas=True，from_config 会把它带给 Euler（实测确认），
      于是"欧拉 + 普通"会悄悄变成"欧拉 + Karras"。
    """
    from diffusers import DPMSolverMultistepScheduler, EulerDiscreteScheduler

    key = normalize_sampler(name)
    cfg = dict(base_cfg or {})
    # 去掉属于别的调度器的私有键，免得 from_config 一直刷 warning
    for k in ("algorithm_type", "solver_order", "solver_type", "lower_order_final",
              "use_lu_lambdas", "final_sigmas_type", "euler_at_final", "solver_p"):
        cfg.pop(k, None)
    if key == "dpmpp2m_karras":
        return DPMSolverMultistepScheduler.from_config(cfg, use_karras_sigmas=True)
    return EulerDiscreteScheduler.from_config(
        cfg, use_karras_sigmas=False, timestep_spacing="linspace"
    )


def apply_sampler(name: str) -> None:
    """切换采样器（不重新加载权重 —— 只是换一个调度器对象，毫秒级）。"""
    key = normalize_sampler(name)
    pipe = _state.get("pipe")
    if pipe is None or key == _state.get("sampler"):
        _state["sampler"] = key
        return
    cfg = _state.get("sched_cfg") or {}
    if not cfg:
        _state["sampler"] = key
        return
    # 两条管道各给一个独立的调度器实例：调度器内部会在 set_timesteps 时存状态，
    # 共用同一个对象虽然串行不会错，但一旦将来放开并发就是隐患。
    for slot in ("pipe", "pipe2"):
        target = _state.get(slot)
        if target is None:
            continue
        try:
            target.scheduler = make_scheduler(key, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"[sd-worker] 换采样器失败（{slot}）: {exc}", flush=True)
            return
    _state["sampler"] = key
    print(f"[sd-worker] 采样器 = {key}（{SAMPLERS.get(key, key)}）", flush=True)


def apply_loras(picked: list[dict]) -> None:
    """按请求切换 LoRA：先全卸，再装这次的（可叠加）。

    picked: [{"path": "...", "scale": 0.8, "name": "cunnyfunky"}, ...]
            空列表 = 不挂 LoRA。
    ★ 每次先 unload_lora_weights() 再装：实测卸载后出图与"从未挂过"逐像素一致
      （差 0.00），不会有残留影响下一张；卸载 0.13 秒、每个加载约 2 秒。
    ★ 加载失败【不致命】：只记日志、继续出图 —— 挂个 LoRA 不该让整张图失败。
    """
    pipe = _state["pipe"]
    if pipe is None:
        return
    try:
        pipe.unload_lora_weights()
    except Exception:  # noqa: BLE001
        pass  # 本来就没挂

    names: list[str] = []
    weights: list[float] = []
    for idx, item in enumerate(picked or []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        # adapter_name 只留安全字符：带空格/中文会让 diffusers 内部报错
        name = "".join(
            c if (c.isalnum() or c in "_-") else "_" for c in str(item.get("name") or f"lora{idx}")
        ) or f"lora{idx}"
        try:
            pipe.load_lora_weights(path, adapter_name=name)
        except Exception as exc:  # noqa: BLE001
            print(f"[sd-worker] LoRA 加载失败（跳过）{path}: {type(exc).__name__}: {exc}",
                  flush=True)
            continue
        names.append(name)
        weights.append(float(item.get("scale") or 0.8))

    if names:
        try:
            pipe.set_adapters(names, adapter_weights=weights)
        except Exception as exc:  # noqa: BLE001
            print(f"[sd-worker] set_adapters 失败（按默认强度用）: {exc}", flush=True)
        desc = " + ".join(f"{n}@{w}" for n, w in zip(names, weights))
        _state["lora"] = desc
        _state["lora_scale"] = None
        print(f"[sd-worker] 已切换 LoRA: {desc}", flush=True)
    else:
        _state["lora"] = ""
        _state["lora_scale"] = None
        print("[sd-worker] 本轮不挂 LoRA", flush=True)


def load_lora(lora: str, scale: float) -> None:
    """把 LoRA 挂到已加载的 pipeline 上。

    ★ 用 diffusers 的 `load_lora_weights` + 推理时传 `cross_attention_kwargs={"scale": s}`。
      `scale` 是【推理强度】，不改权重本身 —— 所以换强度不用重新加载。
    ★ 实测（2026-10-17，style_GZC / SD1.5 kohya LoRA）：无 LoRA ↔ 0.8 平均像素差 18.08，
      无 LoRA ↔ 1.0 是 19.54，0.8 ↔ 1.0 是 7.40 —— 加载成功且强度确实生效。
    ★ 失败【不致命】：只记日志、继续用无 LoRA 出图。挂个 LoRA 不该让整条链路起不来。
    """
    pipe = _state["pipe"]
    if pipe is None or not lora:
        return
    t0 = time.time()
    try:
        pipe.load_lora_weights(lora)
    except Exception as exc:  # noqa: BLE001
        print(f"[sd-worker] LoRA 加载失败（继续无 LoRA 出图）: {type(exc).__name__}: {exc}",
              flush=True)
        return
    _state["lora"] = Path(lora).name
    _state["lora_scale"] = float(scale)
    print(f"[sd-worker] LoRA 已加载: {Path(lora).name} · scale={scale:.2f} · "
          f"{time.time() - t0:.2f} 秒", flush=True)


# SDXL 的融合 checkpoint 一般 6~7 GB。
SDXL_MIN_BYTES = 3.5 * 1024**3


def detect_arch(checkpoint: str) -> str:
    """返回架构名。**本插件现在只支持 SDXL。**

    ★ 2026-10-18：SD1.5 那条支线整个删掉了 —— 画质代差太大，用户直接换到
      Illustrious 系 SDXL。这个函数留着只是为了：拿老 SD1.5 文件来配的时候
      能【明确提醒】，而不是让 diffusers 抛一堆看不懂的 shape mismatch。
    """
    try:
        size = os.path.getsize(checkpoint)
    except OSError:
        return "sdxl"
    if size < SDXL_MIN_BYTES:
        print(
            f"[sd-worker] 警告：{Path(checkpoint).name} 只有 {size / 1024**3:.2f} GB，"
            "看起来是老 SD1.5 底模 —— 本插件只支持 SDXL，加载大概率会失败。",
            flush=True,
        )
    return "sdxl"


def load_pipeline(
    checkpoint: str, device: str, arch: str = "auto", offload: str = "auto",
    sampler: str = "euler",
) -> None:
    """加载 SDXL pipeline（只支持 SDXL）。

    ★ CPU offload：把各模块留在内存、用到哪个搬哪个进显存。
      再叠 VAE 分块解码 + 注意力切片，实测峰值从约 7.6 GB 降到约 5 GB ——
      这台机器只有 12.2 GB，LM Studio 一个 4B 模型就吃 7 GB，不压没法共存。
    """
    import torch
    from diffusers import StableDiffusionXLPipeline

    arch = detect_arch(checkpoint)
    # ★ 以【这里】为准：/health 的 checkpoint 就是"现在真正加载着的那份"。
    #   main() 里也设过一次，但切换底模时只有这里会更新。
    _state["checkpoint"] = os.path.abspath(checkpoint)

    use_offload = False
    if device == "cuda":
        mode = (offload or "auto").strip().lower()
        if mode in ("on", "model"):
            use_offload = "model"
        elif mode == "sequential":
            # 逐层搬运：显存峰值 ~2~3 GB，但慢 3~5 倍。
            # 用在你【必须】同时留着 LM Studio 大模型的时候。
            use_offload = "sequential"
        elif mode == "off":
            use_offload = False
        else:
            use_offload = "model"  # auto：默认 model offload

    t0 = time.time()
    set_phase("读取权重（6.5 GB，首次可能要几十秒）", 10)
    dtype = torch.float16 if device == "cuda" else torch.float32
    pipe = StableDiffusionXLPipeline.from_single_file(checkpoint, torch_dtype=dtype)
    set_phase("装配管道组件", 55)
    # ★ 底模自带的 scheduler.config 是 beta 曲线（SDXL 是 scaled_linear）的来源。
    #   换调度器只换算法，不换这条曲线 —— 否则整体色彩/对比会偏。
    _state["sched_cfg"] = dict(pipe.scheduler.config)
    _state["sampler"] = normalize_sampler(sampler)
    pipe.scheduler = make_scheduler(_state["sampler"], _state["sched_cfg"])
    # ★ img2img 管道必须在【开 offload 之前】建好。
    #   实测踩过：在已经 enable_model_cpu_offload() 的 pipe 上再 from_pipe，
    #   然后再对 pipe2 调一次 offload —— 会重复挂 hook，请求直接卡死
    #   （客户端 400 秒超时，worker 无任何响应，日志也不报错）。
    #   现在共享模块 + 只由 pipe 挂一次 offload，pipe2 自动受益。
    _state["pipe2"] = None
    try:
        if arch == "sdxl":
            from diffusers import StableDiffusionXLImg2ImgPipeline as _I2I
        else:
            from diffusers import AutoPipelineForImage2Image as _I2I
        # ★ 用 components 构造而不是 from_pipe()：
        #   from_pipe 在这个版本上实测【复制了一份权重】（worker 内存 6.5G → 18.8G），
        #   叠上 offload 之后疯狂换页，请求永远不返回。
        #   **pipe.components 只是把同一批模块对象的引用交给新壳，不复制。
        comps = {k: v for k, v in pipe.components.items() if v is not None}
        _state["pipe2"] = _I2I(**comps)
        _state["pipe2"].set_progress_bar_config(disable=True)
        print("[sd-worker] img2img 管道已建好（共享权重，不开二次 offload）", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[sd-worker] img2img 管道建立失败（参考图将不可用）: {exc}", flush=True)

    set_phase("准备 img2img 管道", 70)
    if use_offload == "sequential":
        # ★ 开了 offload 就【不能】再 .to(device)：两者会打架。
        pipe.enable_sequential_cpu_offload()
    elif use_offload:
        pipe.enable_model_cpu_offload()
    else:
        pipe = pipe.to(device)
    # ★ SDXL 再压一层峰值：VAE 分块解码 + 注意力切片。
    #   实测 720x1024 的显存峰值从 7.6 GB 降到约 5 GB ——
    #   这台机器只有 12.2 GB，LM Studio 一个 4B 模型就 7 GB，不压根本共存不了。
    #   代价是稍微慢一点（本来 offload 就慢）。
    if arch == "sdxl" and device == "cuda":
        # ★ 这个 diffusers 版本上 VAE tiling 在 pipe.vae 上，不在 pipe 上
        #   （实测 AttributeError: 'StableDiffusionXLPipeline' object has no attribute
        #    'enable_vae_tiling' —— 别想当然）。
        try:
            pipe.vae.enable_tiling()
        except Exception as exc:  # noqa: BLE001
            print(f"[sd-worker] vae tiling 开启失败（忽略）: {exc}", flush=True)
        try:
            pipe.enable_attention_slicing()
        except Exception as exc:  # noqa: BLE001
            print(f"[sd-worker] attention slicing 开启失败（忽略）: {exc}", flush=True)
    set_phase("搬运到显存 / 接上 offload", 85)
    pipe.set_progress_bar_config(disable=True)
    _state["arch"] = arch
    _state["offload"] = use_offload or False
    # ★ 纯 CPU 时开 attention slicing：显存/内存的峰值会明显降下来。
    #   代价是慢一点（本来 CPU 就慢）。只在非 cuda 时开，GPU 上不需要。
    if device != "cuda":
        try:
            pipe.enable_attention_slicing()
            print("[sd-worker] 已开启 attention slicing（降低内存峰值）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[sd-worker] attention slicing 开启失败（忽略）: {exc}", flush=True)
    # 空转一遍不让 GPU 等到第一张图才初始化
    _state["pipe"] = pipe
    _state["ready"] = True
    set_phase("就绪", 100)
    print(f"[sd-worker] 模型加载完成，{time.time() - t0:.2f} 秒", flush=True)


def current_checkpoint() -> str:
    """现在真正加载着的底模（绝对路径）。没加载成功就是空串。"""
    return str(_state.get("checkpoint") or "") if _state.get("ready") else ""


def switch_checkpoint(path: str) -> tuple[bool, float]:
    """运行时换底模。返回 (是否真的换了, 耗时秒)；同一个模型再切 = (False, 0)。

    ★ 为什么必须"先卸再装"：一个 SDXL 底模 6.5 GB，两份同时在场会直接把内存
      打满（img2img 那次 from_pipe 复制权重就是 6.5G→18.8G 然后疯狂换页）。
      所以先把旧 pipe 置空、gc、清显存，再加载新的。
    ★ 和出图共用同一把锁：换底模期间绝不能有另一个线程在跑 pipeline。
    ★ 纯函数式的替代方案（两套 pipe 常驻显存）在这里不成立 —— 12 GB 显存
      装两个 SDXL 就爆了，而且用户大多数时候只用一套。
    """
    want = os.path.abspath(str(path or "").strip())
    if not want:
        raise ValueError("没给底模路径")
    if not os.path.isfile(want):
        raise ValueError(f"底模文件不存在: {want}")

    def _already() -> bool:
        cur = str(_state.get("checkpoint") or "")
        return bool(cur) and os.path.abspath(cur) == want and bool(_state.get("ready"))

    if _already():
        return False, 0.0

    with _state["lock"]:
        # 等锁期间可能已经被别的请求换好了，再判一次
        if _already():
            return False, 0.0
        t0 = time.time()
        _state["switching"] = True
        _state["ready"] = False
        set_phase(f"切换底模 {Path(want).name}", 5)
        _state["pipe"] = None
        _state["pipe2"] = None
        _state["lora"] = ""
        _state["lora_scale"] = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as exc:  # noqa: BLE001
            print(f"[sd-worker] 换模型前清显存失败（忽略）: {exc}", flush=True)
        try:
            load_pipeline(
                want,
                _state["device"],
                _state.get("arch_mode") or "auto",
                _state.get("offload_mode") or "auto",
                _state.get("sampler") or "euler",
            )
        finally:
            _state["switching"] = False
    cost = time.time() - t0
    print(f"[sd-worker] 已切换底模 {Path(want).name}（{cost:.1f} 秒）", flush=True)
    return True, cost


def _lora_kwargs() -> dict:
    """推理时要传的 LoRA 参数。没挂 LoRA 就返回空 dict（调用处直接 ** 展开）。

    ★ 强度已经在 apply_loras() 里用 set_adapters(adapter_weights=...) 定好了，
      这里【必须】返回空 dict。早期版本在这里又传了一次 cross_attention_kwargs
      的 scale —— apply_loras 把 lora_scale 置成 0 之后，那一下会把刚装上的
      强度清零，表现成"LoRA 装了但完全没效果"，极难查。
    ★ 只有 CLI 启动时用 --lora 预加载那条路（load_lora）才用 cross_attention_kwargs。
    """
    scale = _state.get("lora_scale")
    if not _state.get("lora") or scale is None:
        return {}
    return {"cross_attention_kwargs": {"scale": float(scale)}}


def _encode(out) -> bytes:
    buf = io.BytesIO()
    out.images[0].save(buf, format="PNG")
    return buf.getvalue()


def _run_pipe(call, total: int):
    """带串行锁 + 步进度的跑一次 pipeline 调用。

    `call(torch, on_step_end)` 由调用方决定传什么参数 —— txt2img 和 img2img
    的参数集不一样（img2img 收 image/strength，且**没有** width/height），
    所以这里不假装统一。

    `total` 是【实际会跑几步】，进度条按它算才不会卡在 60% 不动：
    img2img 只跑 `num_inference_steps * strength` 步。
    """
    import torch

    def on_step_end(pipe, step, _timestep, callback_kwargs):
        _state["step"] = int(step) + 1
        return callback_kwargs

    _state.update(
        {
            "busy": True,
            "step": 0,
            "total": int(total),
            "started": time.time(),
            # ★ 报进度时要能解释"为什么只跑 17 步"：图生图只重绘
            #   steps×strength 步，把"本来要跑多少步"一起报出去。
            "steps_full": int(_state.get("steps_full") or total),
        }
    )
    try:
        with _state["lock"]:
            return call(torch, on_step_end)
    finally:
        _state["busy"] = False


def _render(payload: dict) -> tuple[bytes, int]:
    pipe = _state["pipe"]
    if pipe is None:
        raise RuntimeError("模型还没加载好")

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    negative = str(payload.get("negative") or DEFAULT_NEGATIVE)
    # ★ 上限从 --max-side 来（默认 1920）：换张显存更大的卡，插件配置里改
    #   sd_max_side 就能画更大，不用改代码。
    cap = int(_state.get("max_side") or 1920)
    width = _align_side(payload.get("width"), default=512, hi=cap)
    height = _align_side(payload.get("height"), default=512, hi=cap)
    steps = int(payload.get("steps") or 25)
    cfg = float(payload.get("cfg") or 7.0)
    seed = payload.get("seed")
    seed = int(seed) if seed is not None else random.randint(1, 2**48)

    device = _state["device"]

    def call(torch, on_step):
        return pipe(
            prompt=prompt,
            negative_prompt=negative,
            width=width,
            height=height,
            num_inference_steps=steps,
            guidance_scale=cfg,
            generator=torch.Generator(device=device).manual_seed(seed),
            callback_on_step_end=on_step,
            callback_on_step_end_tensor_inputs=[],
            **_lora_kwargs(),
        )

    apply_sampler(payload.get("sampler") or _state.get("sampler") or "euler")
    apply_loras(payload.get("loras") or [])
    _state["mode"] = "txt2img"
    _state["steps_full"] = steps
    out = _run_pipe(call, steps)
    return _encode(out), seed


def _img2img_pipe():
    """取 img2img 管道（在 load_pipeline 里就建好了，这里只做检查）。

    ★ 为什么不在这里懒建：在【已经 offload】的 pipe 上 from_pipe 再 offload 一次
      会重复挂 hook，请求直接卡死（实测客户端超时、worker 无响应、日志无报错）。
      所以统一在开 offload 之前建好、共享模块、不二次 offload。
    """
    if _state["pipe"] is None:
        raise RuntimeError("模型还没加载好")
    pipe2 = _state.get("pipe2")
    if pipe2 is None:
        raise RuntimeError(
            "img2img 管道没建起来（多半是加载时那条 from_pipe 失败了）"
            "—— 参考图生图不可用，改用文生图"
        )
    return pipe2


def _render_img2img(payload: dict) -> tuple[bytes, int]:
    """参考图生图。

    ★ img2img 的 `__call__` 没有 width/height（实测签名确认），
      输出尺寸跟随输入图。所以这里不传，而是把输入图裁到 8 的倍数 ——
      diffusers 内部按 8 下采样，非 8 倍数会报错或出怪图。
    ★ strength 越大越偏离原图。0.35~0.5 保构图，0.6~0.75 换风格。
    """
    import base64

    from PIL import Image

    pipe = _img2img_pipe()

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")

    raw = str(payload.get("image") or "").strip()
    if not raw:
        raise ValueError("参考图不能为空（image 传 base64 或 data URL）")
    if raw.startswith("data:") and "," in raw[:80]:
        raw = raw.split(",", 1)[1]
    try:
        img = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"参考图解码失败（要 base64 的 PNG/JPEG）: {exc}") from exc

    w, h = img.size
    w8, h8 = max(64, w // 8 * 8), max(64, h // 8 * 8)
    if (w, h) != (w8, h8):
        img = img.resize((w8, h8), Image.LANCZOS)

    strength = float(payload.get("strength") or 0.6)
    if not 0.0 < strength <= 1.0:
        raise ValueError("strength 要在 (0, 1] 之间")
    steps = int(payload.get("steps") or 25)
    cfg = float(payload.get("cfg") or 7.0)
    negative = str(payload.get("negative") or DEFAULT_NEGATIVE)
    seed = payload.get("seed")
    seed = int(seed) if seed is not None else random.randint(1, 2**48)
    device = _state["device"]

    def call(torch, on_step):
        return pipe(
            prompt=prompt,
            negative_prompt=negative,
            image=img,
            strength=strength,
            num_inference_steps=steps,
            guidance_scale=cfg,
            generator=torch.Generator(device=device).manual_seed(seed),
            callback_on_step_end=on_step,
            callback_on_step_end_tensor_inputs=[],
            **_lora_kwargs(),
        )

    # diffusers 内部 init_timestep = min(int(steps * strength), steps)，
    # 实际只跑这么多步。
    # ★ 别想用 len(pipe.scheduler.timesteps) 来"精确"校正 —— 实测 20 步 × 0.55
    #   的时候 scheduler.timesteps 仍然是完整的 20：diffusers 是【跳过前面的
    #   时间步】而不是把 timesteps 截短，所以那个长度不是实际执行步数。
    #   用 steps*strength 这个估算才是对的（实测 step 正好走到 11）。
    effective = max(1, min(int(steps * strength), steps))
    apply_sampler(payload.get("sampler") or _state.get("sampler") or "euler")
    apply_loras(payload.get("loras") or [])
    _state["mode"] = "img2img"
    _state["steps_full"] = steps
    out = _run_pipe(call, effective)
    return _encode(out), seed


class Handler(BaseHTTPRequestHandler):
    server_version = "sd-worker/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 默认会往 stderr 打每一条请求；插件是按需拉起它的，日志留给我们自己
        return

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/progress":
            # ★ 故意不更新 last_hit：这是只读观察，不能让"页面开着轮询"
            #   把 worker 无限续命（那样显存就一直不回）。
            elapsed = (
                time.time() - _state["started"] if _state["busy"] and _state["started"] else 0.0
            )
            self._json(
                200,
                {
                    "ok": True,
                    "busy": bool(_state["busy"]),
                    "ready": bool(_state.get("ready")),
                "phase": _state.get("phase") or "",
                "phase_pct": int(_state.get("phase_pct") or 0),
                "step": int(_state["step"]),
                    "total": int(_state["total"]),
                    # ★ 图生图只跑 steps×strength 步：把原步数和模式带出去，
                    #   聊天里就能解释"为什么是 17 步而不是 25"。
                    "steps_full": int(_state.get("steps_full") or _state["total"]),
                    "mode": _state.get("mode") or "",
                    "elapsed": round(elapsed, 1),
                },
            )
            return

        if path != "/health":
            self._json(404, {"ok": False, "error": "not found"})
            return
        _state["last_hit"] = time.time()
        self._json(
            200,
            {
                "ok": True,
                "checkpoint": _state["checkpoint"],
                "idle_timeout": _state["idle_timeout"],
                "device": _state["device"],
                "busy": bool(_state["busy"]),
                "ready": bool(_state.get("ready")),
                "switching": bool(_state.get("switching")),
                "phase": _state.get("phase") or "",
                "phase_pct": int(_state.get("phase_pct") or 0),
                "arch": _state.get("arch") or "",
                "offload": bool(_state.get("offload")),
                "lora": _state.get("lora") or "",
                "lora_scale": float(_state.get("lora_scale") or 0.0),
                "sampler": _state.get("sampler") or "",
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path not in ("/txt2img", "/img2img", "/load"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        _state["last_hit"] = time.time()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            if path == "/load":
                # ★ 预先换模型（switch_model 工具走这条）：这样"换"这一步的
                #   16 秒是当场付掉的，后面第一张图不用再等。
                switched, cost = switch_checkpoint(str(payload.get("checkpoint") or ""))
                self._json(
                    200,
                    {
                        "ok": True,
                        "checkpoint": _state.get("checkpoint") or "",
                        "name": Path(str(_state.get("checkpoint") or "")).name,
                        "switched": bool(switched),
                        "seconds": round(cost, 2),
                    },
                )
                return
            # ★ 出图请求里带 checkpoint 时，先对账：不一样的当场换掉。
            #   这样调用方不必记得"我上次让他们换过没有"。
            want = str(payload.get("checkpoint") or "").strip()
            if want:
                switch_checkpoint(want)
            render = _render if path == "/txt2img" else _render_img2img
            png, seed = render(payload)
        except Exception as exc:  # noqa: BLE001
            print(f"[sd-worker] 生成失败: {type(exc).__name__}: {exc}", flush=True)
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("X-Seed", str(seed))
        self.end_headers()
        self.wfile.write(png)
        _state["last_hit"] = time.time()
        print(f"[sd-worker] 出图完成 {len(png):,} 字节 seed={seed}", flush=True)


def _idle_watchdog() -> None:
    """空闲超时自己退出 —— 把显存还回去。"""
    while True:
        time.sleep(5.0)
        # ★ 出图 / 换底模期间【绝不能】退出：last_hit 是"请求进来时"打的，
        #   渲染过程中不更新。只要单张耗时超过 idle_timeout（CPU 模式、大分辨率、
        #   或者把 sd_idle_timeout 调到 60），watchdog 就会在渲染中途
        #   os._exit(0) —— 客户端只看到"连接被重置"，根本看不出原因。
        #   换底模也一样：加载 16 秒也是"没人在动"的假象。
        if _state["busy"] or _state.get("switching"):
            continue
        idle = time.time() - _state["last_hit"]
        if idle >= _state["idle_timeout"]:
            print(f"[sd-worker] 空闲 {idle:.0f} 秒 ≥ {_state['idle_timeout']:.0f}，退出", flush=True)
            # os._exit 而不是 sys.exit：要连带终止 HTTP 线程和 CUDA 上下文
            os._exit(0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--idle-timeout", type=float, default=300.0)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--arch", default="auto", choices=["auto", "sd15", "sdxl"],
                    help="底模架构；auto=按文件大小猜")
    ap.add_argument("--offload", default="auto",
                    choices=["auto", "on", "off", "sequential"],
                    help="显存策略；auto=model offload，sequential=最省显存但最慢")
    # LoRA 是可选的：不填就是纯底模；填了就加载并在推理时按 --lora-scale 缩放。
    ap.add_argument("--lora", default="", help="LoRA 的 .safetensors 路径，留空=不用")
    ap.add_argument("--lora-scale", type=float, default=0.8, help="LoRA 推理强度 0~1")
    ap.add_argument("--sampler", default="euler", choices=sorted(SAMPLERS),
                    help="采样器/调度器：euler=欧拉+普通（默认），dpmpp2m_karras=DPM++ 2M+Karras")
    ap.add_argument("--max-side", type=int, default=1920,
                    help="单边像素上限（插件配置 sd_max_side）；超过会被夹到这个值")
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[sd-worker] 警告：--host={args.host} 不是回环地址。"
            "这个服务没有鉴权，只应监听本机。",
            file=sys.stderr,
            flush=True,
        )

    _state["checkpoint"] = args.checkpoint
    _state["idle_timeout"] = args.idle_timeout
    _state["device"] = args.device
    # 切换底模时要原样复用这两个 —— 换模型不该悄悄换掉显存策略
    _state["arch_mode"] = args.arch
    _state["offload_mode"] = args.offload
    _state["sampler"] = normalize_sampler(args.sampler)
    _state["max_side"] = int(args.max_side or 1920)

    print(f"[sd-worker] python {sys.version.split()[0]}", flush=True)
    # ★ 模型放后台线程加载：这样 HTTP 服务能立刻起来，
    #   加载期间客户端也能通过 /health、/progress 看到百分比（而不是干等/超时）。
    threading.Thread(
        target=load_pipeline,
        args=(args.checkpoint, args.device, args.arch, args.offload, args.sampler),
        daemon=True,
    ).start()
    if args.lora:
        load_lora(args.lora, args.lora_scale)

    _state["last_hit"] = time.time()
    threading.Thread(target=_idle_watchdog, daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[sd-worker] 就绪 http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[sd-worker] 收到中断，退出", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
