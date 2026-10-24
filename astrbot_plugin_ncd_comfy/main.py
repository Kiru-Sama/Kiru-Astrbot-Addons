"""NCD 出图 · ComfyUI：把你调好的工作流接成 AstrBot 的绘图后端。

形态：
  · 后端是最小 ComfyUI 便携版（按需拉起、空闲卸模型/关进程）
  · 出图用你给的四个 API 格式工作流（workflows/*.json），不另拼节点图
  · 模型只填参数：正面/负面/宽高/步数/CFG/种子/底模/LoRA

三个约定：
  · 换工作流、换底模、第一次出图 → 先往会话发一条"要加载，稍等"的提示
  · 空闲 5 分钟让 ComfyUI 卸载模型（还显存），30 分钟没动静才关进程
  · 只杀插件自己拉起的进程；端口上已有别人开的 ComfyUI 就直接用

取代 astrbot_plugin_ncd_draw（diffusers 直连那套）：那个只认 SDXL 单文件，
这个能用 ComfyUI 的完整节点生态，模型/工作流都不用改代码。
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

import astrbot.api.message_components as Comp
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.command import GreedyStr

try:  # 审核模块（内部用，不注册成聊天工具）
    from . import moderation
except ImportError:  # 离线单测
    import moderation  # type: ignore
from astrbot.core.message.message_event_result import MessageChain

try:  # AstrBot 以包的形式加载插件
    from . import model_paths, presets, sizing, tts
    from .comfy_backend import ComfyClient, ComfyError
except ImportError:  # 离线单测
    import model_paths
    import presets
    import sizing
    import tts
    from comfy_backend import ComfyClient, ComfyError

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR_NAME = os.path.basename(PLUGIN_DIR)
#: 面板总线的状态文件（monitor 只读它）
BUS_STATE = (
    Path(PLUGIN_DIR).parents[2] / "data" / "plugin_data" / "ncd_bus" / "state" / "comfy.json"
)

# 出错时给模型的保底建议：别让它继续瞎试
RECOMMENDED_HINT = (
    "\n★ 非用户特别要求，直接用预设 sdxl（默认底模 waiIllustriousSDXL_v170，不叠 LoRA），"
    "不要手动指定 model / lora。"
)

TOOL_NAMES = ("comfy_tools", "comfy_generate", "comfy_models", "comfy_status", "comfy_say", "comfy_output", "comfy_gallery", "comfy_prompt_help")

TOOL_SUMMARY = {
    "comfy_tools": "comfy_tools() —— 出图入口（必须先调）：四个工作流、参数范围、当前默认值",
    "comfy_generate": "comfy_generate(prompt, ...) —— 按预设工作流出图并发到会话",
    "comfy_models": "comfy_models(kind) —— 列底模 / 列 LoRA",
    "comfy_status": "comfy_status() —— ComfyUI 起没起来、显存、队列",
    "comfy_say": "comfy_say(text) —— 用参考音频的克隆音色合成一条语音发出去（慢，按需用）",
    "comfy_output": "comfy_output(index, hours, file) —— 发本会话画过的图（不用重画）",
    "comfy_gallery": "comfy_gallery(query, limit) —— 按提示词/时间翻本会话的历史图",
    "comfy_prompt_help": "comfy_prompt_help(query, category=sfw|nsfw|all, limit) —— 按关键词查本地画图提示词法典",
}

# 后台任务里会再次调用 comfy_generate，用这个标记避免无限套娃
_ASYNC_RUN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ncd_comfy_async_job", default=False
)
HISTORY_FILE = "history.json"

GATE_ERROR = (
    "出图失败：ComfyUI 的绘图操作必须【先调用 comfy_tools】（每个会话调一次即可）。\n"
    "它会告诉你四个工作流各自的底模、默认画幅/步数/CFG，以及参数范围。\n"
    "现在请先调用 comfy_tools，然后再调用 {tool}；这次没有出图。"
)


class NcdComfyPlugin(star.Star):
    def __init__(
        self, context: star.Context, config: dict[str, Any] | None = None
    ) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf: dict[str, Any] = config or {}
        self._client_obj: ComfyClient | None = None
        self._client_key: tuple | None = None
        self._revealed: dict[str, float] = {}
        self._render_lock = asyncio.Lock()
        # 正在排队等出图的数量（对话不排队，生图才排队）
        self._gen_waiting = 0
        # 上次用的工作流/底模：用来判断"要不要先发一条切换提示"
        self._last_preset: str | None = None
        self._last_model: str | None = None
        # 上一张生成的图（reference="last" 做图生图时用）
        # 每个会话的“上一张图”（reference="last" 用；绝不能全局共用）
        self._last_image_bytes: dict[str, bytes] = {}
        # 视觉能力（由 modalities 决定）与“探测失败”分开缓存
        self._vision_caps: dict[str, tuple[float, bool]] = {}
        self._vision_probe_fail: dict[str, float] = {}
        # 模型清单缓存（目录扫一遍要几百毫秒，别每次出图都扫）
        self._scan_cache: dict[str, tuple[float, list[dict]]] = {}
        # 参考音频上传缓存（改了文件会自动重传）
        self._voice_ref_key: str | None = None
        self._voice_ref_name: str = ""
        # 刚发过的语音文本（同一轮去掉重复文字用）
        self._spoken: dict[str, tuple[str, float]] = {}
        # comfy_output 用：每个会话已发过的文件、最近一次生成的图
        self._sent_files: dict[str, list[str]] = {}
        self._sent_at: dict[str, dict[str, float]] = {}
        self._last_output: dict[str, str] = {}
        # 面板总线：最近产物 / 审核计数 / 当前进度 / 本次任务
        self._recent: list[dict] = []
        self._audit_counts = {"pass": 0, "reject": 0}
        self._progress: dict | None = None
        self._job: dict = {}
        # 空闲时也要能显示上一次用的工作流/底模
        self._last_job: dict = {}
        # 审核明细（最多 20 条；被拦的留一张 96px 小图，方便面板核对）
        self._audit_recent: list[dict] = []
        self._audit_thumb_dir = Path(PLUGIN_DIR) / "data" / "audit"
        self._queue_pos: int | None = None
        self._state_ts = 0.0
        # 反风控：每个会话上次发图/发语音的时间、上次任意消息时间
        self._last_image_at: dict[str, float] = {}
        self._last_voice_at: dict[str, float] = {}
        self._last_reply_at: dict[str, float] = {}
        # 云端 API 下的异步出图任务：不让回复等画完
        self._bg_tasks: set[asyncio.Task] = set()
        self._bg_umo: dict[asyncio.Task, str] = {}
        # 异步审核失败后，下一轮给模型的提醒（umo -> (ts, note)）
        self._pending_notes: dict[str, tuple[float, str]] = {}
        # 审核失败自动拉起 bot 回复：每会话 10 分钟最多一次，防止循环
        self._audit_nudge_at: dict[str, float] = {}
        # 异步图写入会话历史时串行，避免并发写坏 content
        self._conv_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    #  配置
    # ------------------------------------------------------------------
    def _conf_str(self, key: str, default: str = "") -> str:
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
            logger.warning(f"[comfy] 配置 {key}={raw!r} 不是整数，用默认值 {default}")
            return default

    def _conf_float(self, key: str, default: float) -> float:
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(f"[comfy] 配置 {key}={raw!r} 不是数字，用默认值 {default}")
            return default

    def _conf_bool(self, key: str, default: bool) -> bool:
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")

    # ------------------------------------------------------------------
    #  后端客户端
    # ------------------------------------------------------------------
    def _log_path(self) -> Path:
        return Path(PLUGIN_DIR) / "data" / "comfy.log"

    @staticmethod
    def _temp_root() -> str:
        """AstrBot 临时目录根：出图后会在这里留一份 comfy_* 副本。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            return str(Path(get_astrbot_temp_path()))
        except Exception:  # noqa: BLE001
            return tempfile.gettempdir()

    @staticmethod
    def _bytes_print(data: bytes) -> str:
        """内容指纹：大小 + 前 64KB，够区分同一张图的不同副本。"""
        try:
            h = hashlib.sha1()
            h.update(str(len(data)).encode())
            h.update(data[:65536])
            return h.hexdigest()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _file_print(path: str) -> str:
        try:
            h = hashlib.sha1()
            h.update(str(os.path.getsize(path)).encode())
            with open(path, "rb") as fh:
                h.update(fh.read(65536))
            return h.hexdigest()
        except OSError:
            return ""

    def _io_dirs(self) -> tuple[str, str]:
        """ComfyUI 的 output/temp 目录 → AstrBot 临时目录（有定时清理）。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            base = Path(get_astrbot_temp_path())
        except Exception:  # noqa: BLE001
            base = Path(tempfile.gettempdir())
        out = base / "comfy_out"
        tmp = base / "comfy_tmp"
        for d in (out, tmp):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                return "", ""
        return str(out), str(tmp)

    def _client(self) -> ComfyClient:
        out_dir, tmp_dir = self._io_dirs()
        key = (
            self._conf_str("comfy_dir"),
            self._conf_str("comfy_python"),
            self._conf_int("comfy_port", 8199),
        )
        if self._client_obj is not None and self._client_key != key:
            self._client_obj.stop()
            self._client_obj = None
        if self._client_obj is None:
            self._client_obj = ComfyClient(
                comfy_dir=self._conf_str("comfy_dir"),
                python=self._conf_str("comfy_python"),
                port=self._conf_int("comfy_port", 8199),
                # ★ 两档回收：先卸模型（5 分钟），再关进程（30 分钟）
                unload_timeout=float(self._conf_int("comfy_unload_timeout", 300)),
                idle_timeout=float(self._conf_int("comfy_idle_timeout", 1800)),
                startup_timeout=float(self._conf_int("comfy_startup_timeout", 240)),
                gen_timeout=float(self._conf_int("comfy_timeout", 300)),
                extra_args=self._conf_str("comfy_extra_args"),
                output_dir=out_dir,
                temp_dir=tmp_dir,
                log_path=self._log_path(),
            )
            self._client_key = key
        return self._client_obj

    def _save_audit_thumb(self, data: bytes) -> str:
        """被拦的图只留 96px 小图（不落原图），给面板核对用。"""
        try:
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((96, 96))
            self._audit_thumb_dir.mkdir(parents=True, exist_ok=True)
            path = self._audit_thumb_dir / f"{uuid.uuid4().hex[:12]}.jpg"
            img.save(path, "JPEG", quality=70)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[comfy] 审核缩略图失败: {exc}")
            return ""

    async def _queue_poller(self) -> None:
        """每秒问一次 ComfyUI 队列：0=正在跑，N=前面还有 N 个。"""
        while True:
            try:
                client = self._client()
                pid = str(getattr(client, "last_prompt_id", "") or "")
                if pid:
                    self._queue_pos = await client.queue_position(pid)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.0)

    def _tool_image_result(self, text: str, data: bytes) -> Any:
        """把图一起放进工具结果：模型当轮就能看见自己画的图。

        历史里不会留图（工具消息只存文字），所以不会每轮重复烧 token；
        只在出图这一轮让模型自查一次。
        """
        if not self._conf_bool("comfy_self_view", True):
            return text
        try:
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            payload = base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[comfy] 回传图准备失败（只回文字）: {exc}")
            return text
        return CallToolResult(
            content=[
                TextContent(type="text", text=text),
                ImageContent(type="image", data=payload, mimeType="image/jpeg"),
            ]
        )

    def _jitter(self) -> float:
        """发送前随机停顿：0.4~2.0 秒（可在配置里改）。"""
        import random

        lo = self._conf_float("comfy_send_jitter_min", 0.4)
        hi = self._conf_float("comfy_send_jitter_max", 2.0)
        lo = max(0.0, min(lo, hi))
        return random.uniform(lo, max(lo, hi))

    async def _pace_send(self) -> None:
        delay = self._jitter()
        if delay > 0:
            await asyncio.sleep(delay)

    def _image_allowed(self, umo: str, event=None) -> tuple[bool, int]:
        """每会话图片最小间隔；返回 (是否允许, 还要等几秒)。

        群聊收紧（默认 300 秒），私聊放宽（默认 30 秒）——
        私聊是自用迭代，群聊才是举报和风控的高发区。
        """
        if event is not None and self._is_group_chat(event):
            gap = self._conf_float("comfy_image_interval", 300.0)
        else:
            gap = self._conf_float("comfy_image_interval_private", 30.0)
        last = self._last_image_at.get(umo) or 0
        wait = int(gap - (time.time() - last))
        return wait <= 0, max(0, wait)

    def _voice_allowed(self, umo: str) -> tuple[bool, int]:
        gap = self._conf_float("comfy_voice_interval", 60.0)
        last = self._last_voice_at.get(umo) or 0
        wait = int(gap - (time.time() - last))
        return wait <= 0, max(0, wait)

    @filter.on_decorating_result()
    async def _pace_all_replies(self, event: AstrMessageEvent) -> None:
        """给所有回复加一点随机延迟，并保证同一会话最小间隔。"""
        if not self._conf_bool("comfy_pace_replies", True):
            return
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            gap = self._conf_float("comfy_reply_min_gap", 1.2)
            last = self._last_reply_at.get(umo) or 0
            wait = max(0.0, gap - (time.time() - last))
            await asyncio.sleep(wait + self._jitter())
            self._last_reply_at[umo] = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[comfy] 回复节流失败（忽略）: {exc}")

    def _publish_state(self, force: bool = False) -> None:
        """把当前状态原子写到总线文件（面板 1 秒读一次）。"""
        now = time.time()
        if not force and now - self._state_ts < 0.5:
            return
        self._state_ts = now
        client = self._client()
        busy = bool(getattr(client, "_busy", False))
        data = {
            "schema_version": 1,
            "ts": now,
            "title": "本地生成",
            "status": {
                "running": busy,
                "preset": (self._job or getattr(self, "_last_job", {})).get("preset") or "",
                "model": (self._job or getattr(self, "_last_job", {})).get("model") or "",
                "queue_running": 1 if busy else 0,
                "queue_pending": max(0, int(getattr(self, "_gen_waiting", 0)) - (1 if busy else 0)),
                "port": self._conf_int("comfy_port", 8199),
                "position": self._queue_pos,
            },
            "progress": self._progress,
            "recent": self._recent[-8:],
            "audit": dict(self._audit_counts),
            "audit_recent": self._audit_recent[-10:],
        }
        try:
            BUS_STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = BUS_STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, BUS_STATE)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[comfy] 写面板状态失败: {exc}")

    async def _on_progress(self, info: dict) -> None:
        self._progress = info
        self._publish_state()

    def _remember_recent(self, entry: dict) -> None:
        self._recent.append(entry)
        del self._recent[:-12]
        self._publish_state(force=True)

    # ------------------------------------------------------------------
    #  模型清单
    # ------------------------------------------------------------------
    def _yaml_path(self) -> Path:
        configured = self._conf_str("comfy_extra_paths")
        if configured:
            return Path(configured)
        return model_paths.default_yaml(self._conf_str("comfy_dir"))

    def _categories(self, kind: str) -> list[Path]:
        """kind = checkpoints / diffusion_models / loras"""
        return model_paths.parse_yaml(self._yaml_path()).get(kind, [])

    def _scan(self, preset: str) -> list[dict]:
        """预设对应的底模清单（按预设的 model_dir 过滤 + 60 秒缓存）。

        ★ 2026-10-22：必须按目录过滤。原来 kind 相同的模型全混在一起，
          Krea2 预设能选到 Anima 的底模，采样时直接报 shape 不匹配。
        """
        meta = presets.PRESETS[preset]
        kind = "checkpoints" if meta.get("model_kind") == "checkpoint" else "diffusion_models"
        key = f"model:{kind}:{preset}"
        hit = self._scan_cache.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        items = model_paths.scan(
            self._categories(kind),
            min_mb=max(0.0, self._conf_float("comfy_min_model_gb", 0.5)),
        )
        want = str(meta.get("model_dir") or "").lower()
        if want:
            items = [m for m in items if m["name"].lower().startswith(want + "\\")]
        self._scan_cache[key] = (time.time(), items)
        return items

    def _scan_loras(self, preset: str | None = None) -> list[dict]:
        """LoRA 清单；给了 preset 就只保留该族的目录。"""
        key = f"lora:{preset or '*'}"
        hit = self._scan_cache.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        items = model_paths.scan(self._categories("loras"), min_mb=0.0)
        if preset:
            dirs = [d.lower() for d in (presets.PRESETS[preset].get("lora_dirs") or ())]
            if dirs:
                items = [
                    m for m in items
                    if any(m["name"].lower().startswith(d + "\\") for d in dirs)
                ]
        self._scan_cache[key] = (time.time(), items)
        return items

    @staticmethod
    def _lora_names(items: list[dict], limit: int = 6) -> str:
        names = [os.path.basename(m["name"]) for m in items[:limit]]
        more = "" if len(items) <= limit else "…"
        return "、".join(names) + more

    def _resolve_name(self, want: str, items: list[dict], label: str) -> str:
        """把模型/LoRA 名字解析成 ComfyUI 认的相对路径。

        支持：完整相对路径、文件名、名字里的一段（大小写不敏感）。
        """
        text = str(want or "").strip().replace("/", "\\")
        names = [m["name"] for m in items]
        if not text:
            raise ComfyError(f"没给{label}名字")
        low = text.lower()
        for name in names:
            if name.lower() == low:
                return name
        for name in names:
            if os.path.basename(name).lower() == low:
                return name
        for name in names:
            if low in name.lower():
                return name
        sample = "、".join(os.path.basename(n) for n in names[:10])
        raise ComfyError(
            f"没有这个{label}：{want}。可用的有：{sample}"
            + ("……" if len(names) > 10 else "")
        )

    # ---- 本地模型互斥：出图/语音前先让 LLM 安静下来 ----
    _GPU_GATE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        "data", "plugin_data", "ncd_gpu_gate.json",
    )

    def _gpu_read(self) -> dict:
        try:
            with open(self._GPU_GATE, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _gpu_write(self, **fields) -> None:
        try:
            data = self._gpu_read()
            data.update(fields)
            os.makedirs(os.path.dirname(self._GPU_GATE), exist_ok=True)
            with open(self._GPU_GATE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    async def _wait_llm_idle(self, timeout: float = 90.0) -> None:
        """本地模型在推理时别开画（会抢显存）。最近 2 秒有 LLM 请求就等。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            last = float(self._gpu_read().get("llm_ts") or 0)
            if time.time() - last >= 2.0:
                return
            await asyncio.sleep(0.5)

    # ---- 设备繁忙检测：游戏/桌面占显存时提醒模型 ----
    async def _comfy_own_gb(self) -> float:
        """ComfyUI 自己占的专用显存（GB）。

        ★ /system_stats 的 torch_vram_total 在 cudaMallocAsync 下基本不涨，
          不能拿它当模型占用；优先查 Windows 按进程的 GPU 显存计数器。
        """
        client = getattr(self, "_client_obj", None)
        if client is None:
            try:
                client = self._client()
            except Exception:  # noqa: BLE001
                client = None
        pid = 0
        try:
            pid = int(getattr(getattr(client, "_proc", None), "pid", 0) or 0)
        except Exception:  # noqa: BLE001
            pid = 0

        if os.name == "nt":
            ps = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
            if ps:
                script = (
                    f"$targetPid={pid}; "
                    "if (-not $targetPid) { "
                    "$targetPid=(Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -eq 'python.exe' -and "
                    "$_.CommandLine -match 'ComfyUI.*main\\.py' } | "
                    "Select-Object -First 1 -ExpandProperty ProcessId) }; "
                    "$v=0; "
                    "if ($targetPid) { "
                    "$v=(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory "
                    "-ErrorAction SilentlyContinue | "
                    "Where-Object { $_.Name -like ('pid_' + $targetPid + '_*') } | "
                    "Measure-Object DedicatedUsage -Sum).Sum }; "
                    "if ($v) { [string]$v } else { '0' }"
                )
                try:
                    proc = await asyncio.create_subprocess_exec(
                        ps,
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
                    lines = (out or b"").decode("utf-8", "replace").strip().splitlines()
                    if lines:
                        value = float(lines[-1].strip())
                        if value > 0:
                            return value / 1024**3
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[comfy] 读 ComfyUI 进程显存失败: {exc}")

        # 回退 1：torch 统计（某些后端/版本会准）
        try:
            stats = await asyncio.to_thread(client.healthy_sync) if client else None
            dev = ((stats or {}).get("devices") or [{}])[0]
            t = float(dev.get("torch_vram_total") or 0)
            f = float(dev.get("torch_vram_free") or 0)
            if t - f >= 1024**3:
                return max(0.0, (t - f) / 1024**3)
        except Exception:  # noqa: BLE001
            pass

        # 回退 2：模型已加载时按模型大小估（SDXL 约 6.5GB）
        if getattr(client, "_model_loaded", False):
            name = str(getattr(self, "_last_model", "") or "")
            try:
                if name and os.path.isfile(name):
                    return os.path.getsize(name) / 1024**3
            except OSError:
                pass
            return 6.5
        return 0.0

    async def _gpu_metrics(self) -> dict:
        """读 nvidia-smi + ComfyUI 自己的显存，估算外部占用。"""
        exe = shutil.which("nvidia-smi")
        if not exe:
            for candidate in (
                r"C:\Windows\System32\nvidia-smi.exe",
                r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            ):
                if os.path.isfile(candidate):
                    exe = candidate
                    break
        if not exe:
            return {}
        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                "--query-gpu=memory.total,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.5)
            line = (out or b"").decode("utf-8", "replace").strip().splitlines()
            if not line:
                return {}
            parts = [p.strip() for p in line[0].split(",")]
            total_mib = float(parts[0])
            free_mib = float(parts[1])
            util = float(parts[2]) if len(parts) > 2 else -1.0
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[comfy] 读 GPU 显存失败（忽略）: {exc}")
            return {}

        total_gb = total_mib / 1024.0
        free_gb = free_mib / 1024.0
        comfy_gb = await self._comfy_own_gb()
        external_gb = max(0.0, total_gb - free_gb - comfy_gb)
        return {
            "total_gb": total_gb,
            "free_gb": free_gb,
            "util": util,
            "comfy_gb": comfy_gb,
            "external_gb": external_gb,
        }

    def _busy_note(self, metrics: dict) -> str:
        """把繁忙指标翻成给模型看的一句话；不忙返回空串。"""
        if not metrics or not self._conf_bool("comfy_busy_detect", True):
            return ""
        ext = metrics.get("external_gb")
        free = float(metrics.get("free_gb") or 0)
        util = float(metrics.get("util") or -1)
        ext_limit = float(self._conf_int("comfy_busy_external_gb", 3))
        free_limit = float(self._conf_int("comfy_busy_free_gb", 2))
        util_limit = float(self._conf_int("comfy_busy_util", 70))

        reasons = []
        if ext is not None:
            if ext >= ext_limit:
                reasons.append(f"外部占用约 {ext:.1f}GB")
            if free <= free_limit and ext >= 1.0:
                reasons.append(f"可用显存仅 {free:.1f}GB")
            if util >= util_limit and ext >= 1.0:
                reasons.append(f"GPU {util:.0f}%")
        elif free <= 1.0 and util >= util_limit:
            # 拿不到 ComfyUI 自己的占用时只报最严重的：快没显存且 GPU 高负载
            reasons.append(f"可用显存仅 {free:.1f}GB，GPU {util:.0f}%")
        if not reasons:
            return ""
        return (
            "⚠ 设备繁忙（" + "，".join(reasons) + "）："
            "这次出图速度会明显降低，可以告诉用户稍等。"
        )

    async def _busy_note_for_current(self) -> str:
        if _ASYNC_RUN.get():
            return ""
        try:
            first = self._busy_note(await self._gpu_metrics())
            if not first:
                return ""
            # ComfyUI 卸载/加载模型时，进程显存计数可能慢一拍：
            # 只认连续两次都繁忙，避免瞬时误报。
            await asyncio.sleep(0.8)
            second = self._busy_note(await self._gpu_metrics())
            return second if second else ""
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[comfy] 繁忙检测失败（忽略）: {exc}")
            return ""

    def _queue_note(self) -> str:
        """排队人数提示；和设备显存繁忙是两回事。"""
        if not self._conf_bool("comfy_busy_detect", True):
            return ""
        limit = self._conf_int("comfy_busy_queue", 3)
        if limit <= 0:
            return ""
        existing = max(
            len(getattr(self, "_bg_tasks", set()) or ()),
            int(getattr(self, "_gen_waiting", 0) or 0),
        )
        if existing <= limit:
            return ""
        return (
            f"⏳ 排队较多（当前共有 {existing} 个出图任务在画/排队）："
            "这次出图会慢一些，可以告诉用户稍等。"
        )

    # ---- 显存换手：本地模型 ↔ ComfyUI ----
    def _lms_exe(self) -> str:
        """找 LM Studio 的 lms 命令。"""
        configured = self._conf_str("lmstudio_lms")
        if configured and os.path.isfile(configured):
            return configured
        local = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms.exe")
        if os.path.isfile(local):
            return local
        return "lms"

    @staticmethod
    def _is_local_base(url: str) -> bool:
        """api_base 是不是本机（本地模型才需要和出图抢显存）。"""
        try:
            from urllib.parse import urlsplit

            host = (urlsplit(str(url or "")).hostname or "").lower()
        except Exception:  # noqa: BLE001
            return True
        if not host:
            return True
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        if host.startswith(("10.", "192.168.", "169.254.")):
            return True
        if host.startswith("172."):
            try:
                if 16 <= int(host.split(".")[1]) <= 31:
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _llm_is_local(self, event: AstrMessageEvent) -> bool:
        """当前会话用的模型是不是本机（30 秒缓存；拿不到按本地处理，宁可多卸一次）。"""
        cached = getattr(self, "_llm_local_cache", None)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        local = True
        try:
            provider = await self.context.get_using_provider_async(
                getattr(event, "unified_msg_origin", None)
            )
            cfg = dict(getattr(provider, "provider_config", {}) or {})
            base = str(cfg.get("api_base") or cfg.get("base_url") or "")
            if base:
                local = self._is_local_base(base)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[comfy] 读取 provider 失败（按本地处理）: {exc}")
        self._llm_local_cache = (time.time(), local)
        return local

    @staticmethod
    def _is_group_chat(event: AstrMessageEvent) -> bool:
        try:
            if event.get_group_id():
                return True
        except Exception:  # noqa: BLE001
            pass
        return "GroupMessage" in str(getattr(event, "unified_msg_origin", "") or "")

    async def _nsfw_blocked(
        self, event: AstrMessageEvent, data: bytes, image_prompt: str = ""
    ) -> bool:
        """群聊图片审核：True = 判为违规，拒发。

        标准由用户定：只有明确露出三点才拒；性暗示/情趣内衣/露沟都放行。
        判断不了（模型不能读图 / 调用失败 / 回答含糊）时放行并记日志。
        """
        if not self._conf_bool("comfy_group_guard", True):
            return False
        if not self._is_group_chat(event):
            return False
        if not data:
            return False
        pid = ""
        try:
            guard_id = self._conf_str("comfy_group_guard_provider")
            provider = None
            if guard_id:
                provider = self.context.get_provider_by_id(guard_id)
            if provider is None:
                provider = await self.context.get_using_provider_async(
                    event.unified_msg_origin
                )
            if provider is None:
                logger.warning("[comfy] 群聊审核跳过：拿不到 provider")
                return False

            cfg = dict(getattr(provider, "provider_config", {}) or {})
            mods = [str(x).lower() for x in (cfg.get("modalities") or [])]
            pid = str(cfg.get("id") or "")
            # ★ 能力只看 modalities；某次“判断不了”不能当成“读不了图”，
            #   否则一次含糊回答会让后续 10 分钟直接免审。
            fails = getattr(self, "_vision_probe_fail", None)
            if fails is None:
                fails = self._vision_probe_fail = {}
            can_vision = ("image" in mods) if mods else True
            if not can_vision:
                logger.warning(
                    "[comfy] 群聊审核跳过：这个 provider 读不了图"
                    "（可把 modalities 配上 image，或指定 comfy_group_guard_provider）"
                )
                return False
            last_fail = fails.get(pid) or 0
            if time.time() - last_fail < 600:
                logger.warning(
                    "[comfy] 群聊审核跳过：上次调用视觉模型失败，10 分钟内不再试"
                )
                return False

            user_text = ""
            try:
                user_text = str(event.get_message_str() or "")
            except Exception:  # noqa: BLE001
                pass

            verdict = await moderation.judge(
                provider,
                data,
                user_text=user_text,
                image_prompt=image_prompt,
            )
            if verdict is None:
                # 结论含糊：不动“能力”，也不记失败（下次照常再审）
                logger.warning("[comfy] 审核没给出明确结论（按放行处理）")
                return False
            fails.pop(pid, None)
            # verdict: True=通过 / False=拒绝
            if verdict:
                self._audit_counts["pass"] += 1
            else:
                self._audit_counts["reject"] += 1
            record = {
                "ts": time.time(),
                "verdict": "pass" if verdict else "reject",
                "provider": pid,
                "prompt": (image_prompt or "")[:80],
                "thumb": "",
            }
            if not verdict:  # 只有被拦的才留 96px 小图
                record["thumb"] = self._save_audit_thumb(data)
            self._audit_recent.append(record)
            del self._audit_recent[:-20]
            self._publish_state(force=True)
            return not verdict
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 群聊审核失败（放行）: {exc}")
            if pid:
                fails = getattr(self, "_vision_probe_fail", None)
                if fails is None:
                    fails = self._vision_probe_fail = {}
                fails[pid] = time.time()
            return False

    async def _unload_llm(self) -> None:
        """出图前把 LM Studio 的模型卸掉，腾出显存（失败只警告，不阻断出图）。"""
        if not self._conf_bool("comfy_handoff", True):
            return
        exe = self._lms_exe()
        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                "unload",
                "--all",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=40)
            text = (out or b"").decode("utf-8", errors="replace").strip()
            logger.info(f"[comfy] 已卸载本地模型：{text[-200:] or 'ok'}")
        except FileNotFoundError:
            logger.warning("[comfy] 找不到 lms 命令，跳过卸载本地模型（可能抢显存）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 卸载本地模型失败（继续出图）: {exc}")

    async def _free_comfy(self) -> None:
        """生成结束就释放 ComfyUI 显存，好让 LM Studio 下次自动重载。"""
        if not self._conf_bool("comfy_handoff", True):
            return
        try:
            await self._client().free_models()
            logger.info("[comfy] 已释放 ComfyUI 显存")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 释放显存失败（忽略）: {exc}")

    @asynccontextmanager
    async def _gen_slot(self, event: AstrMessageEvent):
        """生图串行槽：同一块 GPU 一次只画一张，后面来的先告诉用户排第几。

        ★ 对话（LLM 回复）不排队 —— 这条只串行【出图】这一段。
        """
        self._gen_waiting += 1
        try:
            logger.info("[comfy] 等待出图槽…")
            async with self._render_lock:
                logger.info("[comfy] 获得出图槽")
                # ★ 换手/等 LLM 只在本地模型时做：云端 API 不占显卡
                local = await self._llm_is_local(event)
                handoff = local and self._conf_bool("comfy_handoff", True)
                if local:
                    if handoff:
                        await self._unload_llm()
                    await self._wait_llm_idle()
                self._gpu_write(comfy=1, comfy_ts=time.time())
                poller = asyncio.create_task(self._queue_poller())
                try:
                    yield
                finally:
                    poller.cancel()
                    self._queue_pos = None
                    self._gpu_write(comfy=0, comfy_ts=time.time())
                    self._progress = None
                    if self._job:
                        self._last_job = dict(self._job)
                    self._job = {}
                    self._publish_state(force=True)
                    if handoff:
                        # 生成完释放显存，本地模型下次请求会自动重载
                        await self._free_comfy()
        finally:
            self._gen_waiting = max(0, self._gen_waiting - 1)

    # ------------------------------------------------------------------
    #  参考图（图生图）
    # ------------------------------------------------------------------
    def _component_image_bytes(self, comps, depth: int = 0) -> bytes | None:
        """在一组消息段里找第一张图；会钻进被引用消息（Reply.chain）里找。

        ★ 用户「引用一张图 + 说用这张改」时，图在被引用消息里，不在当前消息外层。
          AstrBot 的适配器已经把引用解析成 Reply.chain，往下钻一层就够。
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

    def _reference_ready(
        self, reference: str, event: AstrMessageEvent
    ) -> str | None:
        """异步入队前先检查参考图存不存在，避免先承诺再失败。"""
        ref = str(reference or "").strip().lower()
        if not ref:
            return None
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if ref in ("last", "previous", "上一张", "刚才那张"):
            if self._last_image_bytes.get(umo):
                return None
            entries = self._history_read(umo)
            if entries:
                return None
            return (
                "出图失败：本会话还没有画过图，reference=last 没有参考图。"
                "留空改成文生图，或先画一张。"
            )
        if ref.startswith(("index:", "history:")):
            try:
                idx = max(1, int(ref.split(":", 1)[1]))
            except ValueError:
                return "出图失败：reference 的写法应是 index:3（1=最新）。"
            entries = self._history_read(umo)
            if idx > len(entries):
                return (
                    f"出图失败：本会话只有 {len(entries)} 张历史图，"
                    f"取不到 index:{idx}。先 comfy_gallery 看序号，或留空改成文生图。"
                )
            return None
        if ref.startswith("file:"):
            name = os.path.basename(str(reference).split(":", 1)[1].strip())
            if not name or not (self._session_image_dir(umo) / name).is_file():
                return f"出图失败：本会话历史里没有「{name}」，先 comfy_gallery 查文件名。"
            return None
        return None

    async def _reference_bytes(self, reference: str, event: AstrMessageEvent) -> bytes | None:
        """把 reference 解析成图片字节。

        · ""            → 不用参考图（走文生图）
        · "message"     → 这条 / 被引用消息里的图
        · "last"        → 上一张生成的图（重启后从会话历史里取最新一张）
        · "index:3"     → 本会话历史里第 3 新的图（1=最新）
        · "file:xxx.png"→ 本会话历史里指定文件名的图
        ★ 认不出、或没有图 → 返回 None，由调用方给出明确错误（不要静默退回文生图，
          那会让人以为"改了图"，实际出来一张全新的图）。
        """
        ref = str(reference or "").strip().lower()
        if ref in ("last", "previous", "上一张", "刚才那张"):
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            data = self._last_image_bytes.get(umo)
            if data:
                return data
            # 插件重启/内存清空后，仍能从会话目录拿最新一张
            try:
                entries = self._history_read(umo)
                if entries:
                    path = self._session_image_dir(umo) / entries[0]["file"]
                    if path.is_file():
                        return path.read_bytes()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[comfy] 取历史最新图失败: {exc}")
            return None
        if ref.startswith(("index:", "history:")):
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            try:
                idx = max(1, int(ref.split(":", 1)[1]))
                entries = self._history_read(umo)
                if idx <= len(entries):
                    path = self._session_image_dir(umo) / entries[idx - 1]["file"]
                    if path.is_file():
                        return path.read_bytes()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[comfy] 取历史第 N 张失败: {exc}")
            return None
        if ref.startswith("file:"):
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            name = os.path.basename(str(reference).split(":", 1)[1].strip())
            if name:
                path = self._session_image_dir(umo) / name
                if path.is_file():
                    return path.read_bytes()
            return None
        if ref in ("message", "msg", "image", "图", "这张", "消息图"):
            data = self._component_image_bytes(event.get_messages() or [])
            if data:
                return data
            try:
                for comp in event.get_messages() or []:
                    if isinstance(comp, Comp.Image):
                        getter = getattr(comp, "convert_to_file_path", None)
                        if callable(getter):
                            p = await getter()
                            if p and Path(str(p)).is_file():
                                return Path(str(p)).read_bytes()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[comfy] 异步取图失败: {exc}")
        return None

    # ------------------------------------------------------------------
    #  预设 / 会话提示
    # ------------------------------------------------------------------
    def _preset(self, want: str = "") -> str:
        name = (want or "").strip().lower() or self._conf_str(
            "comfy_preset", presets.DEFAULT_PRESET
        )
        return name if name in presets.PRESETS else presets.DEFAULT_PRESET

    async def _notify(self, event: AstrMessageEvent, text: str) -> None:
        """往会话发一条纯文本提示（失败不影响出图）。"""
        if not self._conf_bool("comfy_notify", True):
            return
        try:
            await self.context.send_message(
                event.unified_msg_origin, MessageChain(chain=[Comp.Plain(text)])
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 发提示失败（忽略）: {exc}")

    async def _notify_loading(
        self, event: AstrMessageEvent, preset: str, model: str
    ) -> None:
        """换工作流 / 换底模 / 冷启动时，先把"要等"说清楚。"""
        client = self._client()
        cold = await asyncio.to_thread(client.healthy_sync) is None
        parts = []
        if cold:
            parts.append("⏳ 正在启动 ComfyUI（首次约 20~50 秒）…")
        if self._last_preset is not None and self._last_preset != preset:
            meta = presets.PRESETS[preset]
            parts.append(f"🔁 切换工作流：{meta['label']}（约 {meta['eta']} 秒一张）")
        if self._last_model is not None and model and self._last_model != model:
            parts.append(f"🔄 切换底模：{os.path.basename(model)}（第一次加载会慢一点）")
        if parts:
            # ★ 只在「确实要加载权重」时发这一句；别的进度一律不推。
            await self._notify(event, "⏳ 正在加载模型权重…出图后直接把图发你。")

    # ------------------------------------------------------------------
    #  云端 API：异步出图（回复不等信息画完）
    # ------------------------------------------------------------------
    async def _async_ok(self, event: AstrMessageEvent) -> bool:
        """只有云端 API 才异步：本地模型和 ComfyUI 抢显存，必须同步排队。"""
        if not self._conf_bool("comfy_async_api", True):
            return False
        try:
            return not await self._llm_is_local(event)
        except Exception:  # noqa: BLE001
            return False

    def _spawn_image_job(self, event: AstrMessageEvent, kwargs: dict) -> None:
        task = asyncio.create_task(self._image_job(event, kwargs))
        self._bg_tasks.add(task)
        self._bg_umo[task] = str(getattr(event, "unified_msg_origin", "") or "")
        task.add_done_callback(self._on_bg_done)
        logger.info(
            f"[comfy] 后台任务已创建 preset={kwargs.get('preset') or '默认'} "
            f"reference={kwargs.get('reference') or '-'}"
        )

    def _on_bg_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        umo = self._bg_umo.pop(task, "")
        if task.cancelled():
            logger.warning(f"[comfy] 后台任务被取消 → {umo}")
            return
        exc = task.exception()
        if exc:
            logger.error(f"[comfy] 后台出图任务异常: {exc}", exc_info=exc)

    async def _image_job(self, event: AstrMessageEvent, kwargs: dict) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        token = _ASYNC_RUN.set(True)
        logger.info(f"[comfy] 后台任务开始 → {umo}")
        try:
            result = await self.comfy_generate(event, **kwargs)
        except asyncio.CancelledError:
            logger.warning(f"[comfy] 后台任务在生成前被取消 → {umo}")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[comfy] 异步出图异常: {exc}", exc_info=True)
            await self._send_chat(umo, f"图画失败了：{type(exc).__name__}: {exc}")
            return
        finally:
            _ASYNC_RUN.reset(token)
        if isinstance(result, str):
            await self._handle_async_result(event, umo, result)
        else:
            logger.info(f"[comfy] 后台任务完成，图已自动发出 → {umo}")

    def _queue_audit_nudge(self, event: AstrMessageEvent, umo: str) -> bool:
        """审核失败时合成一条系统消息，走完整 pipeline 拉起 bot 回复。"""
        now = time.time()
        if now - self._audit_nudge_at.get(umo, 0) < 600:
            return False
        try:
            session = MessageSession.from_str(umo)
            platform = self.context.get_platform_inst(session.platform_name)
            if platform is None:
                return False
            msg = AstrBotMessage()
            msg.type = session.message_type
            msg.self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
            msg.session_id = session.session_id
            msg.message_id = uuid.uuid4().hex
            msg.sender = MessageMember(user_id="0", nickname="系统")
            text = (
                "[系统通知] 刚才那张图在群聊审核里没通过，没有发出去。"
                "请回复用户：说明没发，并问他是要调整描述重画还是取消。"
                "不要自动重画，也不要重复发图。"
            )
            msg.message = [Comp.Plain(text)]
            msg.message_str = text
            msg.raw_message = None
            msg.timestamp = int(now)
            if session.message_type == MessageType.GROUP_MESSAGE:
                group = Group(session.session_id)
                original = getattr(getattr(event, "message_obj", None), "group", None)
                group.group_name = getattr(original, "group_name", None)
                msg.group = group
            new_event = platform.create_event(msg)
            new_event.is_wake = True
            new_event.is_at_or_wake_command = True
            platform.commit_event(new_event)
            self._audit_nudge_at[umo] = now
            logger.info(f"[comfy] 审核失败已合成系统消息，拉起 bot 回复 → {umo}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 审核失败拉起回复失败，退回文字提示: {exc}")
            return False

    async def _handle_async_result(
        self, event: AstrMessageEvent, umo: str, result: str
    ) -> None:
        first = result.split("\n", 1)[0].strip()
        if "被判为 NSFW" in result:
            # 直接合成一轮，让模型自己组织“没过审，要改还是算了”
            if self._conf_bool("comfy_audit_nudge", True) and self._queue_audit_nudge(
                event, umo
            ):
                self._pending_notes.pop(umo, None)
                return
            await self._send_chat(
                umo,
                "这张图没过审，没发。要整改重画就调整描述再来；不想画了就取消。",
            )
            self._pending_notes[umo] = (
                time.time(),
                "上一张图在群聊审核里没通过，没有发出去。"
                "用户若要重画，先调整描述；不想画就取消。",
            )
            logger.info("[comfy] 异步出图被审核拦下，已提示会话")
            return
        if "这张图先不发了" in result:
            await self._send_chat(umo, first)
            return
        if result.startswith("出图失败"):
            await self._send_chat(umo, first)
            return
        # 画幅提醒、参数校验之类的文字结果：也得让人看见，不能静默
        await self._send_chat(umo, first or "图画失败了，稍后再试。")

    async def _send_chat(self, umo: str, text: str) -> None:
        if not umo or not text:
            return
        try:
            await self.context.send_message(
                umo, MessageChain(chain=[Comp.Plain(str(text))])
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 后台提示发送失败: {exc}")

    @staticmethod
    def _image_data_url(path: Path) -> str:
        """把会话图压到 1024px，供写进历史时控制视觉 token。"""
        try:
            import io

            from PIL import Image

            img = Image.open(path)
            img = img.convert("RGB")
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 历史图转 data URL 失败: {exc}")
            return ""

    async def _append_image_history(self, umo: str, path: Path) -> None:
        """把已发出的异步图作为 user 角色的系统记录写进当前会话历史。

        ★ DeepSeek 只接受 user 消息里的 image_url；assistant 带图会 400。
        ★ 用 [系统记录] 前缀说明这是 bot 自己刚画好的图。
        """
        if not self._conf_bool("comfy_self_view", True):
            return
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                logger.info("[comfy] 异步图未写入历史：当前没有会话 id")
                return
            url = await asyncio.to_thread(self._image_data_url, Path(path))
            if not url:
                return
            async with (getattr(self, "_conv_lock", None) or asyncio.Lock()):
                conv = await cm.get_conversation(umo, cid)
                if conv is None:
                    logger.warning("[comfy] 异步图未写入历史：拿不到当前会话")
                    return
                raw = getattr(conv, "history", None)
                if isinstance(raw, list):
                    history = list(raw)
                elif isinstance(raw, str) and raw.strip():
                    try:
                        import json as _json

                        history = list(_json.loads(raw))
                    except Exception as exc:  # noqa: BLE001
                        # ★ 解析失败绝不能写空历史，否则会把旧对话冲掉
                        logger.warning(f"[comfy] 会话历史解析失败，放弃写入: {exc}")
                        return
                elif raw in (None, ""):
                    history = []
                else:
                    logger.warning("[comfy] 会话历史格式未知，放弃写入")
                    return
                history.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "[系统记录] 这是你刚才画好并已发到会话里的图"
                                    f"（{Path(path).name}），供你查看。"
                                ),
                            },
                            {"type": "image_url", "image_url": {"url": url}},
                        ],
                    }
                )
                await cm.update_conversation(
                    umo, conversation_id=cid, history=history
                )
            logger.info(f"[comfy] 异步图已写入会话历史 → {umo}（{Path(path).name}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 异步图写入历史失败（图已发出）: {exc}")

    @filter.on_llm_request()
    async def _drain_pending_note(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """审核失败后，下一轮让模型知道“刚才那张没发”，方便整改或取消。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        item = self._pending_notes.pop(umo, None)
        if not item:
            return
        ts, note = item
        if time.time() - ts > 900:
            return
        base = (req.system_prompt or "").rstrip()
        req.system_prompt = (base + "\n\n[系统提醒] " + note).strip()

    # ------------------------------------------------------------------
    #  工具入口
    # ------------------------------------------------------------------
    def _gate(self, event: AstrMessageEvent, tool: str) -> str | None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo in self._revealed:
            return None
        logger.info(f"[comfy] {tool} 被直接调用（未先调 comfy_tools），已返回指正")
        return GATE_ERROR.format(tool=tool)

    # ------------------------------------------------------------------
    #  提示词法典（本地只读检索）
    # ------------------------------------------------------------------
    def _codex_root(self) -> Path:
        custom = self._conf_str("comfy_codex_dir")
        if custom:
            return Path(custom)
        return (
            Path(PLUGIN_DIR).parents[2]
            / "data"
            / "plugin_data"
            / "ncd_comfy"
            / "codex"
        )

    def _ensure_codex(self) -> dict[str, Path]:
        """确保两份法典可用：优先配置目录，缺失时从旧绘图插件复制一次。"""
        root = self._codex_root()
        root.mkdir(parents=True, exist_ok=True)
        files = {"sfw": root / "sfw.txt", "nsfw": root / "nsfw.txt"}
        legacy = (
            Path(PLUGIN_DIR).parents[1]
            / "plugins_disabled"
            / "astrbot_plugin_ncd_draw"
            / "kb"
            / "docs"
        )
        srcs = {
            "sfw": legacy / "sfw" / "提示词常规法典.txt",
            "nsfw": legacy / "nsfw" / "提示词色色法典.txt",
        }
        for cat, dst in files.items():
            if dst.exists() and dst.stat().st_size > 0:
                continue
            src = srcs.get(cat)
            if src and src.is_file():
                try:
                    dst.write_bytes(src.read_bytes())
                    logger.info(f"[comfy] 法典已复制：{cat} → {dst}")
                except OSError as exc:
                    logger.warning(f"[comfy] 法典复制失败 {cat}: {exc}")
        return files

    def _codex_lines(self, cat: str) -> list[str]:
        cache = getattr(self, "_codex_cache", None)
        if cache is None:
            cache = self._codex_cache = {}
        if cat in cache:
            return cache[cat]
        path = self._ensure_codex().get(cat)
        lines: list[str] = []
        if path and path.is_file():
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError as exc:
                logger.warning(f"[comfy] 读法典失败 {cat}: {exc}")
        cache[cat] = lines
        return lines

    @filter.llm_tool(name="comfy_tools")
    async def comfy_tools(self, event: AstrMessageEvent) -> str:
        """出图入口（必须先调）：四个工作流、各自默认参数、当前可选底模。

        ★ comfy_generate / comfy_models 都要先调一次本工具；没调会返回错误。
        ★ 每个会话调一次即可，第二次起只回一句提醒（省上下文）。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        already = umo in self._revealed
        self._revealed[umo] = time.time()
        if len(self._revealed) > 200:
            keep = sorted(self._revealed.items(), key=lambda kv: kv[1])[-100:]
            self._revealed = dict(keep)
        logger.info(f"[comfy] 出图入口被调用：{umo or '（未知会话）'}")
        if already:
            return (
                "入口已经开过了，别重复调 comfy_tools。\n"
                "直接调 comfy_generate（参数见上一轮说明）即可。"
            )

        lines = ["ComfyUI 出图入口已打开。四个工作流（用 preset 参数选，留空=默认）："]
        for name, meta in presets.PRESETS.items():
            try:
                info = presets.summarize(presets.load_graph(name))
            except presets.WorkflowError as exc:
                lines.append(f"· {name}（{meta['label']}）⚠ 工作流有问题：{exc}")
                continue
            lines.append(
                f"· {name} —— {meta['label']}，约 {meta['eta']} 秒/张"
                f"（{info['width']}×{info['height']}，{info['steps']} 步，CFG {info['cfg']:g}，"
                f"{info['sampler_name']}/{info['scheduler']}）\n"
                f"  底模：{info['model']}"
            )
        default = self._preset()
        lines += [
            "",
            f"默认工作流：{default}（{presets.PRESETS[default]['label']}）",
            "",
            "★ 写 prompt 前先调 comfy_prompt_help(query, category=sfw|nsfw|all) "
            "—— 查本地法典，会返回匹配 tag + 一条示范成品提示词。",
            "另外：comfy_say(text) 可以用克隆音色说一句话、发成语音（约 6 秒一条）。",
            "云端 API：comfy_generate 会立即返回，图在后台画、画完直接发；不要等，也不要重复调。",
            "翻旧图：comfy_gallery(query) 看列表，comfy_output(index/file) 直接发；本会话保留 "
            f"{self._history_days()} 天 / {self._history_limit()} 张，不会串到别的会话。",
            "超时兜底：comfy_output() 可以把已经画好的图直接发出来，不用重画。",
            "默认风格：sdxl 预设默认底模 waiIllustriousSDXL_v170，不自动叠 LoRA；要加风格用 lora 参数。",
            "推荐组合：sdxl + waiIllustriousSDXL_v170（无 LoRA，默认即此）。非用户特别要求，别手动指定 model / lora。",
            "",
            "comfy_generate 参数（都能留空 = 用工作流里的默认值）：",
            "· prompt      英文标签式描述（10~15 个逗号分隔的短标签；中文会出废图）",
            "· negative    负面词；留空 = 工作流里那个（通常是空的）",
            "· preset      sdxl / krea2 / anima / moss",
            "· size        画幅，如 720x960，或 竖/横/方",
            "· width/height 像素直接给（8 的倍数，覆盖 size）",
            "· steps       1~150；cfg 0~30；seed 整数（-1 = 随机）",
            "· model       换底模（写文件名或其中一段，大小写不敏感）",
            '· lora        加 LoRA，如 "cunnyfunky:0.8"；多个用逗号隔开',
            "· reference   图生图：message=这条/被引用消息里的图；last=上一张；"
            "index:3=本会话历史第 3 新；file:xxx.png=指定历史文件；留空=文生图",
            '· denoise     图生图重绘强度 0.05~1（越大越像重画）；0 = 用配置默认',
            "",
            "prompt 硬规则：① 只写英文；② 逗号分隔短标签，别写长句；",
            "③ 开头写 masterpiece, best quality, highres；④ 别堆 8k / ultra detailed。",
            "",
            "★ 第一次出图、换工作流、换底模都会先往会话发一条加载提示，",
            "  调用后会有「加载权重 / 开始采样」两条进度提示，不用重复调用。",
            f"★ 空闲 {self._conf_int('comfy_unload_timeout', 300) // 60} 分钟后 ComfyUI 会卸掉模型，",
            f"  {self._conf_int('comfy_idle_timeout', 1800) // 60} 分钟后关掉进程。",
        ]
        models = self._scan(default)
        if models:
            lines += [
                "",
                f"{presets.PRESETS[default]['label']} 可用底模（共 {len(models)} 个）：",
            ]
            for m in models[:20]:
                lines.append(f"· {m['name']}（{m['size_mb'] / 1024:.2f} GB）")
            if len(models) > 20:
                lines.append(f"……还有 {len(models) - 20} 个，用 comfy_models 看全")
        else:
            lines += [
                "",
                "⚠ 没扫到底模：检查 comfy_extra_paths / comfy_dir 指向的 extra_model_paths.yaml",
            ]
        return "\n".join(lines)

    @filter.llm_tool(name="comfy_generate")
    async def comfy_generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        negative: str = "",
        preset: str = "",
        size: str = "",
        width: int = 0,
        height: int = 0,
        steps: int = 0,
        cfg: float = 0.0,
        seed: int = -1,
        model: str = "",
        lora: str = "",
        reference: str = "",
        denoise: float = 0.0,
        force: bool = False,
    ) -> str:
        """按预设工作流画一张图并发到当前会话。★ 必须先调用 comfy_tools。

        ★ 云端 API 下本工具会立即返回：图在后台画，画完直接发到会话。
          所以这一轮不要等、不要再调一次，也不要说“图已经画好了/我看到了”。

        Args:
            prompt(string): 英文【标签式】描述（逗号分隔短标签，10~15 个，
                开头写 masterpiece, best quality, highres）。中文会出废图。
            negative(string): 负面提示词；留空 = 工作流里那个（通常为空）。
            preset(string): 工作流：sdxl（约25s）/ krea2（约30s）/ anima（约40s）/
                moss（约60s）；留空 = 配置里的默认。
            size(string): 画幅，如 "720x960"，或简写 竖/横/方；留空 = 工作流默认；
                图生图时留空 = 跟参考图一样大（按比例缩到上限内）。
            width(int): 宽（8 的倍数）；和 height 一起覆盖 size。
            height(int): 高；单独给一个时另一半用默认值。
            steps(int): 采样步数 1~150；0 = 工作流默认。
            cfg(float): CFG 0~30；0 = 工作流默认。
            seed(int): 随机种子；-1 = 每次随机。
            model(string): 换底模（写文件名或其中一段）；留空 = 工作流里那个。
            lora(string): 加 LoRA，如 "cunnyfunky:0.8"；多个用逗号分隔；留空 = 不加。
            reference(string): 图生图的参考图：留空 = 文生图；"message" = 这条或
                被引用消息里的图；"last" = 上一张生成的图。
            denoise(float): 图生图重绘强度 0.05~1（越大越像重画）；0 = 用配置默认。
            force(bool): 画幅超过提醒线、且用户已同意时才填 True。
        """
        gate = self._gate(event, "comfy_generate")
        if gate:
            return gate

        # ★ 云端 API：把整张图丢到后台，先把聊天放出去。
        #   本地模型必须同步 —— 它和 ComfyUI 抢显存。
        #   带“消息里的图”做参考时也走同步：临时图可能在事件结束后被清掉。
        ref_raw = str(reference or "").strip().lower()
        message_ref = ref_raw in ("message", "msg", "image", "图", "这张", "消息图")
        if not _ASYNC_RUN.get() and not message_ref and await self._async_ok(event):
            early = str(prompt or "").strip()
            if not early:
                return "出图失败：prompt 是空的。写一段英文标签式描述再调。"
            want_early = (preset or "").strip().lower()
            if want_early and want_early not in presets.PRESETS:
                return (
                    f"出图失败：没有工作流「{preset}」。可选："
                    + "、".join(presets.PRESETS)
                )
            ref_err = self._reference_ready(reference, event)
            if ref_err:
                return ref_err
            busy_note = await self._busy_note_for_current()
            self._spawn_image_job(
                event,
                {
                    "prompt": prompt,
                    "negative": negative,
                    "preset": preset,
                    "size": size,
                    "width": width,
                    "height": height,
                    "steps": steps,
                    "cfg": cfg,
                    "seed": seed,
                    "model": model,
                    "lora": lora,
                    "reference": reference,
                    "denoise": denoise,
                    "force": force,
                },
            )
            logger.info(
                f"[comfy] 云端异步出图已入队 → "
                f"{str(getattr(event, 'unified_msg_origin', '') or '')}"
            )
            queue_note = self._queue_note()
            return (
                "已经交给后台画了，画好会直接把图发到会话里。\n"
                "★ 这一轮不用等，也不要再调 comfy_generate，"
                "更不要说自己已经看到图。\n"
                "可以先用一句“在画了”告诉用户。"
                + (
                    "\n\n" + "\n".join(n for n in (queue_note, busy_note) if n)
                    if (queue_note or busy_note)
                    else ""
                )
            )

        text = str(prompt or "").strip()
        if not text:
            return "出图失败：prompt 是空的。写一段英文标签式描述再调。"

        want_preset = (preset or "").strip().lower()
        if want_preset and want_preset not in presets.PRESETS:
            return f"出图失败：没有工作流「{preset}」。可选：" + "、".join(presets.PRESETS)
        preset_name = self._preset(want_preset)

        try:
            graph = presets.load_graph(preset_name)
            info = presets.summarize(graph)
        except presets.WorkflowError as exc:
            return f"出图失败：{exc}"

        max_side = self._conf_int("comfy_max_side", 1920)
        max_pixels = int(self._conf_float("comfy_max_mp", 2.7648) * 1e6)

        # ---- 图生图：先把参考图拿下来 ----
        ref_bytes: bytes | None = None
        if (reference or "").strip():
            ref_bytes = await self._reference_bytes(reference, event)
            if not ref_bytes:
                return (
                    "出图失败：reference 要的是图，但没找到。\n"
                    "把图直接发在消息里（或引用一张图）再试；"
                    "不想用参考图就把 reference 留空。"
                )

        try:
            if ref_bytes and not (size or width or height):
                # 没指定画幅 → 跟参考图一样大（等比缩到上限内）
                wh = sizing.image_size(ref_bytes)
                if wh:
                    fw, fh = sizing.fit_within(wh[0], wh[1], max_side, max_pixels)
                    size = f"{fw}x{fh}"
            w, h = sizing.resolve_size(
                size, width, height, (info["width"], info["height"]), max_side, max_pixels
            )
        except ValueError as exc:
            return f"出图失败：{exc}"

        # 提醒线：单边超过上限的 3/4 就先问一句（旧插件也是这么做的）
        warn = sizing.warn_side(max_side)
        if not force and max(w, h) > warn:
            return (
                f"画幅 {w}×{h} 超过提醒线（单边 {warn}）：\n"
                "12 GB 卡上这个尺寸会明显更慢，也可能因为显存不够失败。\n"
                f"用户确认就要这个尺寸的话，带 force=true 再调一次；"
                f"否则改成 {warn} 以内的尺寸（比如 720x960 / 1024x1024）。"
            )

        steps_val = int(steps or 0) or int(
            self._conf_int("comfy_steps", 0) or info["steps"]
        )
        steps_val = max(1, min(150, steps_val))
        cfg_val = float(cfg or 0) or float(self._conf_float("comfy_cfg", 0) or info["cfg"])
        cfg_val = max(0.0, min(30.0, cfg_val))
        seed_val = sizing.normalize_seed(seed)
        denoise_val = float(denoise or 0) or self._conf_float("comfy_img2img_strength", 0.6)
        denoise_val = max(0.05, min(1.0, denoise_val))

        # 底模（只在当前预设的家族目录里找；跨族直接拒绝并给推荐）
        model_name = ""
        if (model or "").strip():
            try:
                model_name = self._resolve_name(model, self._scan(preset_name), "底模")
            except ComfyError:
                meta = presets.PRESETS[preset_name]
                return (
                    f"出图失败：{meta['label']} 只能用 {meta.get('model_dir')} 目录里的底模，"
                    f"你给的「{model}」不属于这一族。\n"
                    f"★ 不想选错就用默认预设：{preset_name}（不填 model 即可）。"
                    + RECOMMENDED_HINT
                )

        # LoRA（提交前动态插节点）
        loras: list[dict] = []
        if (lora or "").strip():
            default_strength = self._conf_float("comfy_lora_strength", 1.0)
            pool = self._scan_loras(preset_name)
            if not pool:
                return "出图失败：这个预设的 LoRA 目录里没有文件（检查 comfy_extra_paths）"
            try:
                for spec in re.split(r"[,，]", lora):
                    spec = spec.strip()
                    if not spec:
                        continue
                    name_part, _, strength_part = spec.partition(":")
                    try:
                        resolved = self._resolve_name(name_part, pool, "LoRA")
                    except ComfyError:
                        # 在别的族里能找着 → 明确告诉模型「这是跨族」
                        other = ""
                        try:
                            other = self._resolve_name(name_part, self._scan_loras(), "LoRA")
                        except ComfyError:
                            pass
                        if other:
                            raise ComfyError(
                                f"LoRA「{os.path.basename(other)}」属于别的模型家族，"
                                f"不能和 {presets.PRESETS[preset_name]['label']} 一起用。"
                                f"这一族可用：{self._lora_names(pool)}"
                                + RECOMMENDED_HINT
                            ) from None
                        raise
                    strength = (
                        float(strength_part) if strength_part.strip() else default_strength
                    )
                    loras.append(
                        {"name": resolved, "strength": max(0.0, min(2.0, strength))}
                    )
            except (ComfyError, ValueError) as exc:
                return f"出图失败：{exc}"

        # 默认 LoRA：只有 sdxl 预设、且用户没指定 lora 时，自动叠一个风格 LoRA
        if not loras and preset_name == "sdxl":
            auto_lora = self._conf_str("comfy_default_lora")
            if auto_lora:
                try:
                    auto_name = self._resolve_name(
                        auto_lora, self._scan_loras(preset_name), "LoRA"
                    )
                    loras.append(
                        {
                            "name": auto_name,
                            "strength": self._conf_float(
                                "comfy_default_lora_strength", 0.8
                            ),
                        }
                    )
                except ComfyError as exc:
                    logger.warning(
                        f"[comfy] 默认 LoRA 在本族里没找到，已跳过（不会跨族硬套）: {exc}"
                    )

        used_model = model_name or info["model"]
        await self._notify_loading(event, preset_name, used_model)

        client = self._client()
        async with self._gen_slot(event):
            try:
                if not await asyncio.to_thread(client.healthy_sync):
                    await client.ensure_server()
                # ★ 图生图：先把参考图传进 ComfyUI 的 input 目录
                ref_name = ""
                if ref_bytes:
                    ref_name = await client.upload_image(
                        ref_bytes, f"ref_{uuid.uuid4().hex[:8]}.png"
                    )
                final = presets.apply_params(
                    graph,
                    prompt=text,
                    negative=(negative or None),
                    width=w,
                    height=h,
                    steps=steps_val,
                    cfg=cfg_val,
                    seed=seed_val,
                    model=model_name or None,
                    loras=loras or None,
                    filename_prefix=self._conf_str("comfy_filename_prefix") or None,
                    source_image=ref_name or None,
                    denoise=(denoise_val if ref_name else None),
                )
                async def _on_sampler() -> None:
                    await self._notify(event, "🖌️ 开始采样了…")

                self._job = {
                    "preset": preset_name,
                    "model": os.path.basename(used_model),
                }
                self._progress = {"overall": 0.0, "phase": "queue", "label": "排队"}
                self._publish_state(force=True)
                result = await client.generate(
                    final,
                    on_sampler_start=_on_sampler,
                    on_progress=self._on_progress,
                )
                image = result["images"][0]
                data = await client.fetch_image(image)
            except ComfyError as exc:
                logger.warning(f"[comfy] 出图失败: {exc}")
                return f"出图失败：{exc}"
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[comfy] 出图异常: {exc}", exc_info=True)
                return f"出图失败：{type(exc).__name__}: {exc}"

        if await self._nsfw_blocked(event, data, image_prompt=text):
            logger.info("[comfy] 群聊图片被判 NSFW，已拒绝发送")
            self._remember_recent(
                {
                    "name": str(image.get("filename") or "image.png"),
                    "preset": preset_name,
                    "model": os.path.basename(used_model),
                    "seconds": result.get("seconds"),
                    "size": len(data),
                    "audit": "reject",
                    "ts": time.time(),
                    "prompt": text[:120],
                }
            )
            return (
                "这张图被判为 NSFW，已经拦下没有发到群里。\n"
                "★ 换个更收敛的画面描述重画；如果其实是误判，可以私聊发或调高判断阈值。"
            )

        umo_ev = str(getattr(event, "unified_msg_origin", "") or "")
        ok, wait = self._image_allowed(umo_ev, event)
        if not ok:
            return (
                f"这张图先不发了：这个会话 {wait} 秒内刚发过图。"
                "提醒用户稍等再看，不要连续刷图。"
            )
        await self._pace_send()
        self._last_image_at[umo_ev] = time.time()
        filename = str(image.get("filename") or "image.png")
        path = self._save_session_image(
            umo_ev,
            data,
            filename,
            {
                "prompt": text,
                "preset": preset_name,
                "model": os.path.basename(used_model),
            },
        )
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Image.fromFileSystem(str(path))]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[comfy] 发送生成的图失败: {exc}", exc_info=True)
            return f"图画好了但发送失败：{type(exc).__name__}: {exc}"

        if _ASYNC_RUN.get():
            await self._append_image_history(umo_ev, path)

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        self._last_output[umo] = str(path)
        self._remember_recent(
            {
                "name": os.path.basename(str(path)),
                "path": str(path),
                "preset": preset_name,
                "model": os.path.basename(used_model),
                "seconds": result.get("seconds"),
                "size": len(data),
                "audit": "pass" if self._is_group_chat(event) else "skip",
                "ts": time.time(),
                "prompt": text[:120],
            }
        )
        print_id = self._bytes_print(data)
        bucket = self._sent_files.setdefault(umo, [])
        if print_id:
            bucket.append(print_id)
            del bucket[:-50]
            marks = self._sent_at.setdefault(umo, {})
            marks[print_id] = time.time()
            for old in sorted(marks, key=marks.get)[:-80]:
                marks.pop(old, None)

        self._last_preset = preset_name
        self._last_model = used_model
        # 下一张要 reference="last" 的话就用它
        self._last_image_bytes[umo] = data
        # 只留最近 20 个会话，防内存涨
        while len(self._last_image_bytes) > 20:
            self._last_image_bytes.pop(next(iter(self._last_image_bytes)), None)
        logger.info(
            f"[comfy] 已发送 {path.name}（{len(data):,} 字节，{preset_name}"
            f"{'，图生图' if ref_name else ''}）→ {event.unified_msg_origin}"
        )
        used = [
            f"工作流 {presets.PRESETS[preset_name]['label']}",
            f"底模 {os.path.basename(used_model)}",
            f"{w}×{h}",
            f"{steps_val} 步",
            f"CFG {cfg_val:g}",
            f"seed {seed_val}",
        ]
        if loras:
            used.append(
                "LoRA "
                + "、".join(f"{os.path.basename(l['name'])}:{l['strength']:g}" for l in loras)
            )
        if ref_name:
            used.append(f"图生图（参考图 denoise {denoise_val:g}）")
        queue_note = self._queue_note()
        busy_note = await self._busy_note_for_current()
        notes = "\n".join(n for n in (queue_note, busy_note) if n)
        return self._tool_image_result(
            f"图已经画好并发到会话里了（{filename}，{len(data):,} 字节，"
            f"ComfyUI 用时 {result['seconds']} 秒）。\n"
            f"参数：{' · '.join(used)}\n"
            + (f"{notes}\n" if notes else "")
            + "★ 这张图已经附在本次结果里，你可以直接看。先自查一遍"
            "（脸、发色、服装、动作是否和用户要求一致），不对就调整描述重画；"
            "不要重复调用本工具，也不要声称看过图却描述不出细节。",
            data,
        )

    @filter.llm_tool(name="comfy_prompt_help")
    async def comfy_prompt_help(
        self,
        event: AstrMessageEvent,
        query: str,
        category: str = "sfw",
        limit: int = 5,
    ) -> str:
        """【写画图 prompt 前先调用】在本地提示词法典里按关键词查写法（只读）。

        法典是长篇 tag 语料。返回匹配条目 + 一条可直接用的 suggested_prompt。
        想要角色/服装/光影/画风 tags，先查它，别凭印象编标签。
        例：comfy_prompt_help("silver hair, school uniform")

        Args:
            query(string): 关键词，多个用空格或逗号分隔（支持中英文）。
            category(string): sfw（默认，常规）/ nsfw（涩涩）/ all（两个都查）。
            limit(number): 返回几条，1~10，默认 5。
        """
        if not self._conf_bool("comfy_codex_enabled", True):
            return json.dumps({"error": "法典检索已在配置里关闭"}, ensure_ascii=False)
        q = str(query or "").strip()
        if not q:
            return json.dumps(
                {"error": "给个关键词，比如：银发 红瞳 / school uniform / cinematic lighting"},
                ensure_ascii=False,
            )
        cat = str(category or "sfw").strip().lower()
        if cat not in ("sfw", "nsfw", "all"):
            cat = "sfw"
        cats = ["sfw", "nsfw"] if cat == "all" else [cat]
        try:
            n = max(1, min(10, int(limit or 5)))
        except (TypeError, ValueError):
            n = 5
        terms = [x for x in re.split(r"[\s,，;；]+", q.lower()) if x]
        if not terms:
            terms = [q.lower()]

        scored: list[tuple[int, str, int, str]] = []
        for c in cats:
            lines = self._codex_lines(c)
            for i, line in enumerate(lines):
                low = line.lower()
                if not low.strip():
                    continue
                hits = sum(1 for term in terms if term in low)
                if not hits:
                    continue
                score = hits * 10 + (5 if q.lower() in low else 0) - min(len(line) // 200, 3)
                scored.append((score, c, i, line))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return json.dumps(
                {
                    "query": q,
                    "category": cat,
                    "matches": [],
                    "note": "法典里没查到这些关键词，换个说法或换 category=all。",
                },
                ensure_ascii=False,
            )

        max_chars = self._conf_int("comfy_codex_max_chars", 1800)
        out: list[dict] = []
        used = 0
        for _score, c, i, line in scored:
            text = line.strip()[:400]
            entry = {"cat": c, "line": i + 1, "text": text}
            cost = len(text) + 40
            if out and used + cost > max_chars:
                break
            out.append(entry)
            used += cost
            if len(out) >= n:
                break
        # 从匹配条目里抽英文 tag，拼一条示范成品提示词
        tags: list[str] = []
        seen_low: set[str] = set()
        for entry in out:
            for part in re.split(r"[,，]", entry["text"]):
                tag = part.strip().strip("【】{}（）()[]")
                if not tag or len(tag) > 40:
                    continue
                if not re.fullmatch(r"[A-Za-z0-9 _\-:.']+", tag):
                    continue
                low = tag.lower()
                if low in seen_low:
                    continue
                seen_low.add(low)
                tags.append(tag)
                if len(tags) >= 12:
                    break
            if len(tags) >= 12:
                break
        suggested = ""
        if tags:
            suggested = "masterpiece, best quality, " + ", ".join(tags)

        return json.dumps(
            {
                "query": q,
                "category": cat,
                "matches": out,
                "suggested_prompt": suggested,
                "note": "suggested_prompt 是照着匹配条目拼的示范成品，可直接改；不够就换关键词再查。",
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="comfy_models")
    async def comfy_models(
        self, event: AstrMessageEvent, kind: str = "model", preset: str = ""
    ) -> str:
        """列出可用的底模或 LoRA。

        Args:
            kind(string): "model" = 底模（默认）；"lora" = LoRA 清单。
            preset(string): 查哪个工作流的底模（sdxl/krea2/anima/moss）；留空=默认。
        """
        gate = self._gate(event, "comfy_models")
        if gate:
            return gate
        want = (kind or "model").strip().lower()
        if want in ("lora", "loras", "lo"):
            items = self._scan_loras()
            if not items:
                return "LoRA 目录里没有文件。检查 comfy_extra_paths 指向的 loras 目录。"
            lines = [
                f"可用 LoRA（{len(items)} 个，comfy_generate 的 lora 参数填文件名即可）："
            ]
            for m in items[:60]:
                lines.append(f"· {m['name']}（{m['size_mb']:.0f} MB）")
            if len(items) > 60:
                lines.append(f"……还有 {len(items) - 60} 个")
            return "\n".join(lines)

        preset_name = self._preset(preset)
        items = self._scan(preset_name)
        label = presets.PRESETS[preset_name]["label"]
        if not items:
            return (
                f"{label} 没有扫到底模。检查 extra_model_paths.yaml 里的 "
                f"{'checkpoints' if preset_name == 'sdxl' else 'diffusion_models'} 目录。"
            )
        current = ""
        try:
            current = presets.summarize(presets.load_graph(preset_name))["model"]
        except presets.WorkflowError:
            pass
        lines = [f"{label} 可用底模（{len(items)} 个）："]
        for m in items[:60]:
            mark = " ← 当前" if m["name"] == current else ""
            lines.append(f"· {m['name']}（{m['size_mb'] / 1024:.2f} GB）{mark}")
        if len(items) > 60:
            lines.append(f"……还有 {len(items) - 60} 个")
        return "\n".join(lines)

    @filter.llm_tool(name="comfy_output")
    async def comfy_output(
        self,
        event: AstrMessageEvent,
        index: int = 1,
        hours: int = 0,
        file: str = "",
        force: bool = False,
    ) -> str:
        """把本会话已经画好的图直接发到会话里（不用重画）。

        ★ 用途：翻旧图、重发上个月那张、异步出图后想再取一次。
        ★ 只能取本会话自己的历史，不会串到别的群/私聊。
        ★ 已经发过的图默认跳过；用户明确说“再发一遍”才用 force=True。

        Args:
            index(int): 1 = 最新一张，2 = 倒数第二张……默认 1。
            hours(int): 只找最近几小时内生成的；0（默认）= 全部保留期。
            file(string): 精确文件名（先用 comfy_gallery 查）；填了就忽略 index。
            force(bool): True = 允许重发已经发过的图。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        entries = self._history_read(umo)
        days = self._history_days()
        idx = max(1, int(index or 1))
        window = max(0, min(days * 24, int(hours or 0)))
        cutoff = time.time() - window * 3600 if window else 0
        want_file = os.path.basename(str(file or "").strip())

        if want_file:
            entries = [e for e in entries if e["file"] == want_file]
            if not entries:
                return (
                    f"本会话历史里没有「{want_file}」。"
                    "先用 comfy_gallery 查文件名，再填 file。"
                )
        else:
            if cutoff:
                entries = [e for e in entries if e["ts"] >= cutoff]
            if not entries:
                span = f"最近 {window} 小时" if window else f"保留期（{days} 天）"
                return (
                    f"本会话{span}没有画好的图。"
                    f"（历史最多保留 {days} 天 / {self._history_limit()} 张）"
                )
            if idx > len(entries):
                return (
                    f"本会话现在有 {len(entries)} 张可发的图，取不到第 {idx} 张。"
                    "先用 comfy_gallery 看列表。"
                )

        entry = entries[0] if want_file else entries[idx - 1]
        path = self._session_image_dir(umo) / entry["file"]
        if not path.is_file():
            return f"历史里有这条记录，但文件已经不在了：{entry['file']}"

        fp = self._file_print(str(path)) or os.path.realpath(str(path))
        marks = self._sent_at.get(umo) or {}
        last_sent = marks.get(fp) or 0
        if not force and last_sent and time.time() - last_sent < 60:
            when = time.strftime("%H:%M:%S", time.localtime(last_sent))
            return (
                f"这张（{os.path.basename(str(path))}，{when}）刚发过，"
                "60 秒内不重复发。确实要再发一次就加 force=True。"
            )

        ok, wait = self._image_allowed(umo, event)
        if not ok:
            return f"这个会话 {wait} 秒内刚发过图，先不重复发了。"
        await self._pace_send()
        self._last_image_at[umo] = time.time()

        if self._is_group_chat(event):
            try:
                raw = Path(path).read_bytes()
            except OSError:
                raw = b""
            if await self._nsfw_blocked(event, raw):
                return f"这张图（{os.path.basename(str(path))}）被判为 NSFW，没有发到群里。"

        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Image(file=str(path))]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[comfy] 发送已有图失败: {exc}", exc_info=True)
            return f"找到图了但发送失败：{type(exc).__name__}: {exc}"

        bucket = self._sent_files.setdefault(umo, [])
        if fp:
            bucket.append(fp)
            del bucket[:-50]
        self._sent_at.setdefault(umo, {})[fp] = time.time()

        busy = bool(getattr(self._client(), "_busy", False)) or self._render_lock.locked()
        prompt = (entry.get("prompt") or "").strip()
        detail = f"（{time.strftime('%Y-%m-%d %H:%M', time.localtime(entry['ts']))}"
        if prompt:
            detail += "，" + prompt[:60]
        detail += "）"
        rest = len(entries) - 1
        note = "（新一轮还在画，这是目前最新的一张）" if busy and idx == 1 and not want_file else ""
        return (
            f"已把图发过来了：{os.path.basename(str(path))}{detail}。"
            f"{note}"
            + (f"\n（本会话还有 {rest} 张更早的，index=2 取上一张）" if rest > 0 else "")
        )

    @filter.llm_tool(name="comfy_gallery")
    async def comfy_gallery(
        self,
        event: AstrMessageEvent,
        query: str = "",
        limit: int = 20,
        days: int = 0,
    ) -> str:
        """列出本会话画过的图（按时间从新到旧），用来追溯旧图。

        Args:
            query(string): 在正向提示词/文件名/工作流里搜关键词；留空 = 最近全部。
            limit(int): 最多列几条，默认 20，上限 50。
            days(int): 只看最近几天；0（默认）= 全部保留期。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        all_entries = self._history_read(umo)
        positions = {e["file"]: i for i, e in enumerate(all_entries, 1)}
        entries = all_entries
        max_days = self._history_days()
        window = max(0, min(max_days, int(days or 0)))
        if window:
            cutoff = time.time() - window * 86400
            entries = [e for e in entries if e["ts"] >= cutoff]
        key = str(query or "").strip().lower()
        if key:
            entries = [
                e for e in entries
                if key in (e.get("prompt") or "").lower()
                or key in e["file"].lower()
                or key in (e.get("preset") or "").lower()
            ]
        if not entries:
            return (
                "本会话历史里没有匹配的图。"
                f"（保留 {max_days} 天 / {self._history_limit()} 张）"
            )
        limit = max(1, min(50, int(limit or 20)))
        lines = [
            f"本会话画过的图（匹配 {len(entries)} 条，最多列 {limit} 条；"
            "发图用 comfy_output(index=N) 或 file=...）："
        ]
        for e in entries[:limit]:
            when = time.strftime("%m-%d %H:%M", time.localtime(e["ts"]))
            prompt = (e.get("prompt") or "").strip()[:70] or "（老图，没有记录提示词）"
            preset = e.get("preset") or "?"
            lines.append(
                f"index={positions[e['file']]} [{when}] {preset} · {prompt}\n"
                f"   file={e['file']}"
            )
        if len(entries) > limit:
            lines.append(f"……还有 {len(entries) - limit} 条，可加 limit 或 query 缩小范围。")
        return "\n".join(lines)

    @filter.llm_tool(name="comfy_status")
    async def comfy_status(self, event: AstrMessageEvent) -> str:
        """看 ComfyUI 后端的运行状态（起没起来、端口、显存、队列）。"""
        gate = self._gate(event, "comfy_status")
        if gate:
            return gate
        client = self._client()
        stats = await asyncio.to_thread(client.healthy_sync)
        if not stats:
            return (
                f"ComfyUI 没在运行（端口 {client.port}）。\n"
                "下一次 comfy_generate 会自动拉起它。"
            )
        sysinfo = stats.get("system") or {}
        devices = stats.get("devices") or []
        lines = [
            f"ComfyUI 在运行（端口 {client.port}，版本 {sysinfo.get('comfyui_version', '?')}）。",
            f"进程归属：{'插件拉起的（空闲会自动退出）' if client.owned() else '外部启动的（插件不会杀它）'}",
            f"显存里的模型：{'有（空闲 5 分钟后会卸掉）' if client.model_loaded() else '已卸载'}",
        ]
        for d in devices[:2]:
            total = float(d.get("vram_total") or 0) / 1024**3
            free = float(d.get("vram_free") or 0) / 1024**3
            lines.append(f"显存：{d.get('name', '?')} 共 {total:.1f} GB，空闲 {free:.1f} GB")
        queue = await client.queue_status()
        lines.append(f"队列：运行中 {queue['running']}，排队 {queue['pending']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  杂项
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    #  语音（IndexTTS-2 克隆音色）
    # ------------------------------------------------------------------
    async def _ensure_voice_ref(self, client: ComfyClient) -> str:
        """把参考音频传进 ComfyUI 的 input 目录，返回 LoadAudio 认的名字。"""
        raw = self._conf_str("comfy_voice_reference", "参考音频.wav")
        p = Path(raw)
        if not p.is_absolute():
            base = Path(self._conf_str("comfy_dir") or ".")
            cand = base / "ComfyUI" / "input" / raw
            p = cand if cand.exists() else base / raw
        if not p.exists():
            raise ComfyError(f"参考音频不存在：{p}（配置项 comfy_voice_reference）")
        key = f"{p}|{p.stat().st_mtime_ns}"
        if self._voice_ref_key == key and self._voice_ref_name:
            return self._voice_ref_name
        name = await client.upload_image(p.read_bytes(), p.name)
        self._voice_ref_key, self._voice_ref_name = key, name
        return name

    @filter.llm_tool(name="comfy_say")
    async def comfy_say(
        self,
        event: AstrMessageEvent,
        text: str,
        speed: float = 0.0,
        emotion: str = "",
    ) -> str:
        """用参考音频的克隆音色说一句话，发成语音消息。

        ★ 这是"说话"本身：调用前不要写任何文字，调用后也不要写文字或解释。
          想配表情包 / 图片，就再调对应的工具（表情包、画图），那些会照常发出。
        ★ 情绪要克制：默认用「平静」或「温柔」；只有情景明显激动时才用害羞/生气/撒娇，
          而且不要每句都加。宁可平淡一点，也不要夸张——拉满会听起来发飘、亢奋。
        ★ 按需使用：用户明确要语音、或情景适合出声时才调。短句约 6 秒合成。

        Args:
            text(string): 要说出口的话（中文）。停顿起步 1 秒：写 -1s- 就是停 1 秒，
                别写 -0.2s- 这种碎停顿；省略号（……）、连续逗号（，，）会自动转成停顿。
            speed(float): 语速 0.5~2.0（越小越慢）；0 = 配置默认。
            emotion(string): 情绪，可选：开心 / 很高兴 / 生气 / 难过 / 委屈 / 害怕 /
                害羞 / 惊讶 / 讨厌 / 低落 / 平静 / 温柔 / 撒娇；留空 = 平淡。
        """
        text = str(text or "").strip()
        if not text:
            return "发语音失败：text 是空的。"
        if len(text) > 300:
            text = text[:300]
        client = self._client()
        speed_val = float(speed or 0) or self._conf_float("comfy_voice_speed", 0.9)
        beams = self._conf_int("comfy_voice_beams", 1)
        emotion_val = tts.normalize_emotion(
            emotion or self._conf_str("comfy_voice_emotion")
        )
        async with self._gen_slot(event):
            try:
                if not await asyncio.to_thread(client.healthy_sync):
                    await client.ensure_server()
                ref = await self._ensure_voice_ref(client)
                graph = tts.build(
                    text=text,
                    reference=ref,
                    speed=speed_val,
                    beams=beams,
                    emotion=emotion_val,
                    pause_seconds=self._conf_float("comfy_voice_pause", 1.0),
                )
                result = await client.generate(graph)
                audio = result["images"][0]
                data = await client.fetch_image(audio)
            except ComfyError as exc:
                logger.warning(f"[comfy] 语音合成失败: {exc}")
                return f"发语音失败：{exc}"
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[comfy] 语音合成异常: {exc}", exc_info=True)
                return f"发语音失败：{type(exc).__name__}: {exc}"
        umo_voice = str(getattr(event, "unified_msg_origin", "") or "")
        vok, vwait = self._voice_allowed(umo_voice)
        if not vok:
            return f"语音先不发了：这个会话 {vwait} 秒内刚发过，稍后再发。"
        await self._pace_send()
        self._last_voice_at[umo_voice] = time.time()
        path = self._save_generated(data, str(audio.get("filename") or "voice.mp3"))
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Record(file=str(path))]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[comfy] 发送语音失败: {exc}", exc_info=True)
            return f"语音生成好了但发送失败：{type(exc).__name__}: {exc}"
        # 记下"这一轮已经用语音说过话" —— 钩子会把同轮的文字整段去掉
        self._spoken[str(getattr(event, "unified_msg_origin", "") or "")] = (
            text,
            time.time(),
        )
        used = [f"{len(data):,} 字节", f"{result['seconds']} 秒"]
        if emotion_val:
            used.append(f"情绪 {emotion_val}")
        return (
            f"语音已经发出去了（{' · '.join(used)}）。\n"
            "★ 这一轮不要再写文字：不要重复语音内容，也不要解释刚发了语音。"
            "想配表情包/图片就调对应工具（那些会照常发出去）。"
        )

    @filter.on_decorating_result()
    async def _voice_turn_no_text(self, event: AstrMessageEvent) -> None:
        """语音轮强制不带文字：这一轮刚发过语音 → 把最终消息里的纯文字全去掉。

        ★ 只动 Plain，图片/表情/语音等组件照常保留 —— 这样"语音 + 表情包"
          这种组合仍然成立。
        ★ 受 comfy_voice_drop_text 开关控制；关掉就退回"只去重复文字"。
        """
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            record = self._spoken.pop(umo, None)
            if not record:
                return
            spoken, stamp = record
            if time.time() - stamp > 120:
                return
            result = event.get_result()
            chain = getattr(result, "chain", None) if result is not None else None
            if not chain:
                return

            drop_all = self._conf_bool("comfy_voice_drop_text", True)
            spoken_n = self._norm_text(spoken)
            kept = []
            removed = 0
            for comp in chain:
                if isinstance(comp, Comp.Plain):
                    text_n = self._norm_text(getattr(comp, "text", "") or "")
                    if not text_n:
                        kept.append(comp)
                        continue
                    if drop_all:
                        removed += 1
                        continue
                    ratio = difflib.SequenceMatcher(None, text_n, spoken_n).ratio()
                    if ratio >= 0.8 or text_n in spoken_n or spoken_n in text_n:
                        removed += 1
                        continue
                kept.append(comp)
            if removed:
                result.chain = kept
                logger.info(f"[comfy] 语音轮去掉了 {removed} 段文字（drop_all={drop_all}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 语音轮去文字失败（忽略）: {exc}")

    @staticmethod
    def _norm_text(text: str) -> str:
        return re.sub(r"[\s，。！？、,.!?～~…—\-（）()\[\]【】「」“”\"'：:；;]", "", text or "")

    @staticmethod
    def _session_image_dir(umo: str) -> Path:
        """每个会话一个目录：data/plugin_data/ncd_comfy/out/<sha1(umo)[:16]>/"""
        import hashlib

        key = hashlib.sha1(str(umo or "").encode("utf-8")).hexdigest()[:16]
        path = (
            Path(PLUGIN_DIR).parents[2]
            / "data"
            / "plugin_data"
            / "ncd_comfy"
            / "out"
            / key
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _history_days(self) -> int:
        try:
            days = int(self._conf_int("comfy_history_days", 30))
        except Exception:  # noqa: BLE001
            days = 30
        return max(1, min(3650, days))

    def _history_limit(self) -> int:
        try:
            limit = int(self._conf_int("comfy_history_per_session", 200))
        except Exception:  # noqa: BLE001
            limit = 200
        return max(1, min(5000, limit))

    def _history_read(self, umo: str) -> list[dict]:
        """读本会话的画图索引；没有索引就按目录里的图补一份。"""
        folder = self._session_image_dir(umo)
        raw: list[dict] = []
        path = folder / HISTORY_FILE
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = list((data or {}).get("items") or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[comfy] 读历史索引失败，按目录重建: {exc}")
                raw = []
        items: dict[str, dict] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = os.path.basename(str(entry.get("file") or ""))
            if not name:
                continue
            fp = folder / name
            if not fp.is_file():
                continue
            try:
                stamp = float(entry.get("ts") or fp.stat().st_mtime)
            except Exception:  # noqa: BLE001
                stamp = fp.stat().st_mtime
            items[name] = {
                "file": name,
                "ts": stamp,
                "size": int(entry.get("size") or fp.stat().st_size),
                "prompt": str(entry.get("prompt") or ""),
                "preset": str(entry.get("preset") or ""),
                "model": str(entry.get("model") or ""),
            }
        if not items:
            # 老目录还没索引：扫一遍图片，至少能按时间取
            try:
                for fp in folder.iterdir():
                    if not fp.is_file():
                        continue
                    if fp.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                        continue
                    items[fp.name] = {
                        "file": fp.name,
                        "ts": fp.stat().st_mtime,
                        "size": fp.stat().st_size,
                        "prompt": "",
                        "preset": "",
                        "model": "",
                    }
            except OSError:
                pass
        return sorted(items.values(), key=lambda x: x["ts"], reverse=True)

    def _history_write(self, umo: str, items: list[dict]) -> None:
        folder = self._session_image_dir(umo)
        path = folder / HISTORY_FILE
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps({"version": 1, "items": items}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 写历史索引失败: {exc}")

    def _history_prune(self, umo: str, items: list[dict]) -> list[dict]:
        """按天数和每会话张数裁剪，顺便删掉不再保留的图。"""
        cutoff = time.time() - self._history_days() * 86400
        kept = [e for e in sorted(items, key=lambda x: x["ts"], reverse=True)
                if e["ts"] >= cutoff][: self._history_limit()]
        keep_names = {e["file"] for e in kept}
        folder = self._session_image_dir(umo)
        try:
            for fp in folder.iterdir():
                if not fp.is_file() or fp.name == HISTORY_FILE:
                    continue
                if fp.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                    continue
                if fp.name not in keep_names:
                    fp.unlink(missing_ok=True)
        except OSError:
            pass
        self._history_write(umo, kept)
        return kept

    def _save_session_image(
        self,
        umo: str,
        data: bytes,
        filename: str,
        meta: dict | None = None,
    ) -> Path:
        """把生成图落到会话目录，写进历史索引，并按 30 天 / 200 张裁剪。"""
        import re as _re
        import uuid as _uuid

        folder = self._session_image_dir(umo)
        safe = _re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "image.png"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = folder / f"{stamp}-{_uuid.uuid4().hex[:6]}-{safe}"
        path.write_bytes(data)
        meta = meta or {}
        items = {e["file"]: e for e in self._history_read(umo)}
        items[path.name] = {
            "file": path.name,
            "ts": time.time(),
            "size": len(data),
            "prompt": str(meta.get("prompt") or "")[:300],
            "preset": str(meta.get("preset") or ""),
            "model": str(meta.get("model") or ""),
        }
        self._history_prune(umo, list(items.values()))
        return path

    @staticmethod
    def _save_generated(data: bytes, filename: str) -> Path:
        """把图落到 AstrBot 的临时目录（有定时清理，不用我们管回收）。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            base = Path(get_astrbot_temp_path())
        except Exception:  # noqa: BLE001
            base = Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "image.png"
        path = base / f"comfy_{uuid.uuid4().hex[:8]}_{safe}"
        path.write_bytes(data)
        return path

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("comfyui", alias={"comfy"})
    async def comfyui_cmd(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """开关 ComfyUI 进程：/comfyui off 关闭，/comfyui on 启动，不带参数看状态。"""
        sub = str(args or "").strip().lower()
        client = self._client()
        port = self._conf_int("comfy_port", 8199)

        if sub in ("off", "stop", "close", "关", "关闭"):
            if not client.running():
                return (
                    "ComfyUI 没在运行（可能已经关了，或者这个进程不是插件拉起的，"
                    "我不会去动别人的进程）。"
                )
            client.stop()
            logger.info("[comfy] 用户执行 /comfyui off，已停止 ComfyUI")
            return "已关闭 ComfyUI。下次出图或发语音时会自动重新拉起。"

        if sub in ("on", "start", "开", "启动"):
            try:
                await client.ensure_server()
            except Exception as exc:  # noqa: BLE001
                return f"启动 ComfyUI 失败：{type(exc).__name__}: {exc}"
            return f"ComfyUI 已启动（端口 {port}）。"

        if sub not in ("", "status", "状态", "?"):
            return "用法：/comfyui off 关闭 · /comfyui on 启动 · /comfyui 看状态"

        running = client.running()
        try:
            healthy = bool(await asyncio.to_thread(client.healthy_sync))
        except Exception:  # noqa: BLE001
            healthy = False
        if running:
            return f"ComfyUI 由插件拉起，运行中（端口 {port}，健康检查{'通过' if healthy else '失败'}）。"
        if healthy:
            return f"端口 {port} 上有 ComfyUI 在跑，但不是插件拉起的（我不会去关它）。"
        return f"ComfyUI 未运行（端口 {port}）。出图或发语音时会自动拉起。"

    async def terminate(self) -> None:
        """插件卸载/重载时收掉后台任务和自己拉起的 ComfyUI。"""
        pending = list(self._bg_tasks)
        umos = {self._bg_umo.get(task, "") for task in pending}
        for task in pending:
            task.cancel()
        for umo in umos:
            if umo:
                try:
                    await self._send_chat(umo, "图画被中断了（插件重载/退出）。")
                except Exception:  # noqa: BLE001
                    pass
        self._bg_tasks.clear()
        self._bg_umo.clear()
        try:
            if self._client_obj is not None:
                self._client_obj.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 停止 ComfyUI 失败（忽略）: {exc}")
        self._client_obj = None
