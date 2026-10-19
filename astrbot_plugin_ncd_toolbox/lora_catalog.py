# -*- coding: utf-8 -*-
"""LoRA 目录：扫描 / 解析 / 按名字解析成"要加载哪几个"。

命名约定（用户定的）：
    <SFW|NSFW>_<画师或画风>.safetensors
例如：
    SFW_cunnyfunky.safetensors
    SFW_guizhencao_chenbin.safetensors
    NSFW_St_Louis_Luxurious_Wheels_ILXL.safetensors

为什么用"扫描目录"而不是配置里手写一张表：
  · 用户往目录里丢一个新 LoRA 就自动可用，不用改配置、不用改代码 ——
    这和之前"菜单清单手写漏了 view_avatar"是同一类教训。
  · 评价前缀直接写进文件名，模型能据此判断该不该用（SFW/NSFW）。

只认带评价前缀的文件：目录里那些没前缀的（比如老的 SD1.5 LoRA）会被忽略，
不会被模型误选 —— 那是另一代底模的东西，挂上去只会报错。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

RATINGS = ("SFW", "NSFW")
DEFAULT_SCALE = 0.8
MIN_SCALE, MAX_SCALE = 0.0, 1.5
_NAME_RE = re.compile(r"^(SFW|NSFW)[_-](.+)\.(safetensors|pt|ckpt)$", re.IGNORECASE)


# 最近一次 scan() 里被跳过的文件及原因：[(文件名, 原因)]
# 给日志/面板排查用 —— 否则用户只会发现"某个画风不见了"，不知道为什么。
SKIPPED: list[tuple[str, str]] = []


class LoraError(Exception):
    """LoRA 相关的可预期失败（消息可以直接给模型看）。"""


@dataclass(frozen=True)
class Lora:
    name: str      # 去掉评价前缀和后缀的名字，例如 cunnyfunky
    path: str
    rating: str    # SFW / NSFW
    size_mb: float

    @property
    def label(self) -> str:
        return f"{self.name}（{self.rating}）"


def probe(path) -> tuple[str, str]:  # noqa: ANN001
    """读 safetensors 头，判断 (格式, 底模)。

    只读文件头（几 KB），不加载权重，扫描整个目录也很快。

    格式：lora / lokr / unknown
    底模：sdxl / sd15 / unknown
    """
    try:
        with open(path, "rb") as f:
            n = int.from_bytes(f.read(8), "little")
            if not (0 < n < 50 * 1024 * 1024):
                return ("unknown", "unknown")
            head = json.loads(f.read(n).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return ("unknown", "unknown")

    meta = head.get("__metadata__") or {}
    keys = [k for k in head if k != "__metadata__"]
    module = str(meta.get("ss_network_module") or "").lower()
    arch = str(meta.get("modelspec.architecture") or "").lower()
    base_ver = str(meta.get("ss_base_model_version") or "").lower()

    if module.startswith("lycoris") or any(".lokr_" in k for k in keys):
        fmt = "lokr"
    elif any(".lora_" in k for k in keys) or module == "networks.lora":
        fmt = "lora"
    else:
        fmt = "unknown"

    te_dual = any("lora_te1_" in k or "lora_te2_" in k for k in keys)
    te_single = any("lora_te_text_model" in k for k in keys)
    # ★ 底模判断要把几种写法都认出来：元数据里可能是
    #   "sdxl_base_v1-0" / "stable-diffusion-xl-v1-base/lora" / 模型名带 XL。
    #   只匹配 "sdxl" 会漏掉后者（实测漏过一次，导致 LoKr 的判断理由写成"认不出来"）。
    blob = " ".join((arch, base_ver, str(meta.get("ss_sd_model_name") or "").lower()))
    if "sdxl" in blob or "stable-diffusion-xl" in blob or "-xl" in blob or "xl_" in blob or te_dual:
        base = "sdxl"
    elif te_single and not te_dual:
        base = "sd15"
    else:
        base = "unknown"
    return (fmt, base)


def usable(path) -> tuple[bool, str]:  # noqa: ANN001
    """能不能在 SDXL diffusers 里用？返回 (能否, 说明)。"""
    fmt, base = probe(path)
    if fmt == "lokr":
        return (False, "LoKr 格式（LyCORIS），diffusers 不支持")
    if fmt == "unknown":
        return (False, "格式认不出来")
    if base == "sd15":
        return (False, "SD1.5 的 LoRA，SDXL 加载会报 shape mismatch")
    if base == "unknown":
        return (False, "看不出是给哪个底模训的")
    return (True, "ok")


def scan(directory) -> list[Lora]:  # noqa: ANN001
    """扫描目录，返回能用的 LoRA（只认带 SFW_/NSFW_ 前缀的）。"""
    root = Path(directory)
    SKIPPED.clear()
    if not root.is_dir():
        return []
    out: list[Lora] = []
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        m = _NAME_RE.match(p.name)
        if not m:
            continue  # 没评价前缀 → 不是给这套目录用的
        rating = m.group(1).upper()
        name = m.group(2).strip()
        if not name:
            continue
        ok, why = usable(p)
        if not ok:
            # 不放进目录 —— 但要留个记号，方便排查"为什么少了某个画风"
            SKIPPED.append((p.name, why))
            continue
        out.append(
            Lora(
                name=name,
                path=str(p),
                rating=rating,
                size_mb=round(p.stat().st_size / 1024 / 1024, 1),
            )
        )
    return out


def _visible(loras: list[Lora], allow_nsfw: bool) -> list[Lora]:
    return [lo for lo in loras if allow_nsfw or lo.rating == "SFW"]


def catalog_text(loras: list[Lora], allow_nsfw: bool = True) -> str:
    """给工具说明/菜单用的一段可用清单（分 SFW / NSFW 两组）。"""
    vis = _visible(loras, allow_nsfw)
    if not vis:
        return "（当前没有可用的画风 LoRA，只能用默认风格）"
    sfw = [lo for lo in vis if lo.rating == "SFW"]
    nsfw = [lo for lo in vis if lo.rating == "NSFW"]
    lines: list[str] = []
    if sfw:
        lines.append("SFW（正常场景用这些）：" + "、".join(lo.name for lo in sfw))
    if nsfw:
        lines.append(
            "NSFW（只在用户明确要露骨内容时用）：" + "、".join(lo.name for lo in nsfw)
        )
    return "\n".join(lines)


def _match_one(token: str, loras: list[Lora]) -> Lora:
    """把一个名字匹配到一个 LoRA。宽容一点：大小写无关、允许子串。"""
    low = token.strip().lower()
    if not low:
        raise LoraError("画风名是空的")
    exact = [lo for lo in loras if lo.name.lower() == low]
    if exact:
        return exact[0]
    # ★ 单个字符只允许精确匹配：否则 'a' 这种会命中一大堆名字里带 a 的。
    #   ★ 2026-10-19 从 3 放宽到 2：中文名两个字就够表意了（「像素」「3D」），
    #     卡 3 个字会逼用户写全名；两个字的歧义由下面的"最短优先/并列报错"兜住。
    if len(low) < 2:
        raise LoraError(
            f"没有叫「{token}」的画风（名字太短，请写完整）。可用的有："
            + "、".join(lo.name for lo in loras)
        )
    partial = [lo for lo in loras if low in lo.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        # 取名字最短的那个（最贴近用户写的），并列就报歧义
        partial.sort(key=lambda lo: len(lo.name))
        if len(partial[0].name) == len(partial[1].name):
            raise LoraError(
                f"「{token}」能对上好几个画风：{', '.join(lo.name for lo in partial)}，"
                "请写完整一点"
            )
        return partial[0]
    raise LoraError(
        f"没有叫「{token}」的画风。可用的有："
        + "、".join(lo.name for lo in loras)
    )


def resolve(
    want: str,
    loras: list[Lora],
    *,
    allow_nsfw: bool = True,
    default_scale: float = DEFAULT_SCALE,
) -> list[dict]:
    """把 "cunnyfunky:0.7, guizhencao" 解析成要加载的列表。

    · 留空 → []（不挂 LoRA）
    · 逗号分隔可叠加（最多 3 个，再多互相打架、还吃显存）
    · 每个可以用 `名字:强度` 单独指定强度，默认 0.8
    · 名字宽容匹配；NSFW 在 allow_nsfw=False 时不能选
    """
    text = str(want or "").strip()
    if not text or text.lower() in ("none", "off", "无", "不用"):
        return []

    vis = _visible(loras, allow_nsfw)
    if not vis:
        raise LoraError("当前没有可用的画风 LoRA")

    picked: list[dict] = []
    seen: set[str] = set()
    for raw in re.split(r"[,，、]", text):
        token = raw.strip()
        if not token:
            continue
        scale = default_scale
        if ":" in token:
            token, _, w = token.partition(":")
            try:
                scale = float(w)
            except ValueError as exc:
                raise LoraError(f"强度写错了：{raw!r}（应该像 cunnyfunky:0.7）") from exc
            scale = max(MIN_SCALE, min(MAX_SCALE, scale))
            if scale <= 0:
                continue  # 0 = 不用这个
        lo = _match_one(token, vis)
        if lo.name in seen:
            continue
        seen.add(lo.name)
        picked.append({"name": lo.name, "path": lo.path, "scale": round(scale, 3)})

    if len(picked) > 3:
        raise LoraError(f"一次最多叠 3 个画风，你给了 {len(picked)} 个")
    return picked


def summarize(picked: list[dict]) -> str:
    """给日志/返回用的一句话。"""
    if not picked:
        return "不挂 LoRA"
    return " + ".join(f"{p['name']}@{p['scale']}" for p in picked)
