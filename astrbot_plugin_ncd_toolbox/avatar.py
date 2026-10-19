# -*- coding: utf-8 -*-
"""头像读取：按需取某个 QQ / 某个群的公开头像，供模型"看"。

设计要点：
  · **不发送**：这里只负责把图抓到本地，谁看由调用方决定（插件把它塞进下一轮
    请求的 image_urls，主模型自己看；绝不往会话里发图）。
  · 数据源优先用 QQ 头像 CDN（不需要协议端支持、不挑 QQ 号是否存在）：
        用户  https://q1.qlogo.cn/g?b=qq&nk=<QQ号>&s=<尺寸>
        群    https://p.qlogo.cn/gh/<群号>/<群号>/<尺寸>
    失败时回一句人话，而不是抛 urllib 的原始异常。
  · 缓存：头像不常变，默认缓存 6 小时。命中缓存不联网 ——
    既省 CDN，也省掉"同一张图反复喂给模型"的钱。
  · 安全：只认 5~12 位数字的 QQ 号；只允许 qlogo 域名；超时 10 秒；上限 4 MB。
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SIZE = 640
MIN_SIZE, MAX_SIZE = 40, 640
CACHE_TTL = 6 * 3600
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 10.0
_ALLOWED_HOSTS = ("q1.qlogo.cn", "q2.qlogo.cn", "q3.qlogo.cn", "p.qlogo.cn", "qlogo.cn")
_QQ_RE = re.compile(r"^\d{5,12}$")


class AvatarError(Exception):
    """取头像失败（消息可以直接给模型/用户看）。"""


def is_qq(value: str) -> bool:
    return bool(_QQ_RE.match(str(value or "").strip()))


def build_url(qq: str, size: int = DEFAULT_SIZE) -> str:
    """用户头像地址。尺寸会被夹在 [40, 640]（CDN 只认这几档）。"""
    if not is_qq(qq):
        raise AvatarError(f"{qq!r} 不像 QQ 号（要 5~12 位数字）")
    s = max(MIN_SIZE, min(MAX_SIZE, int(size or DEFAULT_SIZE)))
    return f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s={s}"


def build_group_url(group_id: str, size: int = DEFAULT_SIZE) -> str:
    if not is_qq(group_id):
        raise AvatarError(f"{group_id!r} 不像群号（要 5~12 位数字）")
    s = max(MIN_SIZE, min(MAX_SIZE, int(size or DEFAULT_SIZE)))
    return f"https://p.qlogo.cn/gh/{group_id}/{group_id}/{s}"


def parse_target(who: str) -> tuple[str, str]:
    """把工具的 who 参数解析成 (类型, 号码)。

    类型：user / group。号码没解析出来就抛 AvatarError（带人话）。
    """
    text = str(who or "").strip().lower()
    if not text:
        return ("user", "")  # 留空 = 由调用方填默认（当前说话的人）
    if text in ("me", "self", "自己", "我"):
        return ("user", "@self")
    if text in ("sender", "说话的人", "对方"):
        return ("user", "@sender")
    if text in ("group", "群", "本群"):
        return ("group", "@group")
    if text.startswith("@"):
        text = text[1:].strip()
    if is_qq(text):
        return ("user", text)
    raise AvatarError(
        f"看不懂 who={who!r}。可用：留空（当前说话的人）、me、group、或 5~12 位 QQ 号。"
    )


def cache_path(cache_dir: pathlib.Path, kind: str, key: str) -> pathlib.Path:
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", key) or "x"
    return pathlib.Path(cache_dir) / f"{kind}_{safe}.png"


def _download(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise AvatarError(f"拒绝访问非 qlogo 地址：{url}")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (AstrBot ncd_toolbox avatar)",
            "Referer": "https://qzone.qq.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise AvatarError(f"头像服务器返回 {exc.code}（这个号可能没有公开头像）") from exc
    except urllib.error.URLError as exc:
        raise AvatarError(f"连不上头像服务器：{exc.reason}") from exc
    except TimeoutError as exc:
        raise AvatarError("取头像超时（10 秒）") from exc
    if len(data) > MAX_BYTES:
        raise AvatarError("头像文件超过 4 MB，已放弃")
    if not data.startswith(b"\x89PNG") and not data.startswith(b"\xff\xd8"):
        raise AvatarError("取回来的不是图片（可能是错误页）")
    return data


async def fetch_avatar(
    *,
    kind: str,
    key: str,
    size: int = DEFAULT_SIZE,
    cache_dir: pathlib.Path,
    ttl: float = CACHE_TTL,
) -> tuple[pathlib.Path, bool]:
    """取头像并缓存。返回 (本地路径, 是否命中缓存)。"""
    if kind not in ("user", "group"):
        raise AvatarError(f"不支持的取图类型：{kind}")
    target = cache_path(cache_dir, kind, key)
    if target.exists() and ttl > 0 and (time.time() - target.stat().st_mtime) < ttl:
        return target, True

    url = build_url(key, size) if kind == "user" else build_group_url(key, size)
    data = await asyncio.to_thread(_download, url)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target, False
