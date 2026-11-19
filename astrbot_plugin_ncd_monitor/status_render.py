# -*- coding: utf-8 -*-
"""把 /设备状态 的数据画成**一张**长图：模糊背景 + 半透明卡片。

背景：插件 assets/status_bg.jpg 裁切铺满 + 高斯模糊 + 压暗（避免直接露原图）。
内容（从上到下）：
  1. 模型 / AstrBot   —— 当前模型、来源、能力、上下文上限、压缩配置、上下文占用
  2. 宿主机资源       —— 手机 + 笔记本温度/占用，最近 20 分钟趋势
  3. 近 7 天          —— 峰值、温度曲线、占用曲线、每日概览
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# 6000px 宽的大图会超过 Pillow 默认解压炸弹阈值（89.5MP），这里放宽
Image.MAX_IMAGE_PIXELS = 400_000_000

# ---------------------------------------------------------------- 配色 / 尺寸
W = 900
MARGIN = 22
PAD = 18
TXT = "#111827"
SUB = "#4b5563"
SUB2 = "#6b7280"
LINE = "#e5e7eb"
ACC = "#2563eb"
GREEN = "#059669"
ORANGE = "#d97706"
RED = "#dc2626"
PURPLE = "#7c3aed"

HEADER_H = 128
BLOCK_QUOTA_H = 158
BLOCK_MODEL_H = 292
BLOCK_RES_H = 780
BLOCK_WEEK_H = 1000
GAP = 16

_SCALE: float = 1.0
LAST_TIMING: dict = {}
_BG_CACHE: dict = {}


def _clear_bg_cache() -> None:
    _BG_CACHE.clear()


def _set_scale(scale: float) -> None:
    global _SCALE
    _SCALE = max(1.0, float(scale or 1.0))


class _SDraw:
    """把「900 宽基础坐标系」的绘制调用按 _SCALE 放大到实际画布。"""

    def __init__(self, img):
        self._img = img
        self._d = ImageDraw.Draw(img)

    @staticmethod
    def _xy(xy):
        if xy is None:
            return xy
        if isinstance(xy, (list, tuple)):
            if xy and isinstance(xy[0], (list, tuple)):
                return [tuple(float(v) * _SCALE for v in pt) for pt in xy]
            return [float(v) * _SCALE for v in xy]
        return xy

    def text(self, xy, text, *args, **kwargs):
        return self._d.text(self._xy(xy), text, *args, **kwargs)

    def textbbox(self, xy, text, *args, **kwargs):
        box = self._d.textbbox(self._xy(xy), text, *args, **kwargs)
        return tuple(float(v) / _SCALE for v in box)

    def line(self, xy, *args, **kwargs):
        if kwargs.get("width"):
            kwargs["width"] = max(1, int(round(float(kwargs["width"]) * _SCALE)))
        return self._d.line(self._xy(xy), *args, **kwargs)

    def ellipse(self, xy, *args, **kwargs):
        if kwargs.get("width"):
            kwargs["width"] = max(1, int(round(float(kwargs["width"]) * _SCALE)))
        return self._d.ellipse(self._xy(xy), *args, **kwargs)

    def rounded_rectangle(self, xy, radius=0, *args, **kwargs):
        kwargs["radius"] = max(0, int(round(float(radius) * _SCALE)))
        if kwargs.get("width"):
            kwargs["width"] = max(1, int(round(float(kwargs["width"]) * _SCALE)))
        return self._d.rounded_rectangle(self._xy(xy), *args, **kwargs)

    def rectangle(self, xy, *args, **kwargs):
        if kwargs.get("width"):
            kwargs["width"] = max(1, int(round(float(kwargs["width"]) * _SCALE)))
        return self._d.rectangle(self._xy(xy), *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._d, name)


_FONT_PATH: str = ""
_FONTS: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def configure(font_path: str | None = None) -> None:
    global _FONT_PATH
    if font_path:
        _FONT_PATH = str(font_path)
    _FONTS.clear()


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    size = max(1, int(round(int(size) * _SCALE)))
    key = (int(size), bool(bold))
    hit = _FONTS.get(key)
    if hit is not None:
        return hit
    here = Path(__file__).resolve().parent
    candidates: list[str] = []
    if _FONT_PATH:
        candidates.append(_FONT_PATH)
    candidates += [
        str(here / "assets" / "font_cjk.ttf"),
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/system/fonts/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc",
    ]
    for path in candidates:
        try:
            if path and Path(path).is_file():
                f = ImageFont.truetype(path, int(size))
                _FONTS[key] = f
                return f
        except Exception:  # noqa: BLE001
            continue
    f = ImageFont.load_default()
    _FONTS[key] = f
    return f


# ---------------------------------------------------------------- 小工具
def _tw(d: ImageDraw.ImageDraw, text: str, font) -> int:
    box = d.textbbox((0, 0), text, font=font)
    return int(box[2] - box[0])


def _text(
    d: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    size: int = 16,
    color: str = TXT,
    anchor: str = "la",
    bold: bool = False,
    max_w: int | None = None,
) -> None:
    font = _font(size, bold)
    s = str(text)
    if max_w:
        while s and _tw(d, s, font) > max_w:
            s = s[:-1]
        if s != str(text):
            s = (s[:-1] if len(s) > 1 else "") + "…"
    d.text(xy, s, font=font, fill=color, anchor=anchor)


def _fmt(n) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f} 亿"
    if abs(v - round(v)) < 0.05:
        return f"{int(round(v)):,}"
    return f"{v:,.1f}"


def _pct(n) -> str:
    try:
        return f"{float(n):.1f}%"
    except (TypeError, ValueError):
        return "—"


def _temp(n) -> str:
    try:
        return f"{float(n):.1f}℃"
    except (TypeError, ValueError):
        return "—"


def _temp_color(n) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return SUB2
    if v >= 75:
        return RED
    if v >= 55:
        return ORANGE
    return GREEN


def _pct_color(n) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return SUB2
    if v >= 90:
        return RED
    if v >= 70:
        return ORANGE
    return GREEN


def _bar_row(
    d: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    label: str,
    pct: float | None,
    value_text: str,
    color: str | None = None,
) -> None:
    _text(d, (x, y), label, 14, SUB)
    _text(d, (x + width, y), value_text, 15, TXT, anchor="ra", bold=True)
    by = y + 23
    bh = 11
    d.rounded_rectangle([x, by, x + width, by + bh], radius=6, fill="#e8edf5")
    try:
        frac = max(0.0, min(100.0, float(pct or 0))) / 100.0
    except (TypeError, ValueError):
        frac = 0.0
    if frac > 0:
        d.rounded_rectangle(
            [x, by, x + max(6, int(width * frac)), by + bh],
            radius=6,
            fill=color or _pct_color(pct),
        )


def _chip(d: ImageDraw.ImageDraw, box, title: str, value: str, note: str, color: str) -> None:
    x0, y0, x1, _ = box
    d.rounded_rectangle(list(box), radius=12, fill=(255, 255, 255, 150), outline=(255, 255, 255, 210), width=1)
    _text(d, (x0 + 14, y0 + 10), title, 13, SUB2)
    _text(d, (x0 + 14, y0 + 33), value, 24, color, bold=True)
    _text(d, (x0 + 14, y0 + 67), note, 12, SUB2, max_w=int(x1 - x0 - 28))


def _fmt_duration(sec: int) -> str:
    sec = max(0, int(sec or 0))
    h, rem = divmod(sec, 3600)
    m = rem // 60
    return (f"{h} 小时 " if h else "") + f"{m} 分"


def _kv(d: ImageDraw.ImageDraw, x: int, y: int, width: int, k: str, v: str) -> None:
    _text(d, (x, y), k, 14, SUB2)
    _text(d, (x + width, y), v, 15, TXT, anchor="ra", bold=True)


def _plot(
    d: ImageDraw.ImageDraw,
    box,
    series: list[tuple[str, str, list[tuple[float, float]]]],
    title: str,
    y_min: float = 0.0,
    y_max: float = 100.0,
    y_suffix: str = "%",
    empty_hint: str = "样本不足",
) -> None:
    x0, y0, x1, y1 = box
    _text(d, (x0, y0), title, 16, TXT, bold=True)
    lx = x1
    for label, color, _pts in reversed(series):
        font = _font(13)
        tw = _tw(d, label, font)
        d.line([(lx - tw - 30, y0 + 9), (lx - tw - 18, y0 + 9)], fill=color, width=3)
        _text(d, (lx - tw - 12, y0 + 2), label, 13, SUB2)
        lx -= tw + 46
    plot_y0 = y0 + 28
    plot_y1 = y1 - 18
    plot_x0 = x0 + 48
    plot_x1 = x1
    for i in range(3):
        v = y_min + (y_max - y_min) * i / 2
        yy = plot_y1 - (plot_y1 - plot_y0) * i / 2
        d.line([(plot_x0, yy), (plot_x1, yy)], fill=(229, 231, 235, 220), width=1)
        _text(d, (plot_x0 - 8, yy), f"{v:.0f}{y_suffix}", 12, SUB2, anchor="rm")
    ts: list[float] = []
    for _label, _color, pts in series:
        ts.extend([float(p[0]) for p in pts if p[1] is not None])
    if len(ts) < 2:
        _text(
            d,
            ((plot_x0 + plot_x1) / 2, (plot_y0 + plot_y1) / 2),
            empty_hint,
            14,
            SUB2,
            anchor="mm",
        )
        return
    t_lo, t_hi = min(ts), max(ts)
    span = max(1.0, t_hi - t_lo)

    def px(t: float) -> float:
        return plot_x0 + (float(t) - t_lo) / span * (plot_x1 - plot_x0)

    def py(v: float) -> float:
        frac = (float(v) - y_min) / max(1e-6, y_max - y_min)
        return plot_y1 - max(0.0, min(1.0, frac)) * (plot_y1 - plot_y0)

    for _label, color, pts in series:
        line = [(px(t), py(v)) for t, v in pts if v is not None]
        if len(line) >= 2:
            d.line(line, fill=color, width=2, joint="curve")
        if line:
            d.ellipse([line[-1][0] - 3, line[-1][1] - 3, line[-1][0] + 3, line[-1][1] + 3], fill=color)
    _text(d, (plot_x0, y1 - 2), time.strftime("%m-%d %H:%M", time.localtime(t_lo)), 12, SUB2)
    _text(d, (plot_x1, y1 - 2), time.strftime("%m-%d %H:%M", time.localtime(t_hi)), 12, SUB2, anchor="ra")


# ---------------------------------------------------------------- 背景 / 卡片
def _load_bg_settings(bg_dir) -> dict:
    try:
        path = Path(bg_dir) / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _bg_image_files(bg_dir) -> list[Path]:
    base = Path(bg_dir) if bg_dir else None
    if not base or not base.is_dir():
        return []
    exts = (".jpg", ".jpeg", ".png", ".webp")
    try:
        return [
            p
            for p in sorted(base.iterdir())
            if p.is_file() and p.suffix.lower() in exts
        ]
    except OSError:
        return []


def _pick_bg(bg_dir) -> tuple[Path | None, dict]:
    """返回 (背景文件, 设置)；设置里可带 blur / dim。"""
    settings = _load_bg_settings(bg_dir) if bg_dir else {}
    here = Path(__file__).resolve().parent / "assets" / "status_bg.jpg"
    files = _bg_image_files(bg_dir)
    mode = str(settings.get("mode") or "builtin").lower()
    chosen: Path | None = None
    if mode == "random" and files:
        chosen = random.choice(files)
    elif mode == "single":
        name = str(settings.get("current") or "").strip()
        if name and bg_dir:
            cand = Path(bg_dir) / name
            if cand.is_file():
                chosen = cand
    if chosen is None and here.is_file():
        chosen = here
    if chosen is None and files:
        chosen = random.choice(files)
    return chosen, settings


def _make_background(H: int, bg_dir=None) -> Image.Image:
    W2, H2 = max(1, int(round(W * _SCALE))), max(1, int(round(H * _SCALE)))
    src_path, settings = _pick_bg(bg_dir)
    try:
        _mtime = src_path.stat().st_mtime if src_path else 0
    except OSError:
        _mtime = 0
    _key = (
        str(src_path or ""),
        round(float(_mtime), 3),
        W2,
        H2,
        str(settings.get("blur", 9)),
        str(settings.get("dim", 0.22)),
    )
    _cached = _BG_CACHE.get("img")
    if _cached is not None and _BG_CACHE.get("key") == _key:
        try:
            return _cached.copy()
        except Exception:  # noqa: BLE001
            _BG_CACHE.clear()
    if src_path is None:
        return Image.new("RGB", (W2, H2), "#dfe7f3")
    try:
        src = Image.open(src_path).convert("RGB")
    except Exception:  # noqa: BLE001
        return Image.new("RGB", (W2, H2), "#dfe7f3")
    sw, sh = src.size
    scale = max(W2 / sw, H2 / sh)
    nw, nh = max(1, int(sw * scale + 1)), max(1, int(sh * scale + 1))
    src = src.resize((nw, nh), Image.LANCZOS)
    left = (nw - W2) // 2
    top = (nh - H2) // 2
    bg = src.crop((left, top, left + W2, top + H2))
    try:
        blur = max(1.0, min(40.0, float(settings.get("blur", 9))))
    except (TypeError, ValueError):
        blur = 9.0
    try:
        dim = max(0.0, min(0.75, float(settings.get("dim", 0.22))))
    except (TypeError, ValueError):
        dim = 0.22
    bg = bg.filter(ImageFilter.GaussianBlur(max(1.0, blur * _SCALE)))
    dark = Image.new("RGB", (W2, H2), "#0f172a")
    bg = Image.blend(bg, dark, dim)
    _BG_CACHE["key"] = _key
    _BG_CACHE["img"] = bg.copy()
    return bg


def _glass_card(img: Image.Image, box, radius: int = 18, alpha: int = 146) -> None:
    """在背景上贴一块半透明白色毛玻璃卡片（保留底色色调）。"""
    x0, y0, x1, y1 = [int(round(float(v) * _SCALE)) for v in box]
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(
        [0, 0, w, h],
        radius=max(1, int(round(radius * _SCALE))),
        fill=(255, 255, 255, alpha),
    )
    img.paste(overlay, (x0, y0), overlay)
    d = ImageDraw.Draw(img)  # 坐标已乘过 _SCALE，这里不能再走 _SDraw
    ww = max(1, int(round(2 * _SCALE)))
    inset = max(1, ww // 2)
    d.rounded_rectangle(
        [x0 + inset, y0 + inset, x1 - inset - 1, y1 - inset - 1],
        radius=max(1, int(round(radius * _SCALE))),
        outline=(255, 255, 255, 235),
        width=ww,
    )


def _block_title(d: ImageDraw.ImageDraw, x: int, y: int, title: str, sub: str) -> int:
    _text(d, (x, y), title, 20, TXT, bold=True)
    if sub:
        _text(d, (W - MARGIN - PAD, y + 5), sub, 13, SUB2, anchor="ra")
    d.line([(x, y + 30), (W - MARGIN - PAD, y + 30)], fill=(255, 255, 255, 200), width=2)
    return y + 42


# ---------------------------------------------------------------- 内容块
def _draw_quota_block(img: Image.Image, payload: dict, top: int) -> None:
    q = payload.get("quota") or {}
    x0, y0, x1 = MARGIN, top, W - MARGIN
    _glass_card(img, (x0, y0, x1, y0 + BLOCK_QUOTA_H))
    d = _SDraw(img)
    y = _block_title(d, x0 + PAD, y0 + 16, "出图额度 / 限流", "")
    win_label = str(q.get("window_label") or "")
    if win_label:
        _text(
            d,
            (x1 - PAD, y0 + 24),
            f"{win_label} · 距重置 {_fmt_duration(int(q.get('window_remaining') or 0))}",
            12,
            SUB2,
            anchor="ra",
        )
    pc = int(q.get("private_count") or 0)
    pl = int(q.get("private_limit") or 0)
    triggered = bool(q.get("private_triggered"))
    bans = int(q.get("bans") or 0)
    throttle = int(q.get("throttle_pool") or 0)
    canvas = int(q.get("canvas_pool") or 0)
    if not q:
        _text(d, (x0 + PAD, y + 10), "总线暂无额度数据（comfy 插件未发布）", 14, SUB2)
        return
    cols = [
        (
            "私聊全局成功",
            f"{pc} / {pl}",
            "已触发前 5 限流" if triggered else "本时段私聊额度",
            RED if triggered or (pl and pc > pl) else TXT,
        ),
        (
            "群聊全局生图",
            f"{int(q.get('group_count') or 0)} 张",
            f"累计 {int(q.get('group_total') or 0)} 张 · 不占额度",
            TXT,
        ),
        (
            "本时段参与用户",
            str(int(q.get("total_users") or q.get("users") or 0)),
            f"累计成功 {int(q.get('total_images') or 0)} 张",
            TXT,
        ),
        ("禁生图", str(bans), "暂无" if bans == 0 else "暂停中", GREEN if bans == 0 else RED),
        (
            "限流池 / 画幅池",
            f"{throttle} / {canvas}",
            f"前 {throttle} 名 / 前 {canvas} 名",
            TXT,
        ),
    ]
    cw = (x1 - x0 - 2 * PAD - 4 * 10) // 5
    for i, (title, value, note, color) in enumerate(cols):
        bx = x0 + PAD + i * (cw + 10)
        d.rounded_rectangle(
            [bx, y, bx + cw, y + 96],
            radius=12,
            fill=(255, 255, 255, 120),
            outline=(255, 255, 255, 185),
            width=1,
        )
        _text(d, (bx + 12, y + 10), title, 12, SUB2)
        _text(d, (bx + 12, y + 30), value, 24, color, bold=True)
        _text(d, (bx + 12, y + 68), note, 11, SUB2, max_w=cw - 24)


def _draw_model_block(img: Image.Image, payload: dict, top: int) -> None:
    ov = payload.get("overview") or {}
    extra = payload.get("astrbot_extra") or {}
    ab = ov.get("astrbot") or {}
    ctx = ov.get("context") or {}
    x0, y0, x1 = MARGIN, top, W - MARGIN
    _glass_card(img, (x0, y0, x1, y0 + BLOCK_MODEL_H))
    d = _SDraw(img)
    y = _block_title(d, x0 + PAD, y0 + 16, "模型 / AstrBot", "")

    model = str(ab.get("model") or "—")
    src = str(extra.get("base_url") or "").strip()
    src_text = ("云端 " + src) if src else "—"
    modalities = " / ".join(str(x) for x in (ab.get("modalities") or [])) or "—"
    max_ctx = ab.get("max_context_tokens") or 0
    extra_body = ab.get("custom_extra_body") or {}
    reasoning = str(extra_body.get("reasoning_effort") or "").strip().lower()
    think_text = (
        f"已开启（effort={reasoning}）"
        if reasoning in ("none", "minimal", "low")
        else "未开启" if reasoning == "" else f"effort={reasoning}"
    )
    comp = ab.get("compression_config") or {}
    items = [
        ("当前模型", model),
        ("模型来源", src_text),
        ("模型能力", modalities),
        ("上下文上限", f"{_fmt(max_ctx)} token"),
        ("关闭思考", think_text),
        ("压缩策略", str(comp.get("strategy") or "—")),
        ("压缩轮数阈值", str(comp.get("max_turns") if comp.get("max_turns") is not None else "—")),
        ("保留最近比例", str(comp.get("keep_recent_ratio") if comp.get("keep_recent_ratio") is not None else "—")),
        ("群聊上下文条数", str(ab.get("group_message_max_cnt") if ab.get("group_message_max_cnt") is not None else "—")),
    ]
    col_gap = 12
    col_w = (x1 - x0 - 2 * PAD - 2 * col_gap) // 3
    for i, (k, v) in enumerate(items):
        col = i % 3
        row = i // 3
        bx = x0 + PAD + col * (col_w + col_gap)
        by = y + row * 48
        _text(d, (bx, by), k, 12, SUB2)
        _text(d, (bx, by + 19), v, 15, TXT, bold=True, max_w=col_w)
    y += 3 * 48 + 8

    used = ctx.get("used") or ab.get("last_prompt_tokens") or 0
    pct = ctx.get("percent") or (round(float(used) / float(max_ctx) * 100, 1) if max_ctx else 0)
    _text(d, (x0 + PAD, y), "当前上下文", 15, TXT, bold=True)
    _text(
        d,
        (x1 - PAD, y),
        f"{_fmt(used)} / {_fmt(max_ctx)} token（{_pct(pct)}）",
        14,
        SUB,
        anchor="ra",
    )
    by2 = y + 26
    bw = x1 - x0 - 2 * PAD
    d.rounded_rectangle([x0 + PAD, by2, x0 + PAD + bw, by2 + 14], radius=7, fill="#e8edf5")
    frac = max(0.0, min(100.0, float(pct or 0))) / 100.0
    if frac > 0:
        d.rounded_rectangle(
            [x0 + PAD, by2, x0 + PAD + max(8, int(bw * frac)), by2 + 14],
            radius=7,
            fill=_pct_color(pct),
        )
    _text(
        d,
        (x0 + PAD, by2 + 22),
        f"压缩触发线 {int(float(ab.get('compression_threshold') or 0.82) * 100)}%"
        f"（约 {_fmt(ctx.get('threshold_tokens') or 0)} token）",
        12,
        SUB2,
    )


def _draw_resources_block(img: Image.Image, payload: dict, top: int) -> None:
    si = payload.get("sysinfo") or {}
    latest = si.get("latest") or {}
    pc = si.get("pc") or {}
    pc_age = si.get("pc_age")
    gpu = pc.get("gpu") or {}
    x0, y0, x1 = MARGIN, top, W - MARGIN
    _glass_card(img, (x0, y0, x1, y0 + BLOCK_RES_H))
    d = _SDraw(img)
    y = _block_title(d, x0 + PAD, y0 + 16, "宿主机资源", "温度优先 · 手机实时 + 笔记本 linkd")

    # 温度 chips（4 个）
    cw = (x1 - x0 - 2 * PAD - 3 * 12) // 4
    # ★ 2026-11-19 修 M4：颜色判断用原始数值（之前传 "45.2℃" 字符串，float() 失败→永远灰色）
    chips = [
        ("手机 SoC", _temp(latest.get("temp")), "sysfs · 10 秒刷新", latest.get("temp")),
        ("手机电池", _temp(latest.get("temp_ph_batt")), "power_supply", latest.get("temp_ph_batt")),
        ("手机外壳", _temp(latest.get("temp_ph_shell")), "shell", latest.get("temp_ph_shell")),
        ("笔记本 GPU", _temp(gpu.get("temp")), (gpu.get("name") or "linkd 未连接"), gpu.get("temp")),
    ]
    for i, (t, v, note, raw) in enumerate(chips):
        _chip(
            d,
            (x0 + PAD + i * (cw + 12), y, x0 + PAD + i * (cw + 12) + cw, y + 92),
            t,
            v,
            note,
            _temp_color(raw) if raw is not None else SUB2,
        )
    y += 104

    # 手机卡
    _glass_card(img, (x0 + PAD, y, x1 - PAD, y + 104), radius=14, alpha=80)
    d = _SDraw(img)
    _text(d, (x0 + PAD + 14, y + 10), "手机（AstrBot 宿主）", 15, TXT, bold=True)
    _text(
        d,
        (x1 - PAD - 14, y + 13),
        f"已采 {len(si.get('phone_series') or si.get('series') or [])} 点"
        f" · 1 秒/点",
        13,
        SUB2,
        anchor="ra",
    )
    half = (x1 - x0 - 2 * PAD - 3 * 18) // 2
    _bar_row(d, x0 + PAD + 14, y + 40, half, "CPU", latest.get("cpu"), _pct(latest.get("cpu")))
    _bar_row(
        d,
        x0 + PAD + 14 + half + 18,
        y + 40,
        half,
        "内存",
        latest.get("mem"),
        f"{_fmt(latest.get('mem_used_gb'))} / {_fmt(latest.get('mem_total_gb'))} GB",
    )
    y += 116

    # 笔记本卡
    _glass_card(img, (x0 + PAD, y, x1 - PAD, y + 192), radius=14, alpha=80)
    d = _SDraw(img)
    _text(d, (x0 + PAD + 14, y + 10), "笔记本（ComfyUI 宿主）", 15, TXT, bold=True)
    age_text = f"linkd 数据 {int(float(pc_age))} 秒前" if pc_age is not None else "linkd 未连接"
    _text(d, (x1 - PAD - 14, y + 13), age_text, 13, SUB2, anchor="ra")
    _text(d, (x0 + PAD + 14, y + 32), str(gpu.get("name") or "—"), 12, SUB2, max_w=x1 - x0 - 2 * PAD - 28)
    rows = [
        ("CPU", pc.get("cpu"), _pct(pc.get("cpu"))),
        ("GPU", gpu.get("util"), _pct(gpu.get("util"))),
        (
            "显存",
            (round(gpu.get("mem_used_mb", 0) / gpu.get("mem_total_mb", 1) * 100, 1) if gpu.get("mem_total_mb") else None),
            f"{_fmt(gpu.get('mem_used_mb'))} / {_fmt(gpu.get('mem_total_mb'))} MiB",
        ),
        (
            "内存",
            pc.get("ram_pct"),
            f"{_fmt(pc.get('ram_used_gb'))} / {_fmt(pc.get('ram_total_gb'))} GB",
        ),
    ]
    col_w = (x1 - x0 - 2 * PAD - 3 * 18) // 2
    for i, (label, val, text) in enumerate(rows):
        col = i % 2
        row = i // 2
        _bar_row(
            d,
            x0 + PAD + 14 + col * (col_w + 18),
            y + 54 + row * 58,
            col_w,
            label,
            val,
            text,
        )
    _text(
        d,
        (x0 + PAD + 14, y + 170),
        f"GPU 温度 {_temp(gpu.get('temp'))} · 功耗 "
        f"{(str(gpu.get('power')) + 'W') if gpu.get('power') not in (None, '', '[N/A]') else '—'}"
        f" · 来源 {gpu.get('gpu_temp_src') or pc.get('gpu_temp_src') or '—'}",
        12,
        SUB2,
        max_w=x1 - x0 - 2 * PAD - 28,
    )
    y += 204

    # 趋势卡
    _glass_card(img, (x0 + PAD, y, x1 - PAD, y0 + BLOCK_RES_H - 14), radius=14, alpha=80)
    d = _SDraw(img)
    series: list[tuple[str, str, list[tuple[float, float]]]] = []
    pc_series = si.get("pc_series") or []
    if pc_series:
        series += [
            ("笔记本 GPU", ACC, [(float(r.get("t") or 0), r.get("gpu")) for r in pc_series if r.get("gpu") is not None]),
            ("显存", PURPLE, [(float(r.get("t") or 0), r.get("vram")) for r in pc_series if r.get("vram") is not None]),
            ("内存", GREEN, [(float(r.get("t") or 0), r.get("mem")) for r in pc_series if r.get("mem") is not None]),
        ]
    _plot(
        d,
        (x0 + PAD + 14, y + 10, x1 - PAD - 14, y0 + BLOCK_RES_H - 34),
        [s for s in series if s[2]],
        "最近 20 分钟占用（笔记本）",
        0,
        100,
        "%",
        empty_hint="还没有足够的采样点（笔记本 linkd 未连接）",
    )


def _peak_chips(
    d: ImageDraw.ImageDraw,
    peaks: dict,
    x0: int,
    y: int,
    width: int,
    items: list[tuple[str, str, str, bool]],
) -> None:
    cw = (width - 2 * 12) // 3
    for i, (title, key, unit, is_temp) in enumerate(items[:3]):
        p = peaks.get(key) or {}
        v = p.get("v")
        at = str(p.get("t_str") or "")
        color = (_temp_color(v) if is_temp else _pct_color(v)) if v is not None else SUB2
        value = f"{_fmt(v)}{unit}" if v is not None else "—"
        bx = x0 + i * (cw + 12)
        _chip(d, (bx, y, bx + cw, y + 86), title, value, f"@ {at}" if at else "—", color)


def _draw_week_block(img: Image.Image, payload: dict, top: int, week_h: int) -> None:
    st = payload.get("stats7d") or {}
    samples = int(st.get("samples") or 0)
    days = int(st.get("days") or 7)
    buckets = st.get("buckets") or []
    peaks = st.get("peaks") or {}
    daily = st.get("daily") or []
    x0, y0, x1 = MARGIN, top, W - MARGIN
    _glass_card(img, (x0, y0, x1, y0 + week_h))
    d = _SDraw(img)
    y = _block_title(d, x0 + PAD, y0 + 16, "近 7 天", f"{_fmt(samples)} 个样本 · {days} 天 · 10 秒聚合")

    inner_w = x1 - x0 - 2 * PAD
    _peak_chips(
        d, peaks, x0 + PAD, y, inner_w,
        [
            ("笔记本 GPU 最高", "pc_temp", "℃", True),
            ("手机 SoC 最高", "ph_temp", "℃", True),
            ("GPU 占用峰值", "pc_gpu", "%", False),
        ],
    )
    _peak_chips(
        d, peaks, x0 + PAD, y + 96, inner_w,
        [
            ("显存峰值", "pc_vram", "%", False),
            ("CPU 占用峰值", "pc_cpu", "%", False),
            ("手机 GPU 最高", "ph_gpu_temp", "℃", True),
        ],
    )
    y += 196

    # 温度曲线
    _glass_card(img, (x0 + PAD, y, x1 - PAD, y + 196), radius=14, alpha=80)
    d = _SDraw(img)
    _plot(
        d,
        (x0 + PAD + 14, y + 10, x1 - PAD - 14, y + 182),
        [
            ("笔记本 GPU", RED, [(float(r.get("t") or 0), r.get("pc_temp")) for r in buckets if r.get("pc_temp") is not None]),
            ("手机 SoC", GREEN, [(float(r.get("t") or 0), r.get("ph_temp")) for r in buckets if r.get("ph_temp") is not None]),
        ],
        "温度曲线（按小时平均）",
        20,
        80,
        "℃",
    )
    y += 208

    # 占用曲线
    _glass_card(img, (x0 + PAD, y, x1 - PAD, y + 196), radius=14)
    d = _SDraw(img)
    _plot(
        d,
        (x0 + PAD + 14, y + 10, x1 - PAD - 14, y + 182),
        [
            ("笔记本 GPU", ACC, [(float(r.get("t") or 0), r.get("pc_gpu")) for r in buckets if r.get("pc_gpu") is not None]),
            ("显存", PURPLE, [(float(r.get("t") or 0), r.get("pc_vram")) for r in buckets if r.get("pc_vram") is not None]),
            ("CPU", ORANGE, [(float(r.get("t") or 0), r.get("pc_cpu")) for r in buckets if r.get("pc_cpu") is not None]),
        ],
        "占用曲线（按小时平均）",
        0,
        100,
        "%",
    )
    y += 208

    # 每日概览
    _glass_card(img, (x0 + PAD, y, x1 - PAD, y0 + week_h - 14), radius=14, alpha=80)
    d = _SDraw(img)
    _text(d, (x0 + PAD + 14, y + 10), "每日概览", 15, TXT, bold=True)
    cols = ["日期", "笔记本 GPU 温度", "手机最高温", "GPU 占用", "显存", "样本数"]
    widths = [96, 150, 130, 110, 96, 96]
    cx = x0 + PAD + 14
    cy = y + 38
    for i, c in enumerate(cols):
        _text(d, (cx, cy), c, 12, SUB2, bold=True)
        cx += widths[i]
    cy += 24
    d.line([(x0 + PAD + 14, cy - 4), (x1 - PAD - 14, cy - 4)], fill=(229, 231, 235, 230), width=1)
    for row in daily[-6:]:
        cx = x0 + PAD + 14
        vals = [
            str(row.get("day") or "—")[5:],
            _temp(row.get("pc_temp_max")),
            _temp(row.get("ph_temp_max")),
            _pct(row.get("pc_gpu_max")),
            _pct(row.get("pc_vram_max")),
            _fmt(row.get("samples")),
        ]
        for i, v in enumerate(vals):
            _text(d, (cx, cy), v, 13, TXT if i else SUB2, bold=bool(i))
            cx += widths[i]
        cy += 26
        d.line([(x0 + PAD + 14, cy - 6), (x1 - PAD - 14, cy - 6)], fill=(229, 231, 235, 210), width=1)


# ---------------------------------------------------------------- 对外入口
def render_status_image(
    payload: dict,
    out_dir: Path | str,
    font_path: str | None = None,
    bg_dir: Path | str | None = None,
    target_long_side: int = 6000,
) -> Path:
    """渲染 /设备状态 的整张信息图（原生高分辨率绘制，宽度默认 6000px）。"""
    if font_path:
        configure(font_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    st = payload.get("stats7d") or {}
    daily_rows = len((st.get("daily") or [])[-6:])
    week_h = 42 + 200 + 208 + 208 + (38 + 24 + daily_rows * 26 + 30) + 14
    H = (
        HEADER_H
        + BLOCK_QUOTA_H
        + GAP
        + BLOCK_MODEL_H
        + GAP
        + BLOCK_RES_H
        + GAP
        + week_h
        + 48
    )
    target = int(target_long_side or 0)
    base_long = max(W, H)
    scale = (float(target) / float(base_long)) if target > base_long else 1.0
    _set_scale(scale)
    import time as _time

    _t0 = _time.time()
    img = _make_background(H, bg_dir)
    _t_bg = _time.time()
    d = _SDraw(img)
    # 顶部标题
    _text(d, (MARGIN, 34), "设备状态", 40, "#ffffff", bold=True)
    _text(
        d,
        (MARGIN, 84),
        "模型 / AstrBot · 宿主机资源 · 近 7 天",
        16,
        "#dbe4f0",
    )
    _text(d, (W - MARGIN, 52), str(payload.get("now") or ""), 15, "#dbe4f0", anchor="ra")
    d.line([(MARGIN, HEADER_H - 12), (W - MARGIN, HEADER_H - 12)], fill=(255, 255, 255, 120), width=2)

    y = HEADER_H
    _draw_quota_block(img, payload, y)
    y += BLOCK_QUOTA_H + GAP
    _draw_model_block(img, payload, y)
    y += BLOCK_MODEL_H + GAP
    _draw_resources_block(img, payload, y)
    y += BLOCK_RES_H + GAP
    _draw_week_block(img, payload, y, week_h)

    d = _SDraw(img)
    _text(
        d,
        (W // 2, H - 24),
        "ncd_monitor · 手机 1 秒采样 / 笔记本 10 秒落盘 · 滚动保留 7 天 · 额度为 comfy 实时摘要",
        12,
        "#cbd5e1",
        anchor="mm",
    )

    _t_draw = _time.time()
    # ★ 2026-11-19 修 M5：加微秒避免同秒并发写同一文件；
    #   Windows 的 time.strftime 不支持 %f（会输出字面 %f），用 datetime。
    from datetime import datetime as _dt

    stamp = _dt.now().strftime("%Y%m%d-%H%M%S-%f")
    if img.width > 1600:
        p = out / f"status_{stamp}.jpg"
        img.save(p, format="JPEG", quality=88, optimize=True)
    else:
        p = out / f"status_{stamp}.png"
        img.save(p, format="PNG", optimize=True)
    LAST_TIMING.clear()
    LAST_TIMING.update(
        {
            "bg_s": round(_t_bg - _t0, 2),
            "draw_s": round(_t_draw - _t_bg, 2),
            "save_s": round(_time.time() - _t_draw, 2),
            "total_s": round(_time.time() - _t0, 2),
            "size": str(img.width) + "x" + str(img.height),
        }
    )
    try:
        olds = sorted(
            list(out.glob("status_*.png")) + list(out.glob("status_*.jpg")),
            key=lambda q: q.stat().st_mtime,
            reverse=True,
        )
        for q in olds[20:]:
            q.unlink(missing_ok=True)
    except OSError:
        pass
    return p
