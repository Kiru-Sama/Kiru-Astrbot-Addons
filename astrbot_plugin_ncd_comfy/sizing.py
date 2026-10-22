# -*- coding: utf-8 -*-
"""画幅解析与硬上限（和工作流无关，纯算术）。

从原来那份"内置工作流"里拆出来的 —— 现在工作流由你的预设文件决定，
但"多大算太大"这件事仍然要在这里卡住：12 GB 卡超过 1920×1440 就会爆显存。
"""

from __future__ import annotations

import random
import re

SIZE_MIN_SIDE = 256

# 默认上限：单边 1920、总面积 1920×1440 = 2.7648 MP
DEFAULT_MAX_SIDE = 1920
DEFAULT_MAX_PIXELS = 1920 * 1440

#: 提醒线比例：单边超过上限的 75%（默认 1440）就先问一句，再大才拒绝
WARN_RATIO = 0.75


def warn_side(max_side: int) -> int:
    """警告线：单边超过它就先返回提醒，用户同意后带 force=true 再调。"""
    return max(SIZE_MIN_SIDE, int(int(max_side) * WARN_RATIO))

SIZE_PRESETS = {
    "竖": (720, 960),
    "竖版": (720, 960),
    "portrait": (720, 960),
    "横": (960, 720),
    "横版": (960, 720),
    "landscape": (960, 720),
    "方": (768, 768),
    "方形": (768, 768),
    "square": (768, 768),
}


def check_canvas(width: int, height: int, max_side: int, max_pixels: int) -> None:
    """画幅硬约束：单边 256~max_side，总面积 ≤ max_pixels。不合法抛 ValueError。"""
    w, h = int(width), int(height)
    if not (SIZE_MIN_SIDE <= w <= max_side and SIZE_MIN_SIDE <= h <= max_side):
        raise ValueError(
            f"画幅 {w}×{h} 超出范围：任一边都要在 {SIZE_MIN_SIDE}~{max_side} 之间"
        )
    if w * h > max_pixels:
        raise ValueError(
            f"画幅 {w}×{h} 面积太大（{w * h / 1e6:.2f} MP）：上限 {max_pixels / 1e6:.2f} MP。"
            f"换成 720x960 / 960x720 这类比例。"
        )


def parse_size(text: str, max_side: int, max_pixels: int) -> tuple[int, int] | None:
    """把 "720x960" / "竖" 解析成 (宽, 高)；空串返回 None，解析不出抛 ValueError。"""
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
        # ★ latent 节点要求 8 的倍数：向下取整，别四舍五入往上顶破上限
        return (w // 8 * 8, h // 8 * 8)
    raise ValueError(
        f"看不懂的画幅「{raw}」。写 720x960 这种（宽x高），或者直接写 竖 / 横 / 方。"
    )


def resolve_size(
    size: str,
    width: int,
    height: int,
    default: tuple[int, int],
    max_side: int,
    max_pixels: int,
) -> tuple[int, int]:
    """优先级：width/height 像素 > size 字符串 > 预设默认。"""
    base_w, base_h = default
    if width or height:
        w = int(width or base_w)
        h = int(height or base_h)
        check_canvas(w, h, max_side, max_pixels)
        return (w // 8 * 8, h // 8 * 8)
    parsed = parse_size(size, max_side, max_pixels)
    if parsed:
        return parsed
    return default


def normalize_seed(seed: int) -> int:
    """-1 / 负数 = 随机。ComfyUI 的 seed 是 64 位整数，这里给到 2^32-1 足够。"""
    try:
        value = int(seed)
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        return random.randint(0, 2**32 - 1)
    return value


def fit_within(width: int, height: int, max_side: int, max_pixels: int) -> tuple[int, int]:
    """把任意尺寸按比例缩到上限以内（保持比例，对齐 8）。

    图生图时参考图可能是 4K 截图：不能直接用，也不能粗暴裁剪 —— 等比缩放最稳。
    """
    w, h = max(1, int(width)), max(1, int(height))
    scale = min(1.0, max_side / max(w, h), (max_pixels / (w * h)) ** 0.5)
    w2 = max(SIZE_MIN_SIDE, int(w * scale) // 8 * 8)
    h2 = max(SIZE_MIN_SIDE, int(h * scale) // 8 * 8)
    return w2, h2


def image_size(data: bytes) -> tuple[int, int] | None:
    """从字节里读图片宽高（PNG / JPEG），读不出返回 None。

    图生图时如果用户没指定画幅，就按参考图的尺寸来 —— 所以必须能读出来，
    不能装 Pillow（插件不额外带依赖）。
    """
    if not data:
        return None
    # PNG：IHDR 在固定偏移
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24 and data[12:16] == b"IHDR":
        import struct

        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    # JPEG：扫 SOF 段（0xC0~0xCF，排除 C4/C8/CC）
    if data[:2] == b"\xff\xd8":
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height = int.from_bytes(data[i + 5 : i + 7], "big")
                width = int.from_bytes(data[i + 7 : i + 9], "big")
                return int(width), int(height)
            if seg_len <= 0:
                return None
            i += 2 + seg_len
    return None
