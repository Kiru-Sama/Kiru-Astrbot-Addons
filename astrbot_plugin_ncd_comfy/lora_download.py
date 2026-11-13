# -*- coding: utf-8 -*-
"""LoRA 搜索与下载（civarchive + HuggingFace 镜像）。

设计原则：
  · 只搜索 / 下载 LoRA（type=LORA，下载后还要校验张量含 lora_）
  · 搜索结果只展示，不触发下载；必须用户选择后由工具传 sha256 下载
  · 下载目录：<Mods In It>/loras/bot下载/<基座>/<种类>/
  · 配额：bot下载 目录 > 8GB 拒绝；F 盘剩余空间 < 2GB 拒绝
  · 不改名、不审 NSFW

依赖：仅标准库（urllib / json / re / struct / concurrent.futures）
"""

from __future__ import annotations

import json
import os
import re
import struct
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

CIVARCHIVE = "https://civarchive.com"
HF_HOSTS = ("hf-mirror.com", "huggingface.co")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
BOT_LORA_SUBDIR = "bot下载"
MAX_BOT_LORA_BYTES = 8 * 1024**3        # bot下载 目录上限 8GB
MIN_FREE_BYTES = 2 * 1024**3            # F 盘剩余空间下限 2GB
MAX_LORA_BYTES = 2 * 1024**3            # 单个 LoRA 上限 2GB
HTTP_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 600

# 搜索排序 → civarchive sort 参数
SORTS = {
    "hot": "Most Downloaded",
    "top": "Most Downloaded",
    "new": "Newest",
    "newest": "Newest",
    "liked": "Most Liked",
    "likes": "Most Liked",
}


@dataclass
class LoraResult:
    name: str
    base_model: str
    published_at: float          # unix 秒
    download_count: int
    url: str                     # civarchive 版本页（相对）
    sha256: str = ""             # 版本页解析出的文件 hash
    size_bytes: int = 0          # 文件大小（0 = 未知）
    mirror: str = ""             # HF 镜像直链（原始域名）
    filename: str = ""
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["published_text"] = fmt_time(self.published_at)
        d["age_days"] = age_days(self.published_at)
        d["size_text"] = fmt_size(self.size_bytes)
        d["link"] = CIVARCHIVE + self.url if self.url.startswith("/") else self.url
        return d


def fmt_size(n: int | float) -> str:
    n = float(n or 0)
    if n <= 0:
        return "0 B"
    if n >= 1024**3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def fmt_time(ts: float) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


def age_days(ts: float) -> int:
    if not ts:
        return -1
    return max(0, int((time.time() - float(ts)) // 86400))


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _http_get(url: str, timeout: int = HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_head(url: str, timeout: int = HTTP_TIMEOUT) -> dict[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return {k.lower(): v for k, v in r.headers.items()}


def _next_data(html: str) -> dict | None:
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


def search_loras(
    query: str,
    sort: str = "hot",
    period: str = "all",
    base_model: str = "",
    limit: int = 5,
) -> list[LoraResult]:
    """civarchive 搜索 LoRA，返回前 limit 条（含 sha256/大小/镜像，尽量并发补齐）。"""
    sort_val = SORTS.get(str(sort or "").strip().lower(), "Most Downloaded")
    params = {
        "q": str(query or "").strip(),
        "type": "LORA",
        "sort": sort_val,
        "period": str(period or "all"),
        "rating": "all",
    }
    if base_model:
        params["base_model"] = str(base_model)
    url = CIVARCHIVE + "/search?" + urllib.parse.urlencode(params)
    html = _http_get(url).decode("utf-8", "replace")
    data = _next_data(html) or {}
    results = (((data.get("props") or {}).get("pageProps") or {}).get("data") or {}).get(
        "results"
    ) or []

    out: list[LoraResult] = []
    seen = set()
    for r in results:
        if not isinstance(r, dict):
            continue
        if str(r.get("type") or "").upper() != "LORA":
            continue
        key = (r.get("name"), r.get("model_id"), r.get("version_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            LoraResult(
                name=str(r.get("name") or ""),
                base_model=str(r.get("base_model") or ""),
                published_at=float(r.get("created_at") or 0),
                download_count=int(r.get("download_count") or 0),
                url=str(r.get("url") or ""),
                tags=[str(t) for t in (r.get("tags") or [])][:12],
            )
        )
        if len(out) >= max(1, min(int(limit), 20)):
            break

    # 并发补详情（sha256 / 大小 / 镜像）
    if out:
        with ThreadPoolExecutor(max_workers=min(5, len(out))) as pool:
            list(pool.map(_enrich, out))
    return out


def _enrich(item: LoraResult) -> None:
    """打开版本页，补 sha256、大小、HF 镜像、真实文件名。"""
    if not item.url:
        return
    try:
        page = _http_get(CIVARCHIVE + item.url).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return
    # sha256
    m = re.search(r"/sha256/([0-9a-f]{64})", page)
    if m:
        item.sha256 = m.group(1)
    # HF 文件直链：只认 /resolve/main/ 的文件链接（排除 spaces 页面链接）
    mirrors = sorted(
        set(
            re.findall(
                r"https://huggingface\.co/[^\"\s<>]+/resolve/main/[^\"\s<>]+", page
            )
        )
    )
    if mirrors:
        item.mirror = mirrors[0]
        item.filename = urllib.parse.unquote(mirrors[0].rsplit("/", 1)[-1])
    # 文件大小：civarchive 的 HTML 结构是 Size:</div><div>87.60 MB</div>
    sm = re.search(
        r"Size:[^0-9]{0,120}([0-9.]+)\s*(KB|MB|GB|TB)", page
    )
    if sm:
        try:
            val = float(sm.group(1))
            unit = sm.group(2).upper()
            mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
            item.size_bytes = int(val * mult)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------


def _hf_urls(mirror: str) -> list[str]:
    """HF 直链 → 镜像站 URL 列表（hf-mirror 优先）。"""
    if not mirror:
        return []
    path = mirror.split("huggingface.co/", 1)[-1]
    return [f"https://{host}/{path}" for host in HF_HOSTS]


def _read_safetensors_header(path: str) -> dict | None:
    try:
        with open(path, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if n <= 0 or n > 200 * 1024 * 1024:
                return None
            head = json.loads(f.read(n).decode("utf-8"))
        return head if isinstance(head, dict) else None
    except Exception:  # noqa: BLE001
        return None


def is_lora_safetensors(path: str) -> bool:
    """校验：safetensors 合法，且张量含 lora_ 前缀（kohya / 新格式都算）。"""
    head = _read_safetensors_header(path)
    if not head:
        return False
    keys = [k for k in head if k != "__metadata__"]
    if not keys:
        return False
    lora_like = 0
    for k in keys[:4000]:
        low = k.lower()
        if "lora_" in low or "lora." in low or ".lora_a" in low or ".lora_b" in low:
            lora_like += 1
    return lora_like > 0


def classify_lora(name: str, tags: list[str], base_model: str) -> tuple[str, str]:
    """返回 (基座目录, 种类目录)。"""
    text = (name + " " + " ".join(tags or [])).lower()
    base_map = [
        (("moss",), "MOSS"),
        (("anima", "anima-preview"), "anima"),
        (("krea2", "krea"), "Krea2"),
        (("illustrious", "ilxl", "noobai", "pony", "sdxl"), "sdxl"),
        (("qwen",), "Qwen"),
        (("wan", "wan2.2"), "WAN2.2"),
        (("flux", "flux-2", "f2k"), "flux-2-klein"),
        (("zimage", "z-image"), "Zimage"),
        (("minimax", "h3"), "minimax_h3"),
        (("sd15", "sd1.5"), "sd15"),
    ]
    base = ""
    blob = (str(base_model) + " " + text).lower()
    for kws, folder in base_map:
        if any(k in blob for k in kws):
            base = folder
            break
    if not base:
        base = "未分类"

    style_kw = ("style", "artist", "painting", "illust", "anime", "lineart", "watercolor", "comic", "pixel")
    tool_kw = ("turbo", "detail", "lighting", "brightness", "contrast", "saturation", "skin", "context", "upscale", "fix", "repair", "helper", "slider", "steps", "noise")
    real_kw = ("real", "realistic", "photo", "photoreal")
    char_kw = ("character", "girl", "boy", "waifu", "oc", "person", "人物", "角色")
    if any(k in text for k in tool_kw):
        kind = "工具"
    elif any(k in text for k in real_kw):
        kind = "写实"
    elif any(k in text for k in style_kw):
        kind = "画风"
    elif any(k in text for k in char_kw):
        kind = "角色"
    else:
        kind = "未分类"
    return base, kind


def bot_lora_root(mods_in_it: str) -> str:
    return os.path.join(mods_in_it, "loras", BOT_LORA_SUBDIR)


def dir_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def disk_free(path: str) -> int:
    try:
        usage = __import__("shutil").disk_usage(path)
        return usage.free
    except Exception:  # noqa: BLE001
        return 0


def quota_check(mods_in_it: str, need_bytes: int = 0) -> tuple[bool, str]:
    """检查 bot下载 目录上限 + 磁盘空间。返回 (ok, 原因)。"""
    root = bot_lora_root(mods_in_it)
    used = dir_size(root)
    if used + max(0, need_bytes) > MAX_BOT_LORA_BYTES:
        return (
            False,
            f"bot下载 目录已用 {fmt_size(used)}，上限 {fmt_size(MAX_BOT_LORA_BYTES)}；"
            "等管理员清理后再下",
        )
    drive = os.path.splitdrive(os.path.abspath(mods_in_it))[0] or "F:"
    free = disk_free(mods_in_it)
    if free and free < max(MIN_FREE_BYTES, need_bytes + 512 * 1024**2):
        return (
            False,
            f"{drive} 盘剩余 {fmt_size(free)}，低于安全线；先清理磁盘再下",
        )
    return True, ""


def download_lora(
    result: LoraResult,
    mods_in_it: str,
    on_progress=None,
) -> dict[str, Any]:
    """按搜索结果下载 LoRA 到 bot下载/<基座>/<种类>/。

    返回 {"ok": bool, "path": str, "message": str}
    """
    if not result.sha256 or not result.mirror:
        return {"ok": False, "path": "", "message": "这个模型没有可用的 HF 镜像，下载不了"}
    if result.size_bytes and result.size_bytes > MAX_LORA_BYTES:
        return {
            "ok": False,
            "path": "",
            "message": f"文件 {fmt_size(result.size_bytes)} 超过 LoRA 上限 {fmt_size(MAX_LORA_BYTES)}，拒绝",
        }

    ok, why = quota_check(mods_in_it, result.size_bytes)
    if not ok:
        return {"ok": False, "path": "", "message": why}

    # 校验是 LoRA（先看文件名/标签太冒险，下载后校验）
    base_dir, kind_dir = classify_lora(result.name, result.tags, result.base_model)
    dest_dir = os.path.join(bot_lora_root(mods_in_it), base_dir, kind_dir)
    os.makedirs(dest_dir, exist_ok=True)

    filename = result.filename or (
        re.sub(r'[\\/:*?"<>|]', "_", result.name) + ".safetensors"
    )
    dest = os.path.join(dest_dir, filename)
    part = dest + ".part"

    if os.path.isfile(dest):
        return {"ok": True, "path": dest, "message": f"已存在，跳过下载：{filename}"}

    last_err = ""
    for url in _hf_urls(result.mirror):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if on_progress:
                        try:
                            on_progress(got, total)
                        except Exception:  # noqa: BLE001
                            pass
            if not is_lora_safetensors(part):
                os.remove(part)
                return {
                    "ok": False,
                    "path": "",
                    "message": "下载的文件不是 LoRA（校验失败），已删除",
                }
            os.replace(part, dest)
            return {
                "ok": True,
                "path": dest,
                "message": f"已下载并归档：{os.path.relpath(dest, mods_in_it)}",
            }
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            try:
                if os.path.isfile(part):
                    os.remove(part)
            except OSError:
                pass

    return {"ok": False, "path": "", "message": f"下载失败：{last_err}"}


def status(mods_in_it: str) -> dict[str, Any]:
    """配额状态（给面板/工具用）。"""
    root = bot_lora_root(mods_in_it)
    used = dir_size(root)
    free = disk_free(mods_in_it)
    cnt = 0
    for _root, _dirs, files in os.walk(root):
        cnt += sum(1 for f in files if f.endswith(".safetensors"))
    return {
        "bot_dir": root,
        "used_bytes": used,
        "used_text": fmt_size(used),
        "limit_bytes": MAX_BOT_LORA_BYTES,
        "limit_text": fmt_size(MAX_BOT_LORA_BYTES),
        "free_bytes": free,
        "free_text": fmt_size(free),
        "count": cnt,
        "over_limit": used > MAX_BOT_LORA_BYTES,
        "free_low": bool(free and free < MIN_FREE_BYTES),
        "max_lora_bytes": MAX_LORA_BYTES,
    }
