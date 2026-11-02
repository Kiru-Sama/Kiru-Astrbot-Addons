# -*- coding: utf-8 -*-
"""ComfyUI 连接配置（纯数据，不依赖 AstrBot）。

为什么要单独一个类（2026-11-02）：
  原来连接参数是在 main.py._client() 里由 8 个 _conf_* 调用现拼出来的，
  结果就是「想知道这个插件会连到哪台机器」得读三个文件；也没法在测试里
  构造一个指向假服务器的客户端。

现在：
  · 所有连接相关参数只在这一个 dataclass 里
  · from_conf() 负责把 AstrBot 的配置映射进来（只依赖 .get）
  · base_url / is_remote 是派生属性，不再各处重复判断
"""

from __future__ import annotations

from dataclasses import dataclass

LOCAL_HOSTS = ("", "127.0.0.1", "localhost", "::1", "0.0.0.0")


@dataclass(frozen=True)
class ConnectionSettings:
    """纯连接参数：任何 ComfyUI 客户端只需要这些。"""

    host: str = "127.0.0.1"
    port: int = 8199
    scheme: str = "http"
    auth_token: str = ""
    timeout: float = 10.0
    gen_timeout: float = 300.0

    @property
    def base_url(self) -> str:
        s = (self.scheme or "http").strip().lower()
        if s not in ("http", "https"):
            s = "http"
        return f"{s}://{self.host}:{self.port}"

    @property
    def is_remote(self) -> bool:
        return (self.host or "").strip().lower() not in LOCAL_HOSTS


@dataclass(frozen=True)
class ComfySettings:
    comfy_dir: str = ""
    python: str = ""
    scheme: str = "http"
    port: int = 8199
    host: str = "127.0.0.1"
    auth_token: str = ""
    launcher: str = ""
    unload_timeout: float = 300.0
    idle_timeout: float = 1800.0
    startup_timeout: float = 240.0
    gen_timeout: float = 300.0
    manage_process: bool = True
    extra_args: str = ""
    output_dir: str = ""
    temp_dir: str = ""

    @property
    def base_url(self) -> str:
        scheme = (self.scheme or "http").strip().lower()
        if scheme not in ("http", "https"):
            scheme = "http"
        return f"{scheme}://{self.host}:{self.port}"

    @property
    def connection(self) -> ConnectionSettings:
        """纯连接视图（通用客户端只依赖它）。"""
        return ConnectionSettings(
            host=self.host,
            port=self.port,
            scheme=self.scheme,
            auth_token=self.auth_token,
            gen_timeout=self.gen_timeout,
        )

    @property
    def is_remote(self) -> bool:
        """ComfyUI 是否在另一台机器上（手机端就是 True）。"""
        return (self.host or "").strip().lower() not in LOCAL_HOSTS

    @classmethod
    def from_conf(cls, conf) -> "ComfySettings":
        """从插件配置映射构造。

        conf 只需要支持 `.get(key, default)`（AstrBotConfig 本身是 dict 子类）。
        """

        def s(key: str, default: str = "") -> str:
            try:
                raw = conf.get(key, default)
            except TypeError:
                raw = conf.get(key)
            if raw is None:
                return default
            return str(raw).strip().strip('"').strip("'").strip() or default

        def i(key: str, default: int) -> int:
            raw = s(key, "")
            if not raw:
                return default
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return default

        scheme = s("comfy_scheme", "http").lower()
        return cls(
            comfy_dir=s("comfy_dir"),
            python=s("comfy_python"),
            scheme=scheme if scheme in ("http", "https") else "http",
            port=i("comfy_port", 8199),
            auth_token=s("comfy_token"),
            host=s("comfy_host") or "127.0.0.1",
            launcher=s("comfy_launcher"),
            unload_timeout=float(i("comfy_unload_timeout", 300)),
            idle_timeout=float(i("comfy_idle_timeout", 1800)),
            startup_timeout=float(i("comfy_startup_timeout", 240)),
            gen_timeout=float(i("comfy_timeout", 300)),
            manage_process=bool(conf.get("comfy_manage_process", True)),
            extra_args=s("comfy_extra_args"),
        )
