# -*- coding: utf-8 -*-
"""独立的 SD 后端客户端（SDXL，不依赖任何外部服务）。

职责：
  · 第一次要出图时按需拉起 sd_worker.py（子进程，用独立 venv 里的 python）
  · 之后复用同一个 worker
  · worker 自己空闲超时退出 —— 我们不主动杀它，也不信它一定还活着
    （每次都先 /health 探一下，挂了就重新拉起）

★ 用哪个 python：
  独立的 `SD-standalone\venv`（torch + diffusers + transformers + accelerate +
  safetensors + peft）。**不借用 ComfyUI 的 python**，也不读写它的任何目录 ——
  所以 ComfyUI 挪位置、删掉、升级，都跟这里无关。

★ 为什么不走 ComfyUI 的 HTTP：
  那样仍然依赖 ComfyUI 这个服务（端口、自定义节点、被人在界面里改坏的风险）。
  这个 worker 只有一条文生图流程，参数全在代码里，改不动也污染不了。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from astrbot.api import logger

PLUGIN_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = PLUGIN_DIR / "sd_worker.py"


class SDBackendError(Exception):
    """本地 SD 后端的可预期失败。"""


class StandaloneClient:
    def __init__(
        self,
        *,
        python: str,
        checkpoint: str,
        port: int = 8199,
        idle_timeout: float = 300.0,
        startup_timeout: float = 180.0,
        gen_timeout: float = 70.0,
        lora: str = "",
        lora_scale: float = 0.8,
        device: str = "cuda",
        arch: str = "auto",
        offload: str = "auto",
        sampler: str = "euler",
        max_side: int = 1920,
        log_path: Path | None = None,
        worker_script: Path | None = None,
    ) -> None:
        self.python = str(python or "").strip()
        self.checkpoint = str(checkpoint or "").strip()
        # LoRA 可选：留空就是纯底模。★ 只在 worker 启动时加载一次，
        # 换强度不需要重启（推理时按 scale 缩放）。
        self.lora = str(lora or "").strip()
        self.lora_scale = float(lora_scale or 0.8)
        # cuda / cpu。纯 CPU 不吃显存，但会慢一个数量级。
        dev = str(device or "cuda").strip().lower()
        if dev not in ("cuda", "cpu"):
            logger.warning(f"[toolbox] sd_device={dev!r} 不认识，按 cuda 处理")
            dev = "cuda"
        self.device = dev
        # auto / sd15 / sdxl —— 交给 worker 判断，这里只透传
        self.arch = str(arch or "auto").strip().lower()
        # auto / on / off —— CPU offload（SDXL 默认开，显存省一半多）
        self.offload = str(offload or "auto").strip().lower()
        # euler / dpmpp2m_karras —— 采样器（worker 启动时用，之后可按请求换）
        self.sampler = str(sampler or "euler").strip().lower()
        # 单边像素上限（插件配置 sd_max_side，默认 1920）
        self.max_side = int(max_side or 1920)
        self.port = int(port or 8199)
        self.idle_timeout = float(idle_timeout or 300.0)
        self.startup_timeout = float(startup_timeout or 180.0)
        # 单次出图的 HTTP 超时。★ 和插件里 image_timeout 用同一个数：
        # 外层 asyncio.wait_for 负责给出好看的错误消息，这里负责真的把
        # 阻塞线程放掉（urlopen 不能取消，只能靠它自己的 timeout）。
        self.gen_timeout = float(gen_timeout or 70.0)
        # ★ 可覆盖：测试要用桩 worker；线上就是插件目录里那份
        self.worker_script = Path(worker_script) if worker_script else WORKER_SCRIPT
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        self._proc: subprocess.Popen | None = None
        self._lock = asyncio.Lock()

    # ---------- 进程管理 ----------
    def _log_tail(self, limit: int = 600) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
        return text[-limit:].strip()

    def _health_sync(self, timeout: float = 2.0) -> dict | None:
        """/health 探活；返回 None 表示没人在听。"""
        try:
            with urllib.request.urlopen(f"{self.base}/health", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        # ★ 必须是【我们这个 worker】。同一个端口可能被别的程序占着，
        #   盲目发请求过去后果不可预期。
        if not isinstance(data, dict) or "checkpoint" not in data or not data.get("ok"):
            return None
        return data

    async def ensure_worker(self, checkpoint: str | None = None) -> None:
        """确保 worker 活着。已就绪就直接返回。

        ★ checkpoint 可选：只在【需要新拉起 worker】时用得上 —— 让 worker 直接用
          目标底模启动，省掉"先加载默认模型、再换成目标模型"的第二次 16 秒。
        """
        async with self._lock:
            if not self.python:
                raise SDBackendError(
                    "没配 sd_python。填一个【装了 torch 的 python】路径，"
                    "例如 <独立环境>\\Scripts\\python.exe"
                )
            if not os.path.exists(self.python):
                raise SDBackendError(f"sd_python 不存在: {self.python}")
            # ★ 切换底模时以目标为准：不能让"默认模型路径配错了"把换模型也一起挡死
            want = str(checkpoint or self.checkpoint or "").strip()
            if not want:
                raise SDBackendError("没配 sd_checkpoint（要用的 .safetensors 路径）")
            if not os.path.exists(want):
                raise SDBackendError(f"底模不存在: {want}")

            if await asyncio.to_thread(self._health_sync, 1.5) is not None:
                return

            # 端口上有人但不像我们的 worker → 明确报错，别乱发请求
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=1.5) as r:
                    raw = r.read().decode("utf-8", "replace")[:200]
                raise SDBackendError(
                    f"端口 {self.port} 被别的服务占着（返回 {raw!r}），换一个 sd_port"
                )
            except urllib.error.HTTPError:
                raise SDBackendError(
                    f"端口 {self.port} 上有个不认识的 HTTP 服务，换一个 sd_port"
                ) from None
            except SDBackendError:
                raise
            except Exception:  # noqa: BLE001
                pass  # 真的没人听 → 继续拉起

            self._spawn(checkpoint)
            await self._wait_ready()

    def _spawn(self, checkpoint: str | None = None) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        cmd = [
            self.python,
            str(self.worker_script),
            "--checkpoint",
            str(checkpoint or self.checkpoint),
            "--port",
            str(self.port),
            "--idle-timeout",
            str(self.idle_timeout),
            "--device",
            self.device,
            "--arch",
            self.arch,
            "--offload",
            self.offload,
            "--sampler",
            self.sampler,
            "--max-side",
            str(self.max_side),
        ]
        if self.lora:
            cmd += ["--lora", self.lora, "--lora-scale", str(self.lora_scale)]
        logger.info(f"[toolbox] 拉起本地 SD worker: {' '.join(cmd)}")
        try:
            if self.log_path:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                out = open(self.log_path, "ab")
            else:
                out = subprocess.DEVNULL
            self._proc = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=str(PLUGIN_DIR),
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            raise SDBackendError(f"拉起 worker 失败: {exc}") from exc

    async def _wait_ready(self) -> None:
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                tail = await asyncio.to_thread(self._log_tail)
                raise SDBackendError(
                    f"worker 启动就退出了（exit={self._proc.returncode}）。"
                    f"日志尾部：{tail or '（空）'}"
                )
            # ★ 端口通了 ≠ 模型加载完了。worker 现在先把 HTTP 服务起起来、
    #   再在后台线程里加载模型，所以这里必须等 health.ready 为真；
    #   加载期间把阶段记到 self.load_phase，方便上层显示"加载中 70%"。
            health = await asyncio.to_thread(self._health_sync, 1.5)
            if health is not None:
                if health.get("ready", True):
                    logger.info(f"[toolbox] 本地 SD worker 就绪（端口 {self.port}）")
                    return
                self.load_phase = str(health.get("phase") or "")
                self.load_pct = int(health.get("phase_pct") or 0)
            await asyncio.sleep(0.8)
        raise SDBackendError(
            f"等 worker 就绪超过 {self.startup_timeout:.0f} 秒。"
            f"日志尾部：{await asyncio.to_thread(self._log_tail) or '（空）'}"
        )

    # ---------- 生成 ----------
    def _post_sync(self, payload: dict, endpoint: str = "/txt2img") -> tuple[bytes, int]:
        req = urllib.request.Request(
            f"{self.base}{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.gen_timeout) as r:
                png = r.read()
                seed = int(r.headers.get("X-Seed") or 0)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            raise SDBackendError(f"worker 返回 HTTP {exc.code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001
            raise SDBackendError(f"请求 worker 失败: {exc}") from exc
        if not png:
            raise SDBackendError("worker 返回了空图片")
        return png, seed

    async def txt2img(self, payload: dict) -> tuple[bytes, int]:
        await self.ensure_worker()
        return await asyncio.to_thread(self._post_sync, payload)

    async def img2img(self, payload: dict) -> tuple[bytes, int]:
        """参考图生图。payload 里 `image` 是 base64（可带 data URL 前缀）。

        ★ 必须等 worker 起来：img2img 管道是按 text2img 的权重 from_pipe 建出来的。
        """
        await self.ensure_worker()
        return await asyncio.to_thread(self._post_sync, payload, "/img2img")

    # ---------- 换底模 ----------
    def _load_sync(self, checkpoint: str) -> dict:
        """让 worker 换底模（阻塞版）。返回 worker 的 JSON（含 seconds）。"""
        body = json.dumps({"checkpoint": str(checkpoint)}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/load",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.gen_timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise SDBackendError(f"换模型失败（HTTP {exc.code}）: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise SDBackendError(f"请求 worker 换模型失败: {exc}") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            raise SDBackendError(
                "换模型失败: " + str((data or {}).get("error") or "worker 没返回 ok")
            )
        return data

    async def switch_model(self, checkpoint: str) -> dict:
        """换底模（会让 worker 真的加载权重，约十几秒）。

        ★ 已经就是这个模型 = 立即返回（worker 里会判，不会白加载一次）。
        ★ worker 没起来时直接用【目标】底模拉起，只加载一次 —— 不然先按配置
          加载默认模型、再换过去，等于白等两个 16 秒。
        """
        target = os.path.abspath(str(checkpoint or "").strip())
        if not target:
            raise SDBackendError("没给底模路径")
        if not os.path.exists(target):
            raise SDBackendError(f"底模不存在: {target}")
        await self.ensure_worker(target)
        health = await asyncio.to_thread(self._health_sync, 2.0)
        if isinstance(health, dict):
            cur = str(health.get("checkpoint") or "")
            if cur and os.path.abspath(cur) == target and health.get("ready"):
                return {
                    "ok": True,
                    "checkpoint": cur,
                    "name": os.path.basename(cur),
                    "switched": False,
                    "seconds": 0.0,
                }
        return await asyncio.to_thread(self._load_sync, target)

    def _progress_sync(self) -> dict:
        """读 worker 的出图进度（阻塞版）。拿不到就返回"没在跑"。"""
        try:
            with urllib.request.urlopen(f"{self.base}/progress", timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("ok"):
                return data
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "busy": False, "step": 0, "total": 0, "elapsed": 0.0}

    async def progress(self) -> dict:
        """给生图页面轮询用的进度。

        ★ 不要用 self.running() 当门卫：worker 可能是【上一次插件实例】或者
          用户手动拉起来的（那时 self._proc 是 None），但它照样在出图。
          直接问一句就行，问不到 _progress_sync 自己会退回"没在跑"。
        """
        return await asyncio.to_thread(self._progress_sync)

    def stop(self) -> None:
        """卸载插件时调用 —— 主动收掉自己拉起的 worker，别让它多挂 5 分钟。"""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None

    def running(self) -> bool:
        """worker 现在是否活着（给生图页面显示状态用）。"""
        return self._proc is not None and self._proc.poll() is None
