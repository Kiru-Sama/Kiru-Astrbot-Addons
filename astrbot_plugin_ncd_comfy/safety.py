# -*- coding: utf-8 -*-
"""comfy 插件的安全护栏（纯函数，不依赖 AstrBot 运行时）。

单独放一个模块的目的：能被离线测试 import 并断言行为，
而不是像以前那样只断言「源码里有没有这个字符串」。

提供三件事：
  · path_allowed(p, roots)        路径必须落在允许的根目录内（防 ../ 与符号链接逃逸）
  · public_url_error(url)         拒环回/内网/链路本地/保留地址（SSRF 兜底）
  · prune_files(folder, ...)      历史图裁剪：mtime 二次校验 + 单次删除上限
"""

from __future__ import annotations

import ipaddress
import urllib.request
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def path_allowed(p, roots) -> bool:
    """realpath 后必须落在 roots 之一内。"""
    try:
        rp = os.path.realpath(str(p))
    except Exception:  # noqa: BLE001
        return False
    for root in roots or []:
        try:
            rr = os.path.realpath(str(root))
        except Exception:  # noqa: BLE001
            continue
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def public_url_error(url: str) -> str:
    """返回空串表示可以访问；否则返回拒绝原因。"""
    try:
        host = (urlparse(str(url)).hostname or "").strip()
    except Exception:  # noqa: BLE001
        return "地址解析失败"
    if not host:
        return "地址为空"
    candidates = [host]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            candidates = [socket.gethostbyname(host)]
        except Exception:  # noqa: BLE001
            return "域名解析失败"
    for cand in candidates:
        try:
            ip = ipaddress.ip_address(cand)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return "只允许公网地址（拒绝内网/本机）"
    return ""


def prune_files(
    folder,
    keep_names,
    cutoff: float,
    skip_names=(),
    max_delete: int = 20,
) -> tuple[int, int]:
    """删掉 folder 里不再保留的图；返回 (已删数量, 超过上限未删的数量)。

    保护（P1-6）：
      · 文件 mtime 也必须早于 cutoff（不只信索引里的 ts）
      · 单次最多删 max_delete 个，超出的留给下次/人工确认
    """
    removed = 0
    overflow = 0
    skip = set(skip_names or ())
    try:
        for fp in Path(folder).iterdir():
            if not fp.is_file() or fp.name in skip:
                continue
            if fp.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if fp.name in (keep_names or set()):
                continue
            try:
                if fp.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if removed >= max_delete:
                overflow += 1
                continue
            try:
                fp.unlink()
                removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed, overflow


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝跟随 3xx（SSRF 校验只对首跳有效）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_public(url: str, timeout: float = 15.0, max_bytes: int = 12 * 1024 * 1024) -> bytes:
    """下载公网 URL：先校验地址，再禁重定向读取。"""
    err = public_url_error(url)
    if err:
        raise ValueError(err)
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (AstrBot ncd_comfy)"}
    )
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
        return resp.read(max_bytes)
