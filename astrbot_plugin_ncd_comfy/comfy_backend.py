# -*- coding: utf-8 -*-
"""ComfyUI 后端客户端：按需拉起最小便携版，走 HTTP 出图。

职责：
  · 第一次要出图时按需拉起 ComfyUI（子进程、隐藏窗口），之后复用
  · 空闲超时自己收掉进程，把显存还回去
  · 只通过 HTTP 说话（/prompt、/history、/view、/object_info），不碰它的内部代码

★ 只管【自己拉起来的】进程：端口上已经有别人开的 ComfyUI 时直接用，绝不杀。
★ 所有阻塞请求都放在 asyncio.to_thread 里，别把 AstrBot 的事件循环卡死。
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from astrbot.api import logger


class ComfyError(Exception):
    """ComfyUI 后端的可预期失败（要给人话，不是栈）。"""


def _exit_text(code: int | None) -> str:
    """把 Windows 的 4294967295 归一成 -1，避免用户看到天文数字。"""
    if code is None:
        return "未知"
    normalized = code - 0x100000000 if code > 0x7FFFFFFF else code
    if normalized < 0:
        return f"{normalized}（0x{code & 0xFFFFFFFF:08X}）"
    return str(normalized)


def _log_tail(path: Path | None, limit: int = 2000) -> str:
    """读 ComfyUI 日志尾部（启动失败/崩溃时贴给用户看）。

    ★ 只读文件最后几 KB，不把 4MB 日志整个读进内存。
    """
    if not path:
        return ""
    try:
        if not path.exists():
            return ""
        with path.open("rb") as fp:
            fp.seek(0, 2)
            size = fp.tell()
            fp.seek(max(0, size - limit * 4))
            text = fp.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    return text[-limit:].strip()


class ComfyClient:
    def __init__(
        self,
        *,
        comfy_dir: str,
        python: str = "",
        port: int = 8199,
        host: str = "127.0.0.1",
        idle_timeout: float = 300.0,
        unload_timeout: float = 300.0,
        startup_timeout: float = 240.0,
        gen_timeout: float = 180.0,
        extra_args: str = "",
        output_dir: str = "",
        temp_dir: str = "",
        log_path: Path | None = None,
    ) -> None:
        # ★ 最小便携版根目录（里面有 python_embeded 和 ComfyUI 两个子目录）
        self.comfy_dir = Path(str(comfy_dir or "").strip().strip('"'))
        # 留空 = <comfy_dir>\python_embeded\python.exe
        self.python = str(python or "").strip().strip('"')
        self.port = int(port or 8199)
        # ★ 2026-10-30：ComfyUI 不一定在本机。
        #   手机端跑 AstrBot、电脑端跑 ComfyUI 时，这里填电脑的局域网 IP
        #   （电脑热点是 192.168.137.1），否则插件永远只会连手机自己的 127.0.0.1。
        self.host = str(host or "").strip().strip('"') or "127.0.0.1"
        self.idle_timeout = float(idle_timeout or 300.0)
        # ★ 两档回收：先卸模型（还显存、进程留着），再关进程（连内存也还）
        self.unload_timeout = float(unload_timeout or 300.0)
        self.startup_timeout = float(startup_timeout or 240.0)
        self.gen_timeout = float(gen_timeout or 180.0)
        self.extra_args = str(extra_args or "").strip()
        self.output_dir = str(output_dir or "").strip()
        self.temp_dir = str(temp_dir or "").strip()
        self.log_path = Path(log_path) if log_path else None
        self.base = f"http://{self.host}:{self.port}"

        self._proc: subprocess.Popen | None = None
        self._owned = False  # True = 进程是我们拉起来的（空闲时才敢杀）
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._idle_task: asyncio.Task | None = None
        self._object_info: dict | None = None
        self._busy = False  # 正在出图（空闲自退要跳过）
        self._starting = False  # 正在冷启动（同样不能杀）
        self._model_loaded = False  # 显存里有没有模型（决定该不该先卸模型）

    # ------------------------------------------------------------------
    #  路径
    # ------------------------------------------------------------------
    @property
    def main_py(self) -> Path:
        return self.comfy_dir / "ComfyUI" / "main.py"

    def resolved_python(self) -> str:
        if self.python:
            return self.python
        return str(self.comfy_dir / "python_embeded" / "python.exe")

    def check_paths(self) -> None:
        """配置自检：给人话错误，而不是等到 Popen 抛 FileNotFoundError。"""
        if not str(self.comfy_dir):
            raise ComfyError(
                "没配 comfy_dir（最小 ComfyUI 便携版的根目录，例如 F:\\ComfyUI_min）"
            )
        if not self.comfy_dir.exists():
            raise ComfyError(f"comfy_dir 不存在: {self.comfy_dir}")
        if not self.main_py.exists():
            raise ComfyError(
                f"找不到 {self.main_py}。comfy_dir 应该指向【包含 ComfyUI\\main.py "
                "和 python_embeded\\ 的那一层】"
            )
        py = self.resolved_python()
        if not os.path.exists(py):
            raise ComfyError(f"ComfyUI 用的 python 不存在: {py}")

    # ------------------------------------------------------------------
    #  进程管理
    # ------------------------------------------------------------------
    def _health_sync(self, timeout: float = 2.0) -> dict | None:
        """探活：/system_stats 能返回带 system 的 JSON 才算 ComfyUI。"""
        try:
            with urllib.request.urlopen(f"{self.base}/system_stats", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict) or not isinstance(data.get("system"), dict):
            return None
        return data

    def healthy_sync(self) -> dict | None:
        return self._health_sync()

    def _port_busy(self) -> bool:
        """端口有没有人在听（TCP 连得上就算）。"""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex((self.host, self.port)) == 0

    @property
    def is_remote(self) -> bool:
        """ComfyUI 是不是在别的机器上（远程模式不归我们管进程）。"""
        return self.host not in ("127.0.0.1", "localhost", "::1")

    def list_object_names(self, node: str, field: str, timeout: float = 8.0) -> list[str]:
        """从 /object_info/<node> 取某个输入字段的可选值（就是模型名列表）。

        ★ 2026-10-30：远程模式下 comfy_dir 是对方机器的路径，本机读不到
          extra_model_paths.yaml，扫目录永远是空。而 ComfyUI 的 /object_info
          本来就会列出它自己认得的模型名 —— 直接问它更准，也能反映对方的真实模型库。
          代价：拿不到文件大小（调用方自己处理 size_mb=0）。
        """
        try:
            with urllib.request.urlopen(
                f"{self.base}/object_info/{node}", timeout=timeout
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return []
        try:
            spec = data[node]["input"]["required"][field][0]
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(spec, list):
            return []
        return [str(x) for x in spec if isinstance(x, str)]

    def list_models_sync(self, kind: str) -> list[str]:
        """按类别取模型名：checkpoints / loras / diffusion_models。"""
        node_field = {
            "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
            "diffusion_models": ("UNETLoader", "unet_name"),
            "loras": ("LoraLoader", "lora_name"),
        }.get(kind)
        if not node_field:
            return []
        return self.list_object_names(node_field[0], node_field[1])

    def _drop_stale_proc(self) -> None:
        """丢掉已经退出的旧进程句柄，改为接管端口上的 ComfyUI。

        ★ 场景：插件拉起的 ComfyUI 被手动重启/换成了外部进程。
          端口健康但旧 _proc 已死，若继续拿它判断，会把正常任务误判成
          “出图过程中 ComfyUI 退出了”。
        """
        self._proc = None
        self._owned = False

    async def ensure_server(self) -> dict:
        """确保有一个可用的 ComfyUI。返回它的 /system_stats。"""
        async with self._lock:
            stats = await asyncio.to_thread(self._health_sync, 2.0)
            if stats is not None:
                if self._proc is not None and self._proc.poll() is not None:
                    logger.warning("[comfy] 端口健康但旧进程已退出，接管外部 ComfyUI")
                    self._drop_stale_proc()
                self._last_used = time.time()
                return stats
            if self.is_remote:
                # ★ 远程模式：连不上就直说。绝不往下走 check_paths() ——
                #   手机上没有 ComfyUI，报「comfy_dir 不存在」只会让人更糊涂。
                raise ComfyError(
                    f"远程 ComfyUI 不可达：{self.host}:{self.port}。"
                    "请确认：① 电脑已启动 ComfyUI 且用 --listen 0.0.0.0；"
                    "② 手机连的是电脑热点（本机 IP 应形如 192.168.137.x）；"
                    "③ 电脑防火墙已放行该端口。"
                )
            if await asyncio.to_thread(self._port_busy):
                raise ComfyError(
                    f"端口 {self.port} 被别的程序占着（不是 ComfyUI）。"
                    "改一个 comfy_port，或先关掉那个程序。"
                )
            self.check_paths()
            # ★ 冷启动期间 _starting=True，空闲循环不能把它当"没人用"杀掉
            self._starting = True
            try:
                self._spawn()
                stats = await self._wait_ready()
            finally:
                self._starting = False
            self._last_used = time.time()
            return stats

    def _spawn(self) -> None:
        if self.is_remote:
            # 保险：远程模式下绝不该走到这里（ensure_server 会提前报错）。
            raise ComfyError(
                f"comfy_host={self.host} 是远程主机，插件不会在本地拉起 ComfyUI。"
            )
        if self._proc is not None and self._proc.poll() is None:
            return
        cmd = [
            self.resolved_python(),
            "-s",
            str(self.main_py),
            "--port",
            str(self.port),
            "--listen",
            "127.0.0.1",
            "--disable-auto-launch",
            "--preview-method",
            "none",
        ]
        if self.output_dir:
            cmd += ["--output-directory", self.output_dir]
        if self.temp_dir:
            cmd += ["--temp-directory", self.temp_dir]
        if self.extra_args:
            cmd += shlex.split(self.extra_args)
        logger.info(f"[comfy] 拉起 ComfyUI: {' '.join(cmd)}")
        opened = None
        try:
            if self.log_path:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                opened = open(self.log_path, "ab")  # noqa: SIM115 —— 交给子进程持有
                out = opened
            else:
                out = subprocess.DEVNULL
            self._proc = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=str(self.comfy_dir),
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            raise ComfyError(f"拉起 ComfyUI 失败: {exc}") from exc
        finally:
            # 子进程已经拿到自己的句柄，父进程这份可以关掉（不然每次重拉都漏一个 fd）
            if opened is not None:
                try:
                    opened.close()
                except Exception:  # noqa: BLE001
                    pass
        self._owned = True
        self._object_info = None
        self._last_used = time.time()
        self._start_idle_loop()

    async def _wait_ready(self) -> dict:
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                stats = await asyncio.to_thread(self._health_sync, 1.0)
                if stats is not None:
                    logger.warning("[comfy] 旧进程退出但服务已在线，接管外部 ComfyUI")
                    self._drop_stale_proc()
                    return stats
                raise ComfyError(
                    f"ComfyUI 启动就退出了（exit={_exit_text(self._proc.returncode)}）。"
                    f"日志尾部：{_log_tail(self.log_path) or '（空）'}"
                )
            stats = await asyncio.to_thread(self._health_sync, 1.5)
            if stats is not None:
                logger.info(f"[comfy] ComfyUI 就绪（端口 {self.port}）")
                return stats
            await asyncio.sleep(0.7)
        raise ComfyError(
            f"等 ComfyUI 就绪超过 {self.startup_timeout:.0f} 秒。"
            f"日志尾部：{_log_tail(self.log_path) or '（空）'}"
        )

    def _start_idle_loop(self) -> None:
        """空闲自退：只在【进程是我们拉起来的】时候生效。"""
        if self._idle_task is not None and not self._idle_task.done():
            return
        try:
            self._idle_task = asyncio.get_running_loop().create_task(self._idle_loop())
        except RuntimeError:  # 没有事件循环（离线测试）→ 不启
            self._idle_task = None

    def _should_stop_idle(self) -> bool:
        """现在该不该因为空闲把 ComfyUI 收掉。

        ★ 2026-10-21 事故：这里原来的判断是 `if self._last_used and ...: continue`，
          `_last_used` 初值是 0 —— 于是"刚拉起来 15 秒"被当成"空闲 60 秒"，
          冷启动中的进程被自己杀了。现在把四种"不能杀"的情况都显式列出来。
        """
        gap = self._idle_gap()
        return gap is not None and gap >= self.idle_timeout

    def _should_unload_models(self) -> bool:
        """空闲够久了、而且显存里确实还压着模型 → 该让它卸掉（进程留着）。"""
        gap = self._idle_gap()
        return gap is not None and self._model_loaded and gap >= self.unload_timeout

    def _idle_gap(self) -> float | None:
        """离上次用过去了多少秒；不该计时的情况返回 None。"""
        if not self._owned:
            return None  # 别人开的 ComfyUI，不归我们管
        if self._proc is None or self._proc.poll() is not None:
            return None  # 进程已经没了
        if self._busy or self._starting:
            return None  # 正在出图 / 正在冷启动
        if not self._last_used:
            return None  # 刚起来还没用过 —— 给一轮宽限
        return time.time() - self._last_used

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            if not self._owned:
                return
            if self._proc is None or self._proc.poll() is not None:
                return
            if self._should_stop_idle():
                logger.info(f"[comfy] 空闲超过 {self.idle_timeout:.0f} 秒，关掉 ComfyUI 进程")
                self.stop()
                return
            if self._should_unload_models():
                logger.info(f"[comfy] 空闲超过 {self.unload_timeout:.0f} 秒，卸载模型（进程留着）")
                await self.free_models()

    # ------------------------------------------------------------------
    #  HTTP 基础
    # ------------------------------------------------------------------
    def _get_json_sync(self, path: str, timeout: float = 10.0) -> dict:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post_json_sync(self, path: str, payload: dict, timeout: float) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:1200]
            except Exception:  # noqa: BLE001
                pass
            raise ComfyError(f"ComfyUI 返回 HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ComfyError(f"请求 ComfyUI 失败: {exc}") from exc
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ComfyError(f"ComfyUI 返回的不是 JSON: {raw[:200]!r}") from exc

    # ------------------------------------------------------------------
    #  模型 / 节点信息
    # ------------------------------------------------------------------
    def _object_info_sync(self, node: str) -> dict:
        return self._get_json_sync(f"/object_info/{urllib.parse.quote(node)}", timeout=20)

    async def object_info(self, node: str) -> dict:
        """查某个节点类的定义（带缓存）。"""
        await self.ensure_server()
        if self._object_info is None:
            self._object_info = {}
        if node not in self._object_info:
            self._object_info[node] = await asyncio.to_thread(self._object_info_sync, node)
        return self._object_info[node]

    async def list_from_server(self) -> dict:
        """问 ComfyUI 要「它认的」底模 / 采样器 / 调度器清单。"""
        info = await self.object_info("CheckpointLoaderSimple")
        try:
            ckpts = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        except Exception:  # noqa: BLE001
            ckpts = []
        ks = await self.object_info("KSampler")
        try:
            req = ks["KSampler"]["input"]["required"]
            samplers = req["sampler_name"][0]
            schedulers = req["scheduler"][0]
        except Exception:  # noqa: BLE001
            samplers, schedulers = [], []
        return {
            "checkpoints": list(ckpts or []),
            "samplers": list(samplers or []),
            "schedulers": list(schedulers or []),
        }

    # ------------------------------------------------------------------
    #  出图
    # ------------------------------------------------------------------
    async def generate(self, graph: dict, on_sampler_start=None, on_progress=None) -> dict:
        """提交工作流并等结果。返回 {"images": [{filename, subfolder, type}], "seconds": float}。"""
        await self.ensure_server()
        self._last_used = time.time()
        self._busy = True
        self._model_loaded = True  # 提交就意味着要把模型搬进显存
        try:
            started = time.time()
            client_id = uuid.uuid4().hex
            holder = {"prompt_id": ""}
            ready = asyncio.Event()
            watch = None
            # ★ 先连上 WS 再提交：快任务（缓存命中/少步数）会在连接完成前跑完，
            #   晚连就一个节点事件都收不到，进度条只能停在 0。
            if on_progress is not None or on_sampler_start is not None:
                watch = asyncio.create_task(
                    self._watch_progress(
                        graph,
                        holder,
                        on_progress,
                        on_sampler_start,
                        client_id=client_id,
                        ready=ready,
                    )
                )
                try:
                    await asyncio.wait_for(ready.wait(), timeout=5)
                except asyncio.TimeoutError:
                    logger.debug("[comfy] 进度监听连接超时（继续出图）")
            prompt_id = await asyncio.to_thread(self._submit_sync, graph, client_id)
            holder["prompt_id"] = prompt_id
            self.last_prompt_id = prompt_id
            logger.info(f"[comfy] 已提交 prompt_id={prompt_id}")
            try:
                outputs = await self._wait_history(prompt_id)
                # ★ 收尾补一发 100%：WS 事件可能比 /history 晚到，监听任务会被取消
                if on_progress is not None:
                    try:
                        await on_progress(
                            {
                                "overall": 1.0,
                                "phase": "done",
                                "label": "完成",
                                "elapsed": round(time.time() - started, 1),
                            }
                        )
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                if watch is not None:
                    watch.cancel()
            images = []
            for node_out in (outputs or {}).values():
                # ★ SaveImage 给 images，SaveAudioMP3 给 audio，别的节点可能给 files
                for key in ("images", "audio", "files"):
                    for img in (node_out or {}).get(key, []) or []:
                        if isinstance(img, dict) and img.get("filename") and img.get("type") in ("output", "temp"):
                            images.append(img)
            if not images:
                raise ComfyError("ComfyUI 跑完了，但没有任何图片输出（工作流里要留 SaveImage）")
            return {"images": images, "seconds": round(time.time() - started, 1)}
        finally:
            self._busy = False
            self._last_used = time.time()

    async def _watch_progress(
        self,
        graph: dict,
        holder: dict,
        on_progress=None,
        on_sampler_start=None,
        client_id: str = "",
        ready=None,
    ) -> None:
        """盯 ComfyUI WS，把节点事件折算成总进度，回调给上层。"""
        nodes = {
            str(nid): str((node or {}).get("class_type") or "")
            for nid, node in (graph or {}).items()
            if not str(nid).startswith("_")
        }
        total = len(nodes)
        samplers = {nid for nid, cls in nodes.items() if cls in ("KSampler", "KSamplerAdvanced")}
        cached: set[str] = set()
        done: set[str] = set()
        current = ""
        cur_val = 0
        cur_max = 0
        started = time.time()
        last_emit = 0.0
        last_phase = ""
        sampler_hit = False
        max_overall = 0.0

        phase_cn = {
            "queue": "排队",
            "loading": "加载权重",
            "sampling": "采样",
            "decoding": "解码",
            "saving": "保存",
            "running": "执行",
        }

        def classify(cls: str) -> str:
            low = cls.lower()
            if cls in ("KSampler", "KSamplerAdvanced"):
                return "sampling"
            if "vae" in low and "decode" in low:
                return "decoding"
            if low.startswith("save"):
                return "saving"
            if any(k in low for k in ("loader", "checkpoint", "unet", "clip", "lora")):
                return "loading"
            return "running"

        def weight_of(cls: str) -> float:
            if cls in ("KSampler", "KSamplerAdvanced"):
                return 4.0  # 采样通常占大头，权重给高
            low = cls.lower()
            if any(k in low for k in ("loader", "checkpoint", "unet", "clip", "lora")):
                return 2.0
            return 1.0

        weights = {nid: weight_of(cls) for nid, cls in nodes.items()}
        total_w = sum(weights.values()) or 1.0

        async def emit(force: bool = False) -> None:
            nonlocal last_emit, last_phase, max_overall
            if on_progress is None:
                return
            eff = max(0.001, total_w - sum(weights.get(n, 1.0) for n in cached))
            frac = 0.0
            if cur_max and current and current not in done:
                frac = max(0.0, min(1.0, cur_val / cur_max))
            done_w = sum(weights.get(n, 1.0) for n in done)
            overall = min(1.0, (done_w + weights.get(current, 1.0) * frac) / eff)
            # 节点切换会把“当前节点分数”清零，总体不能因此回退
            max_overall = max(max_overall, overall)
            overall = max_overall
            phase = classify(nodes.get(current, ""))
            now = time.time()
            if not force and now - last_emit < 0.25 and phase == last_phase:
                return
            last_emit = now
            last_phase = phase
            label = phase_cn.get(phase, "执行")
            if phase == "sampling" and cur_max:
                label = f"采样 {cur_val}/{cur_max}"
            elif current:
                label = f"{label}（{nodes.get(current) or '节点'}）"
            try:
                await on_progress(
                    {
                        "overall": round(overall, 4),
                        "phase": phase,
                        "label": label,
                        "node": current,
                        "class_type": nodes.get(current, ""),
                        "step": cur_val or None,
                        "total_steps": cur_max or None,
                        "nodes_done": len(done),
                        "nodes_total": eff,
                        "elapsed": round(now - started, 1),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[comfy] 进度回调失败: {exc}")

        try:
            import websockets

            if on_progress is not None:
                await emit(force=True)
            # ★ 必须和 /prompt 用同一个 clientId：ComfyUI 只把执行事件发给提交者
            # ★ 也跟着 self.host 走 —— 远程 ComfyUI 时 ws 也要连对方，否则拿不到进度
            url = f"ws://{self.host}:{self.port}/ws?clientId={client_id or uuid.uuid4().hex}"
            deadline = time.time() + max(60.0, self.gen_timeout)
            async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
                if ready is not None:
                    ready.set()
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    kind = msg.get("type")
                    data = msg.get("data") or {}
                    pid = str(data.get("prompt_id") or "")
                    expected = str((holder or {}).get("prompt_id") or "")
                    if expected and pid and pid != expected:
                        continue
                    if kind == "execution_cached":
                        cached |= {str(x) for x in (data.get("nodes") or [])}
                        await emit()
                    elif kind == "executing":
                        node = data.get("node")
                        if node is None:
                            await emit(force=True)
                            break
                        current = str(node)
                        cur_val, cur_max = 0, 0
                        if current in samplers and not sampler_hit:
                            sampler_hit = True
                            if on_sampler_start is not None:
                                try:
                                    await on_sampler_start()
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug(f"[comfy] 采样开始回调失败: {exc}")
                        await emit(force=True)
                    elif kind == "progress":
                        cur_val = int(data.get("value") or 0)
                        cur_max = int(data.get("max") or 0)
                        await emit()
                    elif kind == "executed":
                        done.add(str(data.get("node") or ""))
                        cur_val, cur_max = 0, 0
                        await emit()
                    elif kind == "execution_error":
                        await emit(force=True)
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[comfy] 进度监听失败（忽略）: {exc}")

    def _submit_sync(self, graph: dict, client_id: str = "") -> str:
        data = self._post_json_sync(
            "/prompt",
            {"prompt": graph, "client_id": client_id or uuid.uuid4().hex},
            timeout=max(30.0, self.gen_timeout),
        )
        if not isinstance(data, dict):
            raise ComfyError(f"ComfyUI /prompt 返回了奇怪的东西: {data!r}")
        node_errors = data.get("node_errors") or {}
        if node_errors:
            raise ComfyError(f"ComfyUI 拒绝了工作流：{_fmt_node_errors(node_errors)}")
        prompt_id = str(data.get("prompt_id") or "")
        if not prompt_id:
            raise ComfyError(f"ComfyUI 没返回 prompt_id：{json.dumps(data, ensure_ascii=False)[:400]}")
        return prompt_id

    async def _wait_history(self, prompt_id: str) -> dict:
        deadline = time.time() + self.gen_timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                # ★ 先看端口：手动重启/外部进程接管时，旧句柄已死但服务还活着，
                #   不能把上一张图的日志当成这一张的崩溃。
                stats = await asyncio.to_thread(self._health_sync, 1.0)
                if stats is not None:
                    logger.warning("[comfy] 旧进程句柄已退出但服务在线，接管后继续等待")
                    self._drop_stale_proc()
                else:
                    raise ComfyError(
                        f"出图过程中 ComfyUI 退出了（exit={_exit_text(self._proc.returncode)}）。"
                        f"日志尾部：{_log_tail(self.log_path) or '（空）'}"
                    )
            try:
                hist = await asyncio.to_thread(
                    self._get_json_sync, f"/history/{urllib.parse.quote(prompt_id)}", 10.0
                )
            except ComfyError as exc:
                # 服务刚起来时可能有短暂 404/连接问题，继续等
                logger.warning(f"[comfy] 轮询历史失败（继续等）: {exc}")
                hist = {}
            entry = (hist or {}).get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise ComfyError(f"ComfyUI 执行出错：{_fmt_status_messages(status)}")
                if status.get("completed") or entry.get("outputs"):
                    return entry.get("outputs") or {}
            await asyncio.sleep(0.6)
        raise ComfyError(
            f"等 ComfyUI 出图超过 {self.gen_timeout:.0f} 秒还没结果。"
            "（第一次跑大模型会慢一些；还不行就看 comfy_timeout 配置和显存）"
        )

    async def fetch_image(self, image: dict) -> bytes:
        """按 /view 取回图片字节。"""
        query = urllib.parse.urlencode(
            {
                "filename": image.get("filename", ""),
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
        )
        return await asyncio.to_thread(self._fetch_sync, query)

    async def upload_image(self, data: bytes, filename: str = "reference.png") -> str:
        """把参考图传进 ComfyUI 的 input 目录，返回 LoadImage 认的名字。

        ★ 走 /upload/image（multipart）而不是直接往磁盘写：这样插件拉起的
          服务和你自己开的服务都能用，也不用知道对方的 input 目录在哪。
        """
        await self.ensure_server()
        info = await asyncio.to_thread(self._upload_sync, data, filename)
        name = str(info.get("name") or "")
        if not name:
            raise ComfyError(f"上传参考图失败：{info!r}")
        sub = str(info.get("subfolder") or "")
        return f"{sub}/{name}" if sub else name

    def _upload_sync(self, data: bytes, filename: str) -> dict:
        boundary = "----comfyplugin" + uuid.uuid4().hex
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        tail = (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
            "true"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        body = head + data + tail
        req = urllib.request.Request(
            f"{self.base}/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise ComfyError(f"上传参考图失败：HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ComfyError(f"上传参考图失败: {exc}") from exc
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ComfyError(f"上传参考图返回的不是 JSON: {raw[:200]!r}") from exc

    def _fetch_sync(self, query: str) -> bytes:
        try:
            with urllib.request.urlopen(f"{self.base}/view?{query}", timeout=60) as r:
                data = r.read()
        except Exception as exc:  # noqa: BLE001
            raise ComfyError(f"取图失败: {exc}") from exc
        if not data:
            raise ComfyError("ComfyUI 返回了空图片")
        return data

    async def queue_status(self) -> dict:
        """队列里还有几个在跑（给状态工具用，连不上就返回空）。"""
        try:
            data = await asyncio.to_thread(self._get_json_sync, "/queue", 3.0)
        except Exception:  # noqa: BLE001
            return {"running": 0, "pending": 0}
        running = len(data.get("queue_running") or [])
        pending = len(data.get("queue_pending") or [])
        return {"running": running, "pending": pending}

    async def queue_position(self, prompt_id: str) -> int | None:
        """0 = 正在跑；N = 前面还有 N 个；None = 不在队列里。"""
        if not prompt_id:
            return None
        try:
            data = await asyncio.to_thread(self._get_json_sync, "/queue", 3.0)
        except Exception:  # noqa: BLE001
            return None
        for item in data.get("queue_running") or []:
            if len(item) > 1 and str(item[1]) == str(prompt_id):
                return 0
        for idx, item in enumerate(data.get("queue_pending") or []):
            if len(item) > 1 and str(item[1]) == str(prompt_id):
                return idx + 1
        return None

    async def free_models(self) -> bool:
        """让 ComfyUI 把模型卸掉（显存还回去），但进程留在端口上。

        ★ 对应 ComfyUI 的 POST /free：unload_models 卸载模型，free_memory 归还显存。
          和"关进程"分开是刻意的：卸完再出图不用重新冷启动（省 20~50 秒）。
        """
        try:
            await asyncio.to_thread(
                self._post_json_sync,
                "/free",
                {"unload_models": True, "free_memory": True},
                15.0,
            )
        except ComfyError as exc:
            logger.warning(f"[comfy] 卸载模型失败（忽略）: {exc}")
            return False
        self._model_loaded = False
        logger.info("[comfy] 已让 ComfyUI 卸载模型")
        return True

    # ------------------------------------------------------------------
    #  收尾
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """停掉【我们自己拉起来的】ComfyUI；别人开的不动。"""
        proc = self._proc
        if proc is not None and proc.poll() is None and self._owned:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[comfy] 停止 ComfyUI 失败（忽略）: {exc}")
        self._proc = None
        self._owned = False
        self._object_info = None

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def owned(self) -> bool:
        return self._owned and self.running()

    def model_loaded(self) -> bool:
        """显存里现在是不是还压着模型（给状态工具看）。"""
        return self._model_loaded


def _fmt_node_errors(node_errors: dict) -> str:
    parts = []
    for node_id, info in (node_errors or {}).items():
        if not isinstance(info, dict):
            parts.append(f"{node_id}: {info}")
            continue
        cls = info.get("class_type") or ""
        errs = info.get("errors") or []
        for e in errs:
            msg = e.get("message") or ""
            det = e.get("details") or ""
            parts.append(f"节点 {node_id}({cls}) {msg} {det}".strip())
    return "；".join(parts) or "（没有详细信息）"


def _fmt_status_messages(status: dict) -> str:
    parts = []
    for msg in status.get("messages") or []:
        if isinstance(msg, (list, tuple)) and len(msg) >= 2 and msg[0] == "execution_error":
            info = msg[1] or {}
            node = info.get("node_type") or info.get("node_id") or ""
            text = info.get("exception_message") or info.get("exception_type") or ""
            parts.append(f"{node}: {text}".strip(": "))
    return "；".join(parts) or "（ComfyUI 没给出原因，看日志）"
