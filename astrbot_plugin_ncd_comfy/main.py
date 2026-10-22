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
import difflib
import os
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.command import GreedyStr
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

TOOL_NAMES = ("comfy_tools", "comfy_generate", "comfy_models", "comfy_status", "comfy_say", "comfy_output")

TOOL_SUMMARY = {
    "comfy_tools": "comfy_tools() —— 出图入口（必须先调）：四个工作流、参数范围、当前默认值",
    "comfy_generate": "comfy_generate(prompt, ...) —— 按预设工作流出图并发到会话",
    "comfy_models": "comfy_models(kind) —— 列底模 / 列 LoRA",
    "comfy_status": "comfy_status() —— ComfyUI 起没起来、显存、队列",
    "comfy_say": "comfy_say(text) —— 用参考音频的克隆音色合成一条语音发出去（慢，按需用）",
}

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
        self._last_image_bytes: bytes | None = None
        # 模型清单缓存（目录扫一遍要几百毫秒，别每次出图都扫）
        self._scan_cache: dict[str, tuple[float, list[dict]]] = {}
        # 参考音频上传缓存（改了文件会自动重传）
        self._voice_ref_key: str | None = None
        self._voice_ref_name: str = ""
        # 刚发过的语音文本（同一轮去掉重复文字用）
        self._spoken: dict[str, tuple[str, float]] = {}

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
        """预设对应的底模清单（带 60 秒缓存）。"""
        meta = presets.PRESETS[preset]
        kind = "checkpoints" if meta.get("model_kind") == "checkpoint" else "diffusion_models"
        key = f"model:{kind}"
        hit = self._scan_cache.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        items = model_paths.scan(
            self._categories(kind),
            min_mb=max(0.0, self._conf_float("comfy_min_model_gb", 0.5)),
        )
        self._scan_cache[key] = (time.time(), items)
        return items

    def _scan_loras(self) -> list[dict]:
        key = "lora"
        hit = self._scan_cache.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        items = model_paths.scan(self._categories("loras"), min_mb=0.0)
        self._scan_cache[key] = (time.time(), items)
        return items

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
        position = self._gen_waiting
        try:
            async with self._render_lock:
                # ★ 显存换手：先把本地模型卸掉，再等它安静下来；跑的时候占住标记
                await self._unload_llm()
                await self._wait_llm_idle()
                self._gpu_write(comfy=1, comfy_ts=time.time())
                try:
                    yield
                finally:
                    self._gpu_write(comfy=0, comfy_ts=time.time())
                    # 生成完立刻释放 ComfyUI 显存，LM Studio 下次请求会自动重载
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

    async def _reference_bytes(self, reference: str, event: AstrMessageEvent) -> bytes | None:
        """把 reference 解析成图片字节。

        · ""            → 不用参考图（走文生图）
        · "message"     → 这条 / 被引用消息里的图
        · "last"        → 上一张生成的图
        ★ 认不出、或没有图 → 返回 None，由调用方给出明确错误（不要静默退回文生图，
          那会让人以为"改了图"，实际出来一张全新的图）。
        """
        ref = str(reference or "").strip().lower()
        if ref in ("last", "previous", "上一张", "刚才那张"):
            if self._last_image_bytes:
                return self._last_image_bytes
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
    #  工具入口
    # ------------------------------------------------------------------
    def _gate(self, event: AstrMessageEvent, tool: str) -> str | None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo in self._revealed:
            return None
        logger.info(f"[comfy] {tool} 被直接调用（未先调 comfy_tools），已返回指正")
        return GATE_ERROR.format(tool=tool)

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
            "另外：comfy_say(text) 可以用克隆音色说一句话、发成语音（约 6 秒一条）。",
            "超时兜底：comfy_output() 可以把 output 里已经画好的图直接发出来，不用重画。",
            "默认风格：sdxl 预设会自动叠 cunnyfunky LoRA（强度 0.8）；要换就用 lora 参数覆盖。",
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
            "· reference   图生图：message=这条/被引用消息里的图；last=上一张；留空=文生图",
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

        # 底模
        model_name = ""
        if (model or "").strip():
            try:
                model_name = self._resolve_name(model, self._scan(preset_name), "底模")
            except ComfyError as exc:
                return f"出图失败：{exc}"

        # LoRA（提交前动态插节点）
        loras: list[dict] = []
        if (lora or "").strip():
            default_strength = self._conf_float("comfy_lora_strength", 1.0)
            pool = self._scan_loras()
            if not pool:
                return "出图失败：LoRA 目录里一个文件都没扫到（检查 comfy_extra_paths）"
            try:
                for spec in re.split(r"[,，]", lora):
                    spec = spec.strip()
                    if not spec:
                        continue
                    name_part, _, strength_part = spec.partition(":")
                    resolved = self._resolve_name(name_part, pool, "LoRA")
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
                    auto_name = self._resolve_name(auto_lora, self._scan_loras(), "LoRA")
                    loras.append(
                        {
                            "name": auto_name,
                            "strength": self._conf_float(
                                "comfy_default_lora_strength", 0.8
                            ),
                        }
                    )
                except ComfyError as exc:
                    logger.warning(f"[comfy] 默认 LoRA 没找到（忽略）: {exc}")

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

                result = await client.generate(final, on_sampler_start=_on_sampler)
                image = result["images"][0]
                data = await client.fetch_image(image)
            except ComfyError as exc:
                logger.warning(f"[comfy] 出图失败: {exc}")
                return f"出图失败：{exc}"
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[comfy] 出图异常: {exc}", exc_info=True)
                return f"出图失败：{type(exc).__name__}: {exc}"

        filename = str(image.get("filename") or "image.png")
        path = self._save_generated(data, filename)
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Image.fromFileSystem(str(path))]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[comfy] 发送生成的图失败: {exc}", exc_info=True)
            return f"图画好了但发送失败：{type(exc).__name__}: {exc}"

        self._last_preset = preset_name
        self._last_model = used_model
        # 下一张要 reference="last" 的话就用它
        self._last_image_bytes = data
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
        return (
            f"图已经画好并发到会话里了（{filename}，{len(data):,} 字节，"
            f"ComfyUI 用时 {result['seconds']} 秒）。\n"
            f"参数：{' · '.join(used)}\n"
            "不要再重复调用本工具；用户还想要就换一个新的画面描述。"
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
        self, event: AstrMessageEvent, index: int = 1, hours: int = 6
    ) -> str:
        """把 ComfyUI 已经画好的图直接发到会话里（不用重画）。

        ★ 用途：上一次出图超时或连接断了，但 ComfyUI 其实已经跑完 ——
          图就躺在 output 目录里。这时先调这个把最新那张取回来，
          不要重新生成（重画又得等几分钟）。

        Args:
            index(int): 1 = 最新一张，2 = 倒数第二张……默认 1。
            hours(int): 只找最近几小时内生成的，默认 6，最大 72。
        """
        idx = max(1, int(index or 1))
        window = max(1, min(72, int(hours or 6)))
        client = self._client()
        dirs = [d for d in (client.output_dir, client.temp_dir) if d]
        base = self._conf_str("comfy_dir")
        if base:
            dirs.extend(
                [
                    os.path.join(base, "ComfyUI", "output"),
                    os.path.join(base, "ComfyUI", "temp"),
                ]
            )

        cutoff = time.time() - window * 3600
        found: list[tuple[float, str]] = []
        for folder in dict.fromkeys(dirs):
            if not os.path.isdir(folder):
                continue
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    continue
                path = os.path.join(folder, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime >= cutoff:
                    found.append((mtime, path))
        found.sort(reverse=True)
        if not found:
            return f"最近 {window} 小时里没有找到已经画好的图。"
        if idx > len(found):
            return f"最近 {window} 小时只有 {len(found)} 张图，取不到第 {idx} 张。"
        mtime, path = found[idx - 1]
        try:
            await self.context.send_message(
                event.unified_msg_origin, MessageChain(chain=[Comp.Image(file=path)])
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[comfy] 发送已有图失败: {exc}", exc_info=True)
            return f"找到图了但发送失败：{type(exc).__name__}: {exc}"
        return (
            f"已把最近生成的第 {idx} 张发过来了：{os.path.basename(path)}"
            f"（{time.strftime('%H:%M:%S', time.localtime(mtime))} 生成，"
            f"{os.path.getsize(path):,} 字节）。"
        )

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
        """插件卸载/重载时收掉自己拉起的 ComfyUI。"""
        try:
            if self._client_obj is not None:
                self._client_obj.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[comfy] 停止 ComfyUI 失败（忽略）: {exc}")
        self._client_obj = None
