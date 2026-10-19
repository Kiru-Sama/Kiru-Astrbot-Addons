"""NCD 绘图：独立 SDXL 绘图后端（工具 + 生图页），不依赖 ComfyUI。

从「链路工具箱」里拆出来的独立插件（2026-10-19 整理）：
  · 三个工具：drawing_tools（入口/规范）、generate_image（全参数）、switch_model（换底模）
  · 一个页面：/ncd-draw/*（出图、进度、下载）
  · 一个独立子进程 sd_worker.py（diffusers + SDXL，按需拉起、空闲退出）

★ 画图相关工具必须先调 drawing_tools：没调就返回错误并指正（不是静默跳过）。
★ 配置项见 `_conf_schema.json`；AstrBot 存到
  `data/config/astrbot_plugin_ncd_draw_config.json`。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.web import json_response
from astrbot.api.web import request as web_request

from astrbot.api.provider import ProviderRequest

try:  # AstrBot 以包的形式加载插件
    from .lora_catalog import (
        SKIPPED as LORA_SKIPPED,
        LoraError,
        catalog_text,
        resolve as resolve_loras,
        scan as scan_loras,
    )
    from . import kb_index
    from .sd_local import SDBackendError, StandaloneClient
except ImportError:  # 离线单测
    from lora_catalog import (
        SKIPPED as LORA_SKIPPED,
        LoraError,
        catalog_text,
        resolve as resolve_loras,
        scan as scan_loras,
    )
    import kb_index
    from sd_local import SDBackendError, StandaloneClient

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR_NAME = os.path.basename(PLUGIN_DIR)
ASTRBOT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(PLUGIN_DIR)))

# 本插件注册的工具名（沉浸/菜单过滤会用它）
TOOL_NAMES = ("drawing_tools", "generate_image", "switch_model", "draw_kb_search")

TOOL_SUMMARY: dict[str, str] = {
    "drawing_tools": "drawing_tools() —— 画图入口（必须先调）：给出全部参数范围、提示词规则、"
    "可选画风/底模清单；调过之后才能用 generate_image / switch_model"
    "（没先调它就直接调画图工具会返回错误）",
    "generate_image": "generate_image(prompt, ...) —— 画一张图并发出去；参数全开"
    "（negative/size/width/height/steps/cfg/seed/sampler/model/style/reference/strength/force）",
    "switch_model": "switch_model(model) —— 换画图用的底模（不带参数=只列可选项），"
    "换完之后的出图都用新模型，要重新加载约十几秒",
    "draw_kb_search": "draw_kb_search(query) —— 查提示词知识库（姿势/机位/负面词/面部/构图）；"
    "空 query = 先看有哪些文件",
}

DRAW_TOOLS = ("generate_image", "switch_model")
DRAW_GATE_ERROR = (
    "画图失败：所有绘图相关操作都必须【先调用 drawing_tools】（每个会话调一次即可）。\n"
    "它会告诉你：有哪些画风/底模可选、画幅上限、以及每个参数的范围和默认值。\n"
    "现在请先调用 drawing_tools，然后再调用 {tool}；这次没有出图。"
)


SIZE_MIN_SIDE = 256


SIZE_MAX_SIDE = 1920


SIZE_MAX_PIXELS = 1920 * 1440


SIZE_WARN_RATIO = 0.75


SIZE_PRESETS: dict[str, tuple[int, int]] = {
    "竖": (832, 1216),
    "竖版": (832, 1216),
    "portrait": (832, 1216),
    "横": (1216, 832),
    "横版": (1216, 832),
    "landscape": (1216, 832),
    "方": (1024, 1024),
    "方形": (1024, 1024),
    "square": (1024, 1024),
    "宽": (1216, 832),
}


def warn_side(max_side: int) -> int:
    """警告线：单边超过它就先问用户。默认 1920 → 1440。"""
    return max(SIZE_MIN_SIDE, int(int(max_side) * SIZE_WARN_RATIO))


def check_canvas(
    width: int, height: int, max_side: int | None = None, max_pixels: int | None = None
) -> None:
    """画幅硬约束：单边 ≥256 且 ≤ max_side，总面积 ≤ max_pixels。不合法抛 ValueError。

    ★ 为什么按【面积】也卡一道：单边都在范围内，但 1920×1920 的面积是
      1920×1440 的 1.33 倍、1024² 的 3.5 倍 —— 12 GB 卡实测就会被逼爆。
      只卡单边挡不住这种情况。
    ★ 上限从配置来（sd_max_side / sd_max_mp），默认 1920 / 2.76 MP：
      换张 24 GB 的卡，用户自己把这两个调大就行，不用改代码。
    """
    side_cap = int(max_side or SIZE_MAX_SIDE)
    px_cap = int(max_pixels or SIZE_MAX_PIXELS)
    w, h = int(width), int(height)
    if not (SIZE_MIN_SIDE <= w <= side_cap and SIZE_MIN_SIDE <= h <= side_cap):
        raise ValueError(
            f"画幅 {w}×{h} 超出范围：任一边都要在 {SIZE_MIN_SIDE}~{side_cap} 之间"
        )
    if w * h > px_cap:
        raise ValueError(
            f"画幅 {w}×{h} 面积太大（{w * h / 1e6:.2f} MP）：上限是 {px_cap / 1e6:.2f} MP"
            f"（默认 1920×1440）—— 再大显存会爆。"
            f"想要更大的画面请用 1920x1440 / 1440x1920 / 1600x1200 这类比例，"
            f"或在插件设置里调大 sd_max_side / sd_max_mp。"
        )


def parse_size(
    text: str, max_side: int | None = None, max_pixels: int | None = None
) -> tuple[int, int] | None:
    """把画幅解析成 (宽, 高)：支持 "832x1216" / "832*1216" / "竖" / "横" / "方"。

    空串返回 None（= 用配置里的默认尺寸）。解析不出来抛 ValueError，
    由调用方转成给模型看的人话 —— 不要静默回落到默认，否则模型以为
    自己指定了画幅，实际画出来是另一个。

    ★ 这里只管【硬上限】。"单边超过 3/4 上限要先问用户"那条规则不在这里
      —— 它需要 force 参数，属于 generate_image 的事。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    key = raw.lower().replace(" ", "")
    if key in SIZE_PRESETS:
        return SIZE_PRESETS[key]
    m = re.fullmatch(r"(\d{2,4})\s*[x*×,，:]\s*(\d{2,4})", key)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        check_canvas(w, h, max_side, max_pixels)
        return (w // 8 * 8, h // 8 * 8)
    raise ValueError(
        f"看不懂的画幅「{raw}」。可以写 832x1216 这种（宽x高），"
        "或者直接写 竖 / 横 / 方。"
    )


def looks_chinese(text: str) -> bool:
    """这段文字里有没有汉字 / 全角标点。

    用途：画图的 prompt 必须是英文（SD1.5 的文本编码器只认英文）。
    有汉字就【提醒模型自己翻译】再调用，而不是硬塞进去白等十几秒出一张废图。

    ★ 只判断"有没有"，不做翻译 —— 翻译要额外一次模型调用，
      而且 4B 翻出来的画面描述往往比自己写的还差。提醒它自己写更好。
    """
    for ch in text or "":
        code = ord(ch)
        if 0x3400 <= code <= 0x9FFF:      # CJK 统一表意文字 + 扩展 A
            return True
        if 0xF900 <= code <= 0xFAFF:      # 兼容表意文字
            return True
        if 0xFF00 <= code <= 0xFFEF:      # 全角标点（「，」「。」也是中文输入的痕迹）
            return True
    return False


class ModelError(Exception):
    """底模名字解析失败（可预期的用户错误，要给人话而不是栈）。"""


class NcdDrawPlugin(star.Star):
    def __init__(
        self, context: star.Context, config: dict[str, Any] | None = None
    ) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf: dict[str, Any] = config or {}
        # 独立 SD 后端：懒建 + 按需拉起子进程（SDXL）
        self._sd: StandaloneClient | None = None
        # 最近一次生成的图（路径, 文件名）—— 生图页「下载原图」与 reference=last 用
        self._last_image: tuple[Path, str] | None = None
        # 画图入口被调过的会话（只用于"别重复要同一份长文案"）
        self._draw_revealed: dict[str, float] = {}
        # 当前底模覆盖（AI/页面换过就用它，否则用配置里的 sd_checkpoint）
        self._model_override: str | None = None
        # 出图串行锁：同一时刻只允许一条管线在跑
        self._render_lock = asyncio.Lock()

        # 扫一遍画风目录，把结果写进配置项（面板可见）
        self._refresh_lora_status()

        # ---- 生图页面 Web API ----
        try:
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-draw/status", self.api_imggen_status, ["GET"],
                "生图页：当前参数、底模/画风、worker 状态",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-draw/generate", self.api_imggen_generate, ["POST"],
                "生图页：按参数生成一张图（返回 base64）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-draw/progress", self.api_imggen_progress, ["GET"],
                "生图页：当前出图进度（步数）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-draw/last", self.api_imggen_last, ["GET"],
                "生图页：最近一次生成的图（下载用）",
            )
            logger.info(f"[draw] 已注册生图页 API，路由前缀 /{PLUGIN_DIR_NAME}/ncd-draw/")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[draw] 注册生图页 API 失败: {exc}")


    def _conf_str(self, key: str, default: str = "") -> str:
        """取字符串配置，顺手去掉用户从资源管理器复制路径时带上的引号。"""
        raw = self._conf.get(key)
        if raw is None:
            return default
        text = str(raw).strip().strip('"').strip("'").strip()
        return text or default

    def _conf_int(self, key: str, default: int) -> int:
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(f"[toolbox] 配置 {key}={raw!r} 不是整数，改用默认值 {default}")
            return default

    def _conf_bool(self, key: str, default: bool) -> bool:
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")

    def _conf_float(self, key: str, default: float) -> float:
        """取浮点配置。

        ★ 和 _conf_int 一样：从 JSON 手改过的值可能是字符串，
          认不出来一律退回默认 —— 不能因为配置写歪了就把行为改掉。
        """
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(f"[toolbox] 配置 {key}={raw!r} 不是数字，改用默认值 {default}")
            return default

    def _discard_worker(self) -> None:
        """把当前 worker 判为脏：停掉并丢弃，下次出图重新拉起。

        ★ 用在「出图超时」之后：asyncio.wait_for 只取消了协程，
          asyncio.to_thread 里的线程停不下来、GPU 还在算。
          如果这时只放锁不管 worker，下一个请求会卡在 worker 的线程锁上，
          再被自己的 urlopen 超时打断 —— 连环报错；而且第一个请求的响应
          还会往已关闭的 socket 上写（BrokenPipe 噪音）。
        """
        try:
            if self._sd is not None:
                self._sd.stop()
                logger.warning("[toolbox] worker 被判为脏并停止（下次出图会重新拉起）")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 停止 worker 失败（忽略）: {exc}")
        finally:
            self._sd = None

    def _sd_client(self) -> StandaloneClient:
        """独立 SD 后端客户端（懒建）。

        ★ 不做常驻服务：worker 是【按需拉起的子进程】，空闲 sd_idle_timeout 秒
          自己退出把显存还回去。这里只记住句柄，每次用之前探活、挂了自动重拉。
        """
        if self._sd is None:
            self._sd = StandaloneClient(
                python=self._conf_str("sd_python"),
                checkpoint=self._conf_str("sd_checkpoint"),
                port=self._conf_int("sd_port", 8199),
                idle_timeout=float(self._conf_int("sd_idle_timeout", 300)),
                gen_timeout=float(self._conf_int("image_timeout", 70) or 70),
                # LoRA：留空 = 纯底模。只在 worker 启动时加载一次。
                lora=self._conf_str("sd_lora"),
                lora_scale=self._conf_float("sd_lora_scale", 0.8),
                # cuda / cpu。cpu 不吃显存（但慢一个数量级）。
                device=self._conf_str("sd_device") or "cuda",
                # auto=按文件大小猜；SDXL 融合模型 6~7 GB，SD1.5 约 2 GB
                arch=self._conf_str("sd_arch") or "auto",
                # auto=SDXL 开 offload（显存 7GB -> 3GB），SD1.5 关
                offload=self._conf_str("sd_offload") or "auto",
                # euler（欧拉+普通，默认）/ dpmpp2m_karras
                sampler=self._conf_str("sd_sampler") or "euler",
                # 单边像素上限（默认 1920）：worker 会把超出的尺寸夹到这个值
                max_side=self._conf_int("sd_max_side", SIZE_MAX_SIDE) or SIZE_MAX_SIDE,
                log_path=Path(ASTRBOT_ROOT)
                / "data"
                / "plugin_data"
                / "ncd_toolbox"
                / "sd_worker.log",
            )
        return self._sd

    async def _render_standalone(
        self, prompt: str, opts: dict | None = None
    ) -> tuple[bytes, str]:
        """独立后端文生图。

        ★ 画风与参考图都在【调用方】解析好、放进 opts 里传进来
          （`loras` / `image` / `strength`）。这里只负责拼 payload 交给 worker
          —— 早先误把 style / reference 的解析写进这里，导致
          `NameError: name 'style' is not defined`，而且参考图分支还会自己调自己。
        """
        opts = opts or {}
        client = self._sd_client()

        data, seed = await client.txt2img(
            {
                "prompt": prompt,
                # ★ 当前底模：worker 会自己比对，不一样就当场换（约十几秒）。
                #   带上它的好处是"谁也不需要记得上次换过没有"。
                "checkpoint": opts.get("checkpoint") or self._current_model(),
                # ★ 画风由调用方解析好放进 opts，worker 只负责装卸。
                "loras": opts.get("loras") or [],
                # ★ 负面词走配置（留空 = 用 worker 里的默认）。以前插件从不传，
                #   standalone 永远吃 worker 那份写死的默认，换模型也改不了。
                # ★ 负面词/采样器：opts 里有就用调用方的（AI 能自己调），否则用配置。
                "negative": self._negative_with_extra(opts.get("negative") or ""),
                "sampler": opts.get("sampler") or self._conf_str("sd_sampler") or "euler",
                "width": int(opts.get("width") or self._conf_int("sd_width", 512) or 512),
                "height": int(opts.get("height") or self._conf_int("sd_height", 512) or 512),
                "steps": int(opts.get("steps") or self._conf_int("sd_steps", 25)),
                "cfg": float(opts.get("cfg") or self._conf_float("sd_cfg", 7.0)),
                "seed": opts.get("seed"),
            }
        )
        return data, f"sd_{seed}.png"

    async def _render_standalone_img2img(
        self, prompt: str, opts: dict
    ) -> tuple[bytes, str]:
        """参考图生图（只有独立后端支持）。

        ★ 分辨率不在这里给 —— img2img 的输出尺寸跟随参考图
          （diffusers 的 img2img `__call__` 没有 width/height，实测确认）。
        """
        client = self._sd_client()
        data, seed = await client.img2img(
            {
                "prompt": prompt,
                "checkpoint": opts.get("checkpoint") or self._current_model(),
                "sampler": opts.get("sampler") or self._conf_str("sd_sampler") or "euler",
                "negative": self._negative_with_extra(opts.get("negative") or ""),
                "image": opts["image"],
                # 画风也要传给 img2img：worker 那边两条路都会 apply_loras
                "loras": opts.get("loras") or [],
                "strength": float(opts.get("strength") or 0.6),
                "steps": int(opts.get("steps") or self._conf_int("sd_steps", 25)),
                "cfg": float(opts.get("cfg") or self._conf_float("sd_cfg", 7.0)),
                "seed": opts.get("seed"),
            }
        )
        return data, f"sd2i_{seed}.png"

    def _progress_text(
        self, info: dict, st: dict, now: float
    ) -> str | None:
        """看一次 /progress 快照，决定这一轮说什么。None = 这轮闭嘴。

        ★ 为什么把判定抽成纯函数：聊天的进度汇报有"静默期 / 去重 / 条数上限"
          三重规则，只看代码很难确认它对不对；抽出来以后可以用假时钟把
          每条规则都测到，不用真的睡 10 秒。
        ★ st 是跨轮次的状态（不在这里 sleep、不在这里发消息）。
        ★ 加载和出图【共用一个配额】（默认 5 条）：加载慢的时候不该把出图的
          份额也吃掉，反过来也一样 —— 用户要的是"整轮最多 5 条"，不是每段各 5 条。
        """
        limit = max(1, int(st.get("max_msgs") or 5))
        sent = int(st.get("sent") or 0)
        if sent >= limit:
            st["done"] = True
            return None

        def _bump(text: str) -> str:
            st["sent"] = sent + 1
            st["done"] = st["sent"] >= limit
            return text

        # —— 在出图：报步数 ——
        if info.get("busy"):
            step = int(info.get("step") or 0)
            total = int(info.get("total") or 0)
            full = int(info.get("steps_full") or total or 0)
            # ★ 图生图只重绘 steps×strength 步 —— 不写清楚，用户会问
            #   "我设的 25 步，怎么进度是 17 步？"
            if info.get("mode") == "img2img" and full and total and full != total:
                return _bump(
                    f"🎨 图生图 {step}/{total} 步"
                    f"（{full} 步 × 强度 {total / full:.2f}）"
                )
            return _bump(f"🎨 出图中… {step}/{total} 步" if total else "🎨 出图中…")

        # —— 还没就绪：模型在加载 ——
        if info.get("ready") is False:
            if st.get("load_from") is None:
                # 第一次看到"没就绪"，从此刻开始算静默期
                st["load_from"] = now
                return None
            # ★ 静默期：worker 刚拉起来那几秒是 `启动中 0%`（python 和 torch
            #   还在 import），这段时间报出去全是 0% —— 看着像卡死，纯噪音。
            if now - float(st["load_from"]) < float(st.get("grace") or 0):
                return None
            pct = int(info.get("phase_pct") or 0)
            if pct <= 0:
                # 百分比一点没动 = 还没进真正的"读权重"，同样报不出信息
                return None
            phase = str(info.get("phase") or "加载模型")
            key = f"{phase}|{pct}"
            if key == st.get("last_load"):
                return None  # 同一档进度不重复刷
            st["last_load"] = key
            return _bump(f"📦 {phase} {pct}%")

        # 就绪但还没开始出图：什么都别说，等它忙起来
        return None

    async def _chat_progress(self, event: AstrMessageEvent, max_msgs: int = 5) -> None:
        """出图期间往会话里报进度（加载 + 步数）。

        ★ 为什么是"发消息"而不是"进度条"：QQ 没有可编辑消息，也没有进度条。
          能做的是隔一段时间发一句「12/20 步」。
        ★ 为什么限条数：慢一点就发几十条会把群聊刷爆。默认整轮最多 5 条
          （加载 + 出图共用这个配额），间隔 sd_progress_interval 秒（默认 8）。
        ★ 为什么加载要等 10 秒（sd_load_grace）：worker 先起 HTTP 服务、
          再在后台线程加载模型，所以最前面那几秒能查到的是 `启动中 0%`。
          那时候就报，用户看到的是一串 0%，反而以为卡死了。
        ★ 出图结束/失败由调用方 cancel 掉这个任务。
        """
        interval = max(3, self._conf_int("sd_progress_interval", 8))
        grace = max(0, self._conf_int("sd_load_grace", 10))
        st: dict = {"max_msgs": max(1, int(max_msgs)), "grace": float(grace)}
        # 兜底上限：正常一张图几十秒，这里只是防止任务在异常路径上永生。
        deadline = time.time() + float(self._conf_int("image_timeout", 180) or 180) + 120.0
        try:
            while time.time() < deadline:
                await asyncio.sleep(interval)
                if self._sd is None:
                    return
                info = await self._sd.progress()
                text = self._progress_text(info, st, time.time())
                if text:
                    await self.context.send_message(
                        event.unified_msg_origin,
                        MessageChain(chain=[Comp.Plain(text=text)]),
                    )
                if st.get("done"):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[toolbox] 汇报出图进度失败（忽略）: {exc}")

    # ---------- 底模（checkpoint）列表 ----------
    def _size_limits(self) -> tuple[int, int, float, int]:
        """画幅上限：(单边上限, 面积上限像素, 面积 MP, 警告线)。

        ★ 这三个数以前是写死的（1920 / 1920×1440 / 1440），其实是【12 GB 卡】
          的结论。换张 24 GB 的卡就该能画更大 —— 所以做成配置项：
          sd_max_side（单边上限）、sd_max_mp（面积上限，单位 MP）。
        ★ 警告线不单独配置，按上限的 3/4 算（默认 1920→1440）：
          三个数字互相打架只会让面板更难解释。
        """
        try:
            cap = int(self._conf_int("sd_max_side", SIZE_MAX_SIDE) or SIZE_MAX_SIDE)
        except (TypeError, ValueError):
            cap = SIZE_MAX_SIDE
        cap = max(SIZE_MIN_SIDE, cap)
        try:
            mp = float(self._conf_float("sd_max_mp", SIZE_MAX_PIXELS / 1e6) or 0)
        except (TypeError, ValueError):
            mp = SIZE_MAX_PIXELS / 1e6
        if mp <= 0:
            mp = SIZE_MAX_PIXELS / 1e6
        px = int(mp * 1_000_000)
        return cap, px, mp, warn_side(cap)

    def _model_dir(self) -> Path:
        """底模目录：配置优先，否则用 sd_checkpoint 所在目录。

        ★ 为什么不把路径写死：这是要给别人用的插件。写死"我的桌面"换台机器就废。
          留空时跟着 sd_checkpoint 走 —— 你换底模目录只要改那一项。
        """
        raw = str(self._conf_str("sd_model_dir") or "").strip()
        if raw:
            return Path(raw)
        ckpt = str(self._conf_str("sd_checkpoint") or "").strip()
        if ckpt:
            return Path(ckpt).parent
        return Path(ASTRBOT_ROOT) / "data" / "plugin_data" / "ncd_toolbox" / "models"

    def _model_list(self) -> list[dict]:
        """扫目录里能当底模用的文件。

        ★ 靠【文件大小】过滤：LoRA 和底模经常被丢在同一个目录里
          （这台机器上就是），而 <sd_model_min_gb>（默认 3 GB）能把
          几十~几百 MB 的 LoRA 全部排除掉，不用维护黑白名单。
        ★ 不缓存：这个目录一般就几个文件，stat 一遍比维护失效逻辑便宜。
        """
        directory = self._model_dir()
        try:
            min_bytes = float(self._conf_float("sd_model_min_gb", 3.0) or 3.0) * 1024**3
        except (TypeError, ValueError):
            min_bytes = 3.0 * 1024**3
        current = os.path.abspath(self._current_model()) if self._current_model() else ""
        out: list[dict] = []
        try:
            files = [p for p in directory.iterdir() if p.is_file()]
        except Exception:  # noqa: BLE001
            return out
        for path in files:
            if path.suffix.lower() not in (".safetensors", ".ckpt"):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < min_bytes:
                continue
            out.append(
                {
                    "name": path.stem,
                    "file": path.name,
                    "path": str(path),
                    "size_mb": round(size / 1024**2, 1),
                    "current": os.path.abspath(str(path)) == current,
                }
            )
        out.sort(key=lambda x: x["name"].lower())
        return out

    def _current_model(self) -> str:
        """现在用哪个底模：换过就用换过的，否则用配置里的默认。"""
        path = getattr(self, "_model_override", None)
        if path:
            return str(path)
        return str(self._conf_str("sd_checkpoint") or "").strip()

    def _resolve_model(self, want: str) -> str:
        """底模名字 → 绝对路径。

        接受：绝对路径 / 文件名 / 不带扩展名的名字 / 唯一的一段子串（如 "noob"）。
        全都不区分大小写。★ 匹配不到就报错【并列出可选值】——
        不猜，猜错了要白等十几秒加载一个用户根本没选的模型。
        """
        want = str(want or "").strip()
        if not want:
            raise ModelError("model 是空的")
        if os.path.isfile(want):
            return os.path.abspath(want)

        models = self._model_list()
        if not models:
            raise ModelError(
                f"底模目录里没有可用模型（目录：{self._model_dir()}；"
                f"小于 {self._conf_float('sd_model_min_gb', 3.0)} GB 的文件会被当成 LoRA 跳过）"
            )
        low = want.lower()
        for item in models:
            if low in (item["name"].lower(), item["file"].lower()):
                return item["path"]
        # 子串匹配：太短的名字（"v1" 之类）容易同时命中好几个，只对 ≥3 个字符生效
        if len(low) >= 3:
            hits = [m for m in models if low in m["name"].lower()]
            if len(hits) == 1:
                return hits[0]["path"]
            if len(hits) > 1:
                raise ModelError(
                    f"「{want}」同时匹配到 {len(hits)} 个底模，请写得更具体一些："
                    + "、".join(m["name"] for m in hits)
                )
        raise ModelError(
            f"没有叫「{want}」的底模。可选的：" + "、".join(m["name"] for m in models)
        )

    def _model_catalog_hint(self) -> str:
        """给 switch_model 的工具描述追加一段"有哪些底模可用"。"""
        try:
            models = self._model_list()
            if not models:
                return f"\n\n【可选底模】\n（{self._model_dir()} 里没有可用模型）"
            lines = []
            for m in models:
                mark = "（当前）" if m.get("current") else ""
                lines.append(f"· {m['name']}{mark}（{round(m['size_mb'] / 1024, 2)} GB）")
            return (
                "\n\n【可选底模（填在 model 参数里，大小写不敏感，可只写一段子串）】\n"
                + "\n".join(lines)
                + "\n换底模要重新加载权重（约 10~20 秒），换完对之后的每一次出图都生效。"
                "\n只有用户明确说要换模型/换底模/用另一个模型画时才调；不确定有哪些就"
                "先不带参数调一次看看清单。"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 生成底模清单失败（不影响本轮）: {exc}")
            return ""

    def _lora_dir(self) -> Path:
        """画风目录：配置优先，否则用「底模同级的 loras/」——换机器不用改配置。"""
        raw = str(self._conf_str("sd_lora_dir") or "").strip()
        if raw:
            return Path(raw)
        ckpt = str(self._conf_str("sd_checkpoint") or "").strip()
        if ckpt:
            return Path(ckpt).parent / "loras"
        return Path(ASTRBOT_ROOT) / "data" / "plugin_data" / "ncd_toolbox" / "loras"

    def _lora_list(self) -> list:
        """扫目录（按 mtime+大小做轻缓存，不必每轮都读文件头）。"""
        directory = self._lora_dir()
        try:
            sig = tuple(
                sorted((p.name, p.stat().st_size) for p in directory.iterdir() if p.is_file())
            )
        except Exception:  # noqa: BLE001
            sig = ()
        cache = getattr(self, "_lora_cache", None)
        if cache and cache[0] == sig:
            return cache[1]
        loras = scan_loras(directory)
        skipped = list(LORA_SKIPPED)
        self._lora_cache = (sig, loras)
        if loras:
            logger.info(
                f"[toolbox] 画风目录 {directory}：可用 {len(loras)} 个"
                + (f"，跳过 {len(skipped)} 个（{skipped[0][1][:40]}…）" if skipped else "")
            )
        return loras

    def _lora_catalog_hint(self) -> str:
        """给工具描述追加的一段可用画风清单。"""
        try:
            loras = self._lora_list()
            if not loras:
                return ""
            allow = self._conf_bool("sd_allow_nsfw_lora", True)
            body = catalog_text(loras, allow)
            return (
                "\n\n【可选画风（填在 style 参数里）】\n"
                + body
                + "\n可以叠加，用逗号分隔，每个可加强度如 cunnyfunky:0.7（默认 0.8）。"
                "\n留空 = 不用任何画风。只在用户明确提到画风/画师/风格时才选。"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 生成画风清单失败（不影响本轮）: {exc}")
            return ""

    def _refresh_lora_status(self) -> str:
        """扫一遍画风目录，把结果写进配置项 sd_lora_status（面板上能直接看到）。

        ★ 为什么写进配置：AstrBot 的配置面板只能渲染 schema 里声明过的字段，
          没法给插件塞一个"动态列表"。所以用"插件自己维护一个只读文本字段"
          的办法 —— 你在面板里看到的永远是上次加载/重扫时的真实情况。

        ★ 只写不算：内容没变就不落盘，避免每次重载都改配置文件。
        """
        try:
            loras = self._lora_list()
            skipped = list(LORA_SKIPPED)
            if loras:
                head = "可用 %d 个：" % len(loras) + "、".join(
                    f"{lo.name}（{lo.rating}，{lo.size_mb:.0f}MB）" for lo in loras
                )
            else:
                head = f"目录 {self._lora_dir()} 里没有可用的画风"
            lines = [head, f"目录：{self._lora_dir()}"]
            if skipped:
                lines.append(
                    "已跳过 %d 个：" % len(skipped)
                    + "；".join(f"{n}（{why}）" for n, why in skipped)
                )
            text = "\n".join(lines)
            conf = getattr(self, "_conf", None)
            if isinstance(conf, dict) and conf.get("sd_lora_status") != text:
                conf["sd_lora_status"] = text
                if hasattr(conf, "save_config"):
                    conf.save_config({"sd_lora_status": text})
            return text
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 刷新画风状态失败: {exc}")
            return ""

    def _local_model_state(self, umo: str) -> tuple[bool, str]:
        """当前会话用的是不是【本地模型】。返回 (是否本地, 模型标识)。

        ★ 为什么要这个：本地小模型（4B 之类）写 SDXL 提示词的质量明显差 ——
          标签少、爱写无效质量词、还会照抄上一次。与其让它画出废图浪费显存，
          不如在本地模型下直接把绘图工具收起来，等换回云端模型再放开。

        ★ 判定依据：provider 的 id 里含 local_model_keywords 里的任一词
          （默认 lmstudio / ollama）。拿不到 provider 就当成"不是本地"，
          宁可不禁用，也别把绘图功能莫名其妙锁死。
        """
        try:
            prov = self.context.get_using_provider(umo)
            pid = ""
            conf = getattr(prov, "provider_config", None)
            if isinstance(conf, dict):
                pid = str(conf.get("id") or "")
            if not pid:
                meta = getattr(prov, "meta", None)
                if callable(meta):
                    pid = str(getattr(meta(), "id", "") or "")
            raw = str(self._conf_str("local_model_keywords") or "lmstudio,ollama")
            keys = [k.strip().lower() for k in raw.split(",") if k.strip()]
            low = pid.lower()
            return (any(k in low for k in keys), pid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[toolbox] 判断本地模型失败（按云端处理）: {exc}")
            return (False, "")

    def _hide_image_tool_for_local(self, req: Any, event: AstrMessageEvent) -> None:
        """本地模型时把 generate_image 从本轮工具集移除。"""
        try:
            if not self._conf_bool("disable_image_on_local", True):
                return
            if not req.func_tool:
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            is_local, pid = self._local_model_state(umo)
            if not is_local:
                return
            names = [t.name for t in req.func_tool.tools]
            if "generate_image" not in names:
                return
            req.func_tool.remove_tool("generate_image")
            logger.info(
                f"[toolbox] 当前是本地模型（{pid}），本轮移除 generate_image —— "
                "本地小模型写的提示词画出来质量差，换云端模型会自动恢复"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 本地模型禁用绘图失败（不影响本轮）: {exc}")

    def _negative_with_extra(self, extra: str = "") -> str:
        """配置里的默认负面词 + 调用方额外给的（去重，不覆盖）。

        ★ 2026-10-20：以前 opts 里有 negative 就【整段替换】配置那套，
          等于模型随手写几个词就把 40 多个默认负面词全冲掉了，画质反而更差。
          现在统一成"追加"：额外词永远叠在默认那套后面。
        """
        base = (self._conf_str("sd_negative") or "").strip()
        extra = (extra or "").strip().strip(",")
        if not extra:
            return base
        seen: set[str] = set()
        out: list[str] = []
        for part in (base + "," + extra).split(","):
            item = part.strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                out.append(item)
        return ", ".join(out)

    def _component_image_bytes(self, comps, depth: int = 0) -> bytes | None:
        """在一组消息段里找第一张图；会钻进被引用消息（Reply.chain）里找。

        ★ 2026-10-19 实测踩坑：用户「引用一张图 + 说用图生图」时，插件原先只扫
          当前消息最外层的 Image，拿不到被引用消息里的图 —— 于是 reference='message'
          静默退回文生图，用户看到的是一张毫无关系的新图。
          AstrBot 的 aiocqhttp adapter 已经把被引用消息解析成 Reply.chain 了，
          这里往下钻一层就够（嵌套引用最多再套一层，depth 兜底防环）。
        """
        if not comps or depth > 3:
            return None
        for comp in comps:
            if isinstance(comp, Comp.Image):
                path = getattr(comp, "path", None) or getattr(comp, "file", None)
                if path and Path(str(path)).is_file():
                    data = Path(str(path)).read_bytes()
                    if data:
                        return data[: 12 * 1024 * 1024]
            chain = getattr(comp, "chain", None)
            if chain and not isinstance(comp, Comp.Image):
                data = self._component_image_bytes(chain, depth + 1)
                if data:
                    return data
        return None

    def _message_image_bytes(self, event: AstrMessageEvent) -> bytes | None:
        """取当前消息（含被引用的那条消息）里第一张图。取不到返回 None。"""
        try:
            return self._component_image_bytes(event.get_messages() or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[toolbox] 读消息里的图失败: {exc}")
        return None

    @staticmethod
    def _wants_reference(reference: str) -> bool:
        """模型是不是【明确】要了参考图（而不是留空/写了"不用"）。"""
        ref = str(reference or "").strip().lower()
        return bool(ref) and ref not in ("none", "off", "无", "不用", "false", "0")

    async def _reference_bytes(self, reference: str, event: AstrMessageEvent) -> bytes | None:
        """把 reference 参数解析成图片字节。

        · ""（留空）→ 不用参考图
        · "message" / "msg" / "图" → 当前消息里附的图
        · "last" / "上一张" → 机器人最近一次生成的那张
        ★ 认不出来就返回 None（当普通文生图），不报错 —— 参考图是加分项，
          不该因为它把整次出图弄失败。
        """
        ref = str(reference or "").strip().lower()
        if not ref or ref in ("none", "off", "无", "不用"):
            return None

        if ref in ("last", "previous", "上一张", "刚才那张", "上一张图"):
            last = getattr(self, "_last_image", None)
            if last and Path(last[0]).is_file():
                return Path(last[0]).read_bytes()
            logger.info("[toolbox] 要求用上一张做参考图，但没有上一张")
            return None

        if ref in ("message", "msg", "image", "图", "消息图", "这张"):
            data = self._message_image_bytes(event)
            if data:
                return data
            # 兜底：有些平台图没有 path，只能异步转
            try:
                for comp in event.get_messages() or []:
                    if isinstance(comp, Comp.Image):
                        getter = getattr(comp, "convert_to_file_path", None)
                        if callable(getter):
                            p = await getter()
                            if p and Path(str(p)).is_file():
                                return Path(str(p)).read_bytes()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[toolbox] 异步取图失败: {exc}")
            logger.info("[toolbox] 要求用消息里的图做参考图，但这条消息没有图")
            return None

        logger.info(f"[toolbox] reference={reference!r} 不认识，按不用参考图处理")
        return None

    async def _render_image(
        self, prompt: str, opts: dict | None = None
    ) -> tuple[bytes, str]:
        """出图（带总超时）。

        ★ 70 秒是【一次出图的总时长上限】，含 worker 冷启动。
          为什么不是 40：实测冷启动到过 31.9 秒，再加 5~6 秒推理就 38 秒，
          40 秒很容易把正常请求误杀。宁可宽一点，也别把能成的图拦掉。
        ★ opts 里有 `image` 就是参考图生图，只支持独立后端。
        """
        opts = opts or {}
        timeout = float(self._conf_int("image_timeout", 70) or 70)
        # ★ 纯 CPU 出图比 GPU 慢一个数量级，70 秒根本不够。
        #   这里自动放宽到至少 300 秒 —— 只放宽不缩短，用户配得更大就听用户的。
        if (
            (self._conf_str("sd_device") or "cuda").lower() == "cpu"
        ):
            timeout = max(timeout, 300.0)
        # ★ 这里【不再】为 SDXL 自动放宽超时：那样会把 image_timeout 变成一个
        #   "写了也没用"的配置（实测把测试里 1 秒超时的那条弄坏了）。
        #   SDXL 的耗时请通过 image_timeout 自己配（默认值已按 SDXL 调到 180）。
        if opts.get("image"):
            coro = self._render_standalone_img2img(prompt, opts)
        else:
            coro = self._render_standalone(prompt, opts)
        # ★ 同一时刻只允许【一条】出图管线在跑（聊天 + 生图页可能同时点）。
        #   worker 内部本来也有锁，但两条请求同时进来会让进度条互相覆盖、
        #   超时互相打架，更重要的是显存峰值会叠起来 —— 那正是之前爆显存的成因之一。
        #   这里在插件侧就串行化：后来的排队等，不并发。
        #   超时只包住真正出图那一段，排队时间不算进去。
        lock = getattr(self, "_render_lock", None)
        if lock is None:
            # 兜底：实例不是走 __init__ 建的（单元测试、或将来别的入口）时补一个，
            # 免得整条出图路因为少个属性就 AttributeError。
            lock = asyncio.Lock()
            self._render_lock = lock
        if lock.locked():
            logger.info("[toolbox] 已有出图任务在跑，排队等待…")
        async with lock:
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                # ★ wait_for 只取消了协程；to_thread 里的线程停不下来、GPU 还在算。
                #   如果这时直接放锁，下一个请求会卡在 worker 的线程锁上，
                #   再被自己的 urlopen 超时打断 —— 连环报错 + BrokenPipe 噪音。
                #   所以先把 worker 判为脏（停掉并丢弃），再放锁，下次重新拉起。
                self._discard_worker()
                # 复用 SDBackendError 是因为两个调用点都已经在 catch 它；
                # 消息里写清楚是超时，别让人误以为是后端崩了。
                raise SDBackendError(
                    f"出图超时：超过 {timeout:.0f} 秒还没画完，已中止。"
                    "常见原因：worker 冷启动太慢（换更小的模型）、显存不够被换到内存、"
                    "或者前面还有别的任务在排。调大 image_timeout 或减小分辨率再试。"
                ) from None

    # ------------------------------------------------------------------
    #  工具菜单入口：模型先调这个，才看得到下面那些工具
    # ------------------------------------------------------------------

    @staticmethod
    def _save_generated(data: bytes, filename: str, tag: str = "sd") -> Path:
        """把生成的图落到 AstrBot 的临时目录。

        ★ 用 AstrBot 的 temp 目录而不是自己建目录：那边有 TempDirCleaner
          定时清理，不用我们管回收，也不会把磁盘越占越多。
        """
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            base = Path(get_astrbot_temp_path())
        except Exception:  # noqa: BLE001
            base = Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "image.png"
        # ★ 前缀由调用方传（现在固定 sd_）：以前固定写 comfy_，
        #   后来独立后端出的图也叫 comfy_xxx.png，排查时误导。
        #   注意这里是 @staticmethod —— 不能读 self，所以前缀只能从参数进来
        #   （2026-10-18 实测踩过：在里面写 self 会 NameError，图画不出来）。
        path = base / f"{tag}_{uuid.uuid4().hex[:8]}_{safe}"
        path.write_bytes(data)
        return path

    def _draw_gate_error(self, event: AstrMessageEvent, tool: str) -> str | None:
        """没调过 drawing_tools 就返回错误提示；调过返回 None。

        ★ 和"把工具藏起来"的区别：藏起来时 runner 只记一条 warning 就跳过，
          模型看不到任何反馈、会反复重试（2026-10-19 实测）；这里返回的是
          一条【可执行的下一步】，模型下一轮就会去调 drawing_tools。
        """
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if umo in getattr(self, "_draw_revealed", {}):
                return None
            logger.info(f"[toolbox] {tool} 被直接调用（未先调 drawing_tools），已返回指正")
            return DRAW_GATE_ERROR.format(tool=tool)
        except Exception:  # noqa: BLE001
            return None

    @filter.llm_tool(name="drawing_tools")
    async def drawing_tools(self, event: AstrMessageEvent) -> str:
        """画图入口（必须先调）：给出全部参数范围、可选画风/底模、当前默认值。

        ★ 所有绘图工具（generate_image / switch_model）都要先调一次本工具；
          没调就直接调它们会返回错误并提示回来调这个。
        ★ 每个会话调一次即可：第二次起只回一句短提醒（省上下文）。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not hasattr(self, "_draw_revealed"):
            self._draw_revealed = {}
        already = umo in self._draw_revealed
        self._draw_revealed[umo] = time.time()
        if len(self._draw_revealed) > 200:
            keep = sorted(self._draw_revealed.items(), key=lambda kv: kv[1])[-100:]
            self._draw_revealed = dict(keep)
        logger.info(f"[toolbox] 画图入口被调用：{umo or '（未知会话）'}")

        if already:
            return (
                "画图入口已经开过了，别重复调 drawing_tools。\n"
                "直接调 generate_image（参数见上一轮那份说明）或 switch_model 即可。"
            )

        model = os.path.basename(self._current_model()) or "（未配置）"
        sampler = self._conf_str("sd_sampler") or "euler"
        sampler_cn = "欧拉 + 普通" if sampler != "dpmpp2m_karras" else "DPM++ 2M + Karras"
        w = self._conf_int("sd_width", 720) or 720
        h = self._conf_int("sd_height", 1024) or 1024
        cap, px, mp, warn = self._size_limits()
        lines = [
            "画图入口已打开。接下来这两个工具可用：",
            "· generate_image(...) —— 画一张图并发出去（参数见下）",
            "· switch_model(model) —— 换底模（不带参数=只列可选项）",
            "· draw_kb_search(query) —— 查提示词知识库（标签/姿势/机位/负面词）",
            "",
            "★ 推荐流程：写 prompt 之前，先 draw_kb_search(关键词) 查一下标签，",
            "  确认「存不存在、放正面还是负面、准确写法」，再调 generate_image。",
            "  别凭印象编标签 —— 编错的标签等于白写，还白等十几秒。",
            "",
            f"当前默认：底模 {model} · 采样器 {sampler_cn} · {w}×{h} · "
            f"{self._conf_int('sd_steps', 25)} 步 · CFG {self._conf_float('sd_cfg', 5.5)}",
            "",
            "generate_image 的可调参数（都能留空 = 用默认值）：",
            "· prompt      英文短标签，10~15 个（详见下方四条硬规则）",
            "· negative    负面词；留空 = 用配置里那套长负面",
            f"· size        画幅，如 832x1216，或 竖/横/方；单边 ≤{warn} 直接画",
            "· width/height 像素直接给（8 的倍数，覆盖 size）",
            f"· steps       1~150；cfg 0~30；seed 整数（不给=随机）",
            "· sampler     euler（欧拉+普通）/ dpmpp2m_karras",
            "· model       底模名字（见下方清单）；留空=当前底模",
            "· style       画风，可叠加如 cunnyfunky:0.7；留空=不用",
            "· reference   message=这条/被引用消息里的图；last=你上一张；留空=文生图",
            "· strength    图生图强度 0~1；留空=配置默认",
            "· force       画幅超过警告线、且用户已同意时才填 true",
            "",
            "prompt 四条硬规则：",
            "① 只能是英文（中文会直接出废图）；② 逗号分隔的【短标签】，不要写长句；",
            "③ 上限 77 token（英文约 50~60 词，10~15 个标签就够），超出会被静默截断；",
            "④ 开头写 masterpiece, best quality, highres；"
            "不要写 8k / ultra detailed / award winning（它不认，只占额度）。",
            "画质更靠具体描述：光线（soft lighting / rim light）、"
            "镜头（close-up / from above / upper body）、质感（detailed eyes / glossy hair）。",
            "",
            f"画幅三档：单边 ≤{warn} 直接画；{warn}~{cap} 之间会先返回警告"
            "（费时费力、可能爆显存），必须先问用户，用户同意后带 force=true 再调；"
            f"单边超过 {cap}、或总面积超过 {mp:.2f} MP 直接拒绝 —— "
            f"最大画布约 {cap}×{int(px / cap)}（再大显存会爆，不给过）。",
        ]

        hint = self._lora_catalog_hint()
        if hint:
            lines.append(hint.strip())
        mhint = self._model_catalog_hint()
        if mhint:
            lines.append(mhint.strip())
        return "\n".join(lines)

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        style: str = "",
        reference: str = "",
        size: str = "",
        force: bool = False,
        negative: str = "",
        model: str = "",
        sampler: str = "",
        steps: int = 0,
        cfg: float = 0.0,
        width: int = 0,
        height: int = 0,
        seed: int = -1,
        strength: float = 0.0,
    ) -> str:
        """按描述画一张图并发到当前会话。★ 必须先调用 drawing_tools。

        Args:
            prompt(string): 英文【标签式】描述（逗号分隔短标签，10~15 个，
                开头写 masterpiece, best quality, highres）。中文会出废图。
                不确定标签写法时先 draw_kb_search 查，别硬编。
            style(string): 画风（LoRA）名字，留空=不用；可叠加，如 "cunnyfunky:0.7"。
            reference(string): 参考图来源：留空=文生图；"message"=这条/被引用消息里的图；
                "last"=你上一张生成的图。
            size(string): 画幅，如 "832x1216"，或简写 竖/横/方；留空=配置默认。
            force(bool): 画幅超过警告线、且用户已同意时才填 True。
            negative(string): 负面提示词；留空=用配置里的那套。
            model(string): 底模名字（见 drawing_tools 的清单）；留空=当前底模。
            sampler(string): 采样器：euler（欧拉+普通）/ dpmpp2m_karras；留空=配置默认。
            steps(int): 采样步数 1~150；0=用配置默认。
            cfg(float): CFG 0~30；0=用配置默认。
            width(int): 宽（8 的倍数，单边上限见配置）；和 height 一起覆盖 size。
            height(int): 高；单独给一个时另一半用配置默认。
            seed(int): 随机种子；-1（或留空）=每次随机。
            strength(float): 图生图强度 0~1；0=用配置默认（仅 reference 非空时有效）。
        """
        gate = self._draw_gate_error(event, "generate_image")
        if gate:
            return gate

        prompt = (prompt or "").strip()
        if not prompt:
            return "画图失败：prompt 是空的。请给一组英文标签，比如 \"1girl, silver hair, forest\"。"

        # ★ 中文提示词提醒：直接传中文不会报错，但会白等十几秒出一张废图。
        #   这里把它拦下来，让模型自己改写后重调 —— 只提醒，不代劳翻译。
        if looks_chinese(prompt):
            logger.info(f"[toolbox] 画图请求里含中文，已提醒模型改写英文：{prompt[:60]}")
            return (
                "先别画：你传的 prompt 里有中文，而底模的文本编码器只认英文，"
                "中文提示词出来的图基本是废的。\n"
                "请把它改写成【英文标签】再调用一次 generate_image：\n"
                "· 逗号分隔的短标签，不要长句\n"
                "· 挑最关键的 10~15 个（上限 77 token，超了会被静默截断）\n"
                "例：中文「银发少女站在森林里，逆光」→ 英文 "
                '"1girl, silver hair, long hair, forest, backlight, depth of field"'
            )

        # ★ 画幅：AI 可以自己选（size），留空就用配置里的默认尺寸。
        #   解析不出来就【报错】而不是悄悄用默认 —— 否则模型以为自己指定了画幅。
        cap, px, mp, warn = self._size_limits()
        dims: tuple[int, int] | None = None
        if int(width or 0) > 0 or int(height or 0) > 0:
            # ★ 显式 width/height 优先于 size（模型两个都给时以像素为准）
            w0 = int(width or 0) or (self._conf_int("sd_width", 720) or 720)
            h0 = int(height or 0) or (self._conf_int("sd_height", 1024) or 1024)
            try:
                check_canvas(w0, h0, cap, px)
            except ValueError as exc:
                return f"画图失败：{exc}"
            dims = (w0 // 8 * 8, h0 // 8 * 8)
        else:
            try:
                dims = parse_size(size, cap, px)
            except ValueError as exc:
                return f"画图失败：{exc}"

        # ★ 大画幅要先问用户：任一边超过 1440 时，一张图会明显更慢、更吃显存
        #   （实测 1920×1920 就能把 12 GB 显存逼到临界）。所以这里【不画】，
        #   先返回警告让模型去问；用户点头之后模型再带 force=True 调一次。
        # ★ 大画幅要先问用户：单边超过 1440 时，一张图会明显更慢、更吃显存
        #   （实测 1440² 就 9.3 GB、1920×1440 峰值 11.6 GB / 12.2 GB）。
        #   ★ 群聊里直接禁止：群里人多、显存一爆整台机器都卡，不能靠"问一句"兜。
        if dims and max(dims) > warn:
            w, h = dims
            in_group = "GroupMessage" in str(getattr(event, "unified_msg_origin", ""))
            if in_group:
                logger.warning(f"[toolbox] 群聊里请求 {w}x{h} 大画幅，已拒绝")
                return (
                    f"画图失败：群聊内严令禁止使用长宽超过 {warn} 像素的画幅"
                    f"（你给的是 {w}×{h}）。\n"
                    f"请改用 ≤{warn} 的尺寸重画，例如 1216x832 / 1024x1024 / 832x1216；"
                    "实在要更大，请让用户私聊我。"
                )
            if not force:
                logger.info(f"[toolbox] 画幅 {w}x{h} 超过 {warn}，先让模型问用户")
                return (
                    f"⚠️ 画幅 {w}×{h} 的任意一边超过了 {warn}："
                    "这个尺寸费时费力（像素量是默认尺寸的好几倍，单张可能要一两分钟），"
                    "显存也可能撑不住直接失败。\n"
                    f"★ 群聊内严令禁止调用长宽超过 {warn} 像素的画幅 —— 只有当这是私聊、"
                    "并且用户明确同意时才继续。\n"
                    "现在【没有开始画】。请把这一点告诉用户（说清楚会慢、可能爆显存），"
                    "问他要不要就按这个尺寸画。\n"
                    f"用户明确同意后，再调用一次 generate_image，参数不变、额外带上 "
                    f"force=true；用户说换个小的就按新的画。\n"
                    f"（硬上限：单边 {cap}、总面积 {mp:.2f} MP；再大直接拒绝。）"
                )

        # ★ SD1.5 的 CLIP 输入上限是 77 token，超出会被静默截断。
        #   这里只【记日志】（不改行为）：词数明显偏多时留一条痕迹，方便回看
        #   为什么某次出图"后半段提示词没生效"。
        word_count = len(prompt.split())
        if word_count > 60:
            logger.warning(
                f"[toolbox] 画图 prompt 偏长（{word_count} 词），"
                f"超过约 77 token 的部分会被截断：{prompt[:80]}"
            )

        # ★ 出图期间往会话报步数（最多 2 条，见 _chat_progress）。
        #   放在 try 外面：无论成功失败都要在 finally 里 cancel 掉。
        progress_task: asyncio.Task | None = None
        if self._conf_bool("sd_progress_in_chat", True):
            progress_task = asyncio.create_task(self._chat_progress(event))

        # ★ 画风：把名字解析成"具体文件 + 强度"，worker 只负责装卸。
        try:
            picked = resolve_loras(
                style,
                self._lora_list(),
                allow_nsfw=self._conf_bool("sd_allow_nsfw_lora", True),
                default_scale=self._conf_float("sd_lora_scale", 0.8) or 0.8,
            )
        except LoraError as exc:
            if progress_task is not None:
                progress_task.cancel()
            return f"画图失败：{exc}"

        # ★ 参考图：SDXL 的 img2img 比 SD1.5 强得多，所以这条路重新打开，
        #   但【由模型决定用不用】—— reference 留空就是普通文生图。
        ref_bytes = await self._reference_bytes(reference, event)
        # ★ 2026-10-19 改：要了参考图却拿不到时【不能静默画一张文生图】。
        #   之前就是这样：用户说"用图生图"，插件找不到图（图在被引用的消息里），
        #   于是一声不响画了新图，用户看到的是一张毫无关系的东西。
        if self._wants_reference(reference) and not ref_bytes:
            if progress_task is not None:
                progress_task.cancel()
            logger.info(f"[toolbox] reference={reference!r} 但没取到图，已明确拒绝而不是画文生图")
            return (
                f"画图失败：你要求用参考图（reference={reference}），但我这边没取到可用的图。\n"
                "· 图要【直接发在这条消息里】，或者【引用刚才那条带图的消息】再说；\n"
                "· 如果图是更早的消息、或者只是个链接，我拿不到 —— 请让用户重发一张图；\n"
                "· 想改我自己上一张生成的图，把 reference 改成 \"last\"。\n"
                "（这次没有出图，避免画出一张和参考图无关的新图。）"
            )
        opts_img: dict = {"loras": picked}
        if dims:
            opts_img["width"], opts_img["height"] = dims
        # ★ 2026-10-19：把 AI 能调的都放出来（以前只有 prompt/style/reference/size）
        if str(negative or "").strip():
            opts_img["negative"] = str(negative).strip()
        if str(sampler or "").strip():
            opts_img["sampler"] = str(sampler).strip()
        try:
            if int(steps or 0) > 0:
                opts_img["steps"] = max(1, min(150, int(steps)))
            if float(cfg or 0) > 0:
                opts_img["cfg"] = max(0.0, min(30.0, float(cfg)))
        except (TypeError, ValueError):
            return "画图失败：steps 要是整数、cfg 要是数字。"
        if int(seed if seed is not None else -1) >= 0:
            opts_img["seed"] = int(seed)
        if str(model or "").strip():
            try:
                opts_img["checkpoint"] = self._resolve_model(model)
            except ModelError as exc:
                return f"画图失败：{exc}"
        if ref_bytes:
            opts_img["image"] = base64.b64encode(ref_bytes).decode("ascii")
            strength_val = float(strength or 0) or (self._conf_float("img2img_strength", 0.6) or 0.6)
            opts_img["strength"] = max(0.05, min(1.0, strength_val))
            logger.info(
                f"[toolbox] 走参考图生图：{len(ref_bytes) // 1024} KB，"
                f"画风={[p.get('name') for p in picked] or '无'}，强度={opts_img['strength']}"
            )

        # ★ finally 里 cancel：以前只在成功路径上 cancel，出图抛别的异常时
        #   这个任务会继续跑到 deadline，对着已经结束的会话报进度。
        try:
            data, filename = await self._render_image(prompt, opts_img)
        except SDBackendError as exc:
            logger.warning(f"[toolbox] 画图失败: {exc}")
            return f"画图失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 画图异常: {exc}", exc_info=True)
            return f"画图失败：{type(exc).__name__}: {exc}"
        finally:
            if progress_task is not None:
                progress_task.cancel()

        path = self._save_generated(
            data,
            filename,
            "sd",
        )
        self._last_image = (path, filename)
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Image.fromFileSystem(str(path))]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 发送生成的图失败: {exc}", exc_info=True)
            return f"图画好了但发送失败：{type(exc).__name__}: {exc}"

        logger.info(
            f"[toolbox] 已发送生成的图 {path.name}（{len(data):,} 字节）→ "
            f"{event.unified_msg_origin}"
        )
        # ★ 这次用到的关键参数一并报给模型：它自己调的，也该知道自己调成了什么。
        if opts_img.get("checkpoint"):
            self._model_override = str(opts_img["checkpoint"])
        used = [
            f"底模 {os.path.basename(self._current_model())}",
            f"画风 {[p.get('name') for p in picked] or '无'}",
            f"{opts_img.get('width')}×{opts_img.get('height')}",
            f"{opts_img.get('steps') or self._conf_int('sd_steps', 25)} 步",
            f"CFG {opts_img.get('cfg') or self._conf_float('sd_cfg', 5.5)}",
            f"采样器 {opts_img.get('sampler') or self._conf_str('sd_sampler') or 'euler'}",
            f"seed {opts_img.get('seed') if opts_img.get('seed') is not None else '随机'}",
        ]
        if opts_img.get("image"):
            used.append(f"图生图强度 {opts_img.get('strength')}")
        return (
            f"图已经画好并发到会话里了（{filename}，{len(data):,} 字节）。\n"
            f"参数：{' · '.join(used)}\n"
            "不要再重复调用本工具；用户还想要就换一个新的画面描述。"
        )

    @filter.llm_tool(name="draw_kb_search")
    async def draw_kb_search(
        self,
        event: AstrMessageEvent,
        query: str = "",
        scope: str = "",
        limit: int = 0,
    ) -> str:
        """查本机标签库（28.5 万条 Danbooru 标签：tag ｜ 中文名 ｜ 帖数 ｜ 释义）。

        ★ 写 prompt 前不确定某个概念用哪个标签、或想确认「存不存在 / 放正面还是负面」时先查它。
          默认只查【核心库】（动作/表情/身体/服饰/物件/场景/风格/常用）；
          角色 / 作品 / 画师 / 长尾 在【全量库】里，核心库查不到时再加 scope="full"。

        Args:
            query(string): 关键词，中文或英文都行（"银发" / "kiss" / "哭泣" / "制服"）。
                留空 = 列出核心库有哪些文件、各多少行。
            scope(string): 留空或 "core" = 只查核心库（默认）；"full" = 查全量库。
            limit(int): 最多返回几条标签；0 = 用配置默认值（默认 8）。
        """
        gate = self._draw_gate_error(event, "draw_kb_search")
        if gate:
            return gate
        if not self._conf_bool("kb_enabled", True):
            return "标签库功能已关闭（配置项 kb_enabled）。需要时让管理员打开再试。"

        want_scope = (scope or "").strip().lower() or str(
            self._conf_str("kb_default_scope") or "core"
        ).strip().lower()
        if want_scope not in kb_index.SCOPES:
            want_scope = "core"
        allow_full = self._conf_bool("kb_allow_full", True)

        try:
            want = int(limit or 0) or int(self._conf_int("kb_default_limit", 8) or 8)
        except (TypeError, ValueError):
            want = 8
        want = max(1, min(20, want))

        try:
            max_chars = int(self._conf_int("kb_max_chars", 600) or 600)
        except (TypeError, ValueError):
            max_chars = 600
        max_chars = max(200, min(4000, max_chars))

        try:
            catalog_limit = int(self._conf_int("kb_catalog_limit", 20) or 20)
        except (TypeError, ValueError):
            catalog_limit = 20
        catalog_limit = max(5, min(60, catalog_limit))

        return kb_index.render(
            query,
            scope=want_scope,
            limit=want,
            max_chars=max_chars,
            catalog_limit=catalog_limit,
            allow_full=allow_full,
        )

    @filter.llm_tool(name="switch_model")
    async def switch_model(self, event: AstrMessageEvent, model: str = "") -> str:
        """换一个画图用的底模（checkpoint），换完对之后的出图立刻生效。★ 必须先调用 drawing_tools。

        Args:
            model(string): 底模名字（可不带 .safetensors，大小写不敏感，也可以只写
                其中一段，比如 "noob"）。可选值见描述末尾的【可选底模】清单。
                留空 = 只列出有哪些底模可选，不换。
        """
        gate = self._draw_gate_error(event, "switch_model")
        if gate:
            return gate
        models = self._model_list()
        if not models:
            return (
                "换模型失败：底模目录里没有可用模型。"
                f"（目录：{self._model_dir()}）"
            )
        if not (model or "").strip():
            lines = [f"现在用的是：{os.path.basename(self._current_model()) or '（未配置）'}"]
            lines.append("可选底模（把名字填进 model 参数）：")
            for m in models:
                lines.append(f"· {m['name']}（{round(m['size_mb'] / 1024, 2)} GB）")
            return "\n".join(lines)

        try:
            path = self._resolve_model(model)
        except ModelError as exc:
            return f"换模型失败：{exc}"

        # ★ 先换、再改"当前模型"：换失败时不能把状态改成一个其实没加载上的模型，
        #   否则后面每张图都会卡在同一个坏文件上，而且报错点看起来毫无关联。
        try:
            info = await self._sd_client().switch_model(path)
        except SDBackendError as exc:
            logger.warning(f"[toolbox] 换底模失败: {exc}")
            return f"换模型失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 换底模异常: {exc}", exc_info=True)
            return f"换模型失败：{type(exc).__name__}: {exc}"

        self._model_override = path
        name = os.path.basename(path)
        cost = float(info.get("seconds") or 0)
        if info.get("switched"):
            logger.info(f"[toolbox] 已切换底模 {name}（{cost:.1f} 秒）")
            return (
                f"已切换底模：{name}（加载耗时 {cost:.1f} 秒）。"
                "之后的出图都用这个模型，直到再换或重载插件。"
                "现在可以接着调 generate_image 画图。"
            )
        return f"现在用的就是 {name}，没有重复加载（0 秒）。"

    # ------------------------------------------------------------------
    #  生图页面（让人也能直接用）
    # ------------------------------------------------------------------

    def _imggen_defaults(self) -> dict:
        """生图页的默认参数（全部来自 sd_* 配置）。"""
        return {
            "backend": "standalone",
            "width": self._conf_int("sd_width", 512) or 512,
            "height": self._conf_int("sd_height", 512) or 512,
            "steps": self._conf_int("sd_steps", 25),
            "cfg": self._conf_float("sd_cfg", 7.0),
            "model": os.path.basename(self._current_model()) or "（未配置）",
            "sampler": self._conf_str("sd_sampler") or "euler",
        }

    async def api_imggen_status(self):
        """生图页初始化：默认参数 + 后端状态。"""
        try:
            info = self._imggen_defaults()
            info["ok"] = True
            info["worker_running"] = bool(self._sd and self._sd.running())
            # 给页面下拉框用：有哪些底模可选
            try:
                info["models"] = [
                    {"name": m["name"], "size_mb": m["size_mb"], "current": m["current"]}
                    for m in self._model_list()
                ]
                info["model_dir"] = str(self._model_dir())
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[toolbox] 取底模列表失败: {exc}")
                info["models"] = []
            # 画幅上限也透出去：页面上的提示和输入框上限跟着配置走
            try:
                cap, _px, mp, warn = self._size_limits()
                info["max_side"] = cap
                info["max_mp"] = round(mp, 2)
                info["warn_side"] = warn
            except Exception:  # noqa: BLE001
                pass
            # 给页面下拉框用：有哪些画风可选
            try:
                info["styles"] = [
                    {"name": lo.name, "rating": lo.rating, "size_mb": lo.size_mb}
                    for lo in self._lora_list()
                    if self._conf_bool("sd_allow_nsfw_lora", True) or lo.rating == "SFW"
                ]
                info["lora_status"] = str((self._conf or {}).get("sd_lora_status") or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[toolbox] 取画风列表失败: {exc}")
                info["styles"] = []
            # ★ 把 worker 的 LoRA 状态透出去：LoRA 加载失败时是"静默降级"
            #   （只记日志、继续无 LoRA 出图），用户只会觉得"风格没出来"。
            #   页面上显示一行，有没有挂上一眼就看得见。
            try:
                if self._sd is not None and self._sd.running():
                    health = await asyncio.to_thread(self._sd._health_sync, 2.0)
                    if isinstance(health, dict):
                        info["lora"] = health.get("lora") or ""
                        info["lora_scale"] = health.get("lora_scale") or 0
                        info["arch"] = health.get("arch") or ""
                        info["offload"] = bool(health.get("offload"))
            except Exception:  # noqa: BLE001
                pass
            info["idle_timeout"] = self._conf_int("sd_idle_timeout", 300)
            info["sd_ready"] = bool(
                self._conf_str("sd_python") and self._conf_str("sd_checkpoint")
            )
            return json_response(info)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 生图页 status 失败: {exc}", exc_info=True)
            return json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

    async def api_imggen_generate(self):
        """生图页出图。返回 base64，前端直接塞进 <img>。"""
        try:
            payload = await web_request.json(default={}) or {}
        except Exception as exc:  # noqa: BLE001
            return json_response(
                {"ok": False, "error": f"请求体不是 JSON: {exc}"}, status_code=400
            )
        if not isinstance(payload, dict):
            return json_response({"ok": False, "error": "请求体应是 JSON 对象"}, status_code=400)

        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return json_response({"ok": False, "error": "prompt 不能为空"}, status_code=400)

        opts: dict = {}
        for key in ("width", "height", "steps"):
            if payload.get(key) in (None, ""):
                continue
            try:
                opts[key] = int(payload[key])
            except (TypeError, ValueError):
                return json_response(
                    {"ok": False, "error": f"{key} 必须是整数"}, status_code=400
                )
        if payload.get("cfg") not in (None, ""):
            try:
                opts["cfg"] = float(payload["cfg"])
            except (TypeError, ValueError):
                return json_response({"ok": False, "error": "cfg 必须是数字"}, status_code=400)
        if payload.get("seed") not in (None, ""):
            try:
                opts["seed"] = int(payload["seed"])
            except (TypeError, ValueError):
                return json_response(
                    {"ok": False, "error": "seed 必须是整数"}, status_code=400
                )
        # 额外负面词（可选）：追加到配置那套后面，不覆盖
        negative = str(payload.get("negative") or "").strip()
        if negative:
            opts["negative"] = negative

        # 画风（可选）：和模型调 generate_image 的 style 同一套解析逻辑
        style = str(payload.get("style") or "").strip()
        if style:
            try:
                opts["loras"] = resolve_loras(
                    style,
                    self._lora_list(),
                    allow_nsfw=self._conf_bool("sd_allow_nsfw_lora", True),
                    default_scale=self._conf_float("sd_lora_scale", 0.8) or 0.8,
                )
            except LoraError as exc:
                return json_response({"ok": False, "error": str(exc)}, status_code=400)

        # 底模（可选）：页面上选的那个。留空 = 用当前底模。
        model = str(payload.get("model") or "").strip()
        if model:
            try:
                opts["checkpoint"] = self._resolve_model(model)
            except ModelError as exc:
                return json_response({"ok": False, "error": str(exc)}, status_code=400)

        # ★ 画幅硬上限对页面同样有效：这里是普通 HTTP 接口，没有"问用户"这一层，
        #   所以 1920×1920 这种必须当场拒 —— 否则一条手滑请求就能把显存打爆。
        if opts.get("width") or opts.get("height"):
            cap, px, _mp, _warn = self._size_limits()
            try:
                check_canvas(
                    int(opts.get("width") or self._conf_int("sd_width", 720) or 720),
                    int(opts.get("height") or self._conf_int("sd_height", 1024) or 1024),
                    cap,
                    px,
                )
            except ValueError as exc:
                return json_response({"ok": False, "error": str(exc)}, status_code=400)

        # 参考图生图（可选）：给了 image 就走 img2img
        img = payload.get("image")
        if isinstance(img, str) and img.strip():
            opts["image"] = img.strip()
            if payload.get("strength") not in (None, ""):
                try:
                    strength = float(payload["strength"])
                except (TypeError, ValueError):
                    return json_response(
                        {"ok": False, "error": "strength 必须是数字"}, status_code=400
                    )
                if not 0.0 < strength <= 1.0:
                    return json_response(
                        {"ok": False, "error": "strength 要在 (0, 1] 之间"},
                        status_code=400,
                    )
                opts["strength"] = strength

        t0 = time.time()
        try:
            data, filename = await self._render_image(prompt, opts)
        except SDBackendError as exc:
            logger.warning(f"[toolbox] 生图页生成失败: {exc}")
            return json_response({"ok": False, "error": str(exc)}, status_code=500)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 生图页生成异常: {exc}", exc_info=True)
            return json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

        elapsed = round(time.time() - t0, 1)
        # ★ 页面选的底模要变成"当前底模"：否则下一次 AI 出图会切回配置默认，
        #   白等十几秒重载一次，用户还会觉得"我刚换的模型怎么没生效"。
        if opts.get("checkpoint"):
            self._model_override = str(opts["checkpoint"])
        path = self._save_generated(
            data,
            filename,
            "sd",
        )
        self._last_image = (path, filename)
        logger.info(f"[toolbox] 生图页出图 {filename} {len(data):,} 字节 {elapsed} 秒")
        return json_response(
            {
                "ok": True,
                "image": "data:image/png;base64," + base64.b64encode(data).decode(),
                "filename": filename,
                "bytes": len(data),
                "elapsed": elapsed,
                "prompt": prompt,
                "model": os.path.basename(self._current_model()),
            }
        )

    async def api_imggen_progress(self):
        """生图进度（步数）。没在跑就回 busy=False。"""
        try:
            if self._sd is None:
                return json_response(
                    {"ok": True, "busy": False, "step": 0, "total": 0, "elapsed": 0.0}
                )
            return json_response(await self._sd.progress())
        except Exception as exc:  # noqa: BLE001
            return json_response({"ok": False, "error": str(exc)}, status_code=500)

    async def api_imggen_last(self):
        """把最近一次生成的图当文件返回 —— 页面用它下载。

        ★ 为什么不让前端自己 a.click() 下载：插件页面跑在
          `sandbox="allow-scripts allow-forms allow-downloads"` 的 iframe 里，
          【没有 allow-same-origin】—— 页面是不透明源，手写 data-URL 下载
          实测无效（点「下载原图」没反应）。让父页面用桥接的 download()
          来取这个接口就稳了：父页面不受该 sandbox 限制。
        """
        if not self._last_image:
            return json_response(
                {"ok": False, "error": "还没有生成过图"}, status_code=404
            )
        path, filename = self._last_image
        if not Path(path).exists():
            return json_response(
                {"ok": False, "error": "临时文件已被清理，重新生成一张即可"},
                status_code=404,
            )
        return file_response(str(path), filename=filename, content_type="image/png")

    @filter.on_llm_request()
    async def _gate_local_model(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """本地小模型不给绘图工具（提示词质量差，画出来是废图）。"""
        self._hide_image_tool_for_local(req, event)
