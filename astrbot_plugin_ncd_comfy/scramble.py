# -*- coding: utf-8 -*-
"""小番茄图片混淆（无损）。

算法来源（不是自创，逐行对照复用）：
  · Gilbert 曲线遍历：liuqirui911/image-scrambler 的 PopularCodecs.java（MIT）
    —— generate2d / split / emitLine 三个函数按原结构一比一翻译，分支、取整、
       坐标变换都保持原样，避免"看起来对但像素错位"。
  · 映射构造（位移 + 置换表）：lihaoyun6/ComfyUI-TomatoCrypt 的 get_scrambling_map
    —— 同一套：offset = round(0.6180339887 × 像素数)，再按曲线顺序做循环位移。

性质：
  · 纯像素重排，不改像素值 → 无损（前提：存 PNG，不能存 JPG）
  · 无密钥（固定规则），谁拿到图都能还原——这正是小番茄的用途

用法：
  scramble_image(png_bytes)            混淆
  scramble_image(png_bytes, decrypt=True)  还原
"""

from __future__ import annotations

import io
import math

#: (√5-1)/2，与开源实现里 (Math.sqrt(5)-1)/2 完全一致
GOLDEN = (math.sqrt(5.0) - 1.0) / 2.0


def _sign(v: int) -> int:
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


class _Sub:
    """一段子矩形：递归遍历参数 + 它在结果数组里的起始下标。"""

    __slots__ = ("start", "x", "y", "ax", "ay", "bx", "by")

    def __init__(self, start, x, y, ax, ay, bx, by):
        self.start = start
        self.x = x
        self.y = y
        self.ax = ax
        self.ay = ay
        self.bx = bx
        self.by = by

    def size(self) -> int:
        return abs(self.ax + self.ay) * abs(self.bx + self.by)


def _emit_line(out: list[int], start: int, x: int, y: int, width: int, dx: int, dy: int, count: int) -> None:
    idx = x + y * width
    step = dx + dy * width
    cur = start
    for _ in range(count):
        out[cur] = idx
        cur += 1
        idx += step


def _split(start: int, x: int, y: int, ax: int, ay: int, bx: int, by: int) -> list[_Sub]:
    w = abs(ax + ay)
    h = abs(bx + by)
    dax, day = _sign(ax), _sign(ay)
    dbx, dby = _sign(bx), _sign(by)
    ax2, ay2 = ax // 2, ay // 2
    bx2, by2 = bx // 2, by // 2
    w2 = abs(ax2 + ay2)
    h2 = abs(bx2 + by2)

    if 2 * w > 3 * h:
        if (w2 & 1) == 1 and w > 2:
            ax2 += dax
            ay2 += day
        s0 = _Sub(start, x, y, ax2, ay2, bx, by)
        s1 = _Sub(s0.start + s0.size(), x + ax2, y + ay2, ax - ax2, ay - ay2, bx, by)
        return [s0, s1]

    if (h2 & 1) == 1 and h > 2:
        bx2 += dbx
        by2 += dby
    s0 = _Sub(start, x, y, bx2, by2, ax2, ay2)
    s1 = _Sub(s0.start + s0.size(), x + bx2, y + by2, ax, ay, bx - bx2, by - by2)
    s2 = _Sub(
        s1.start + s1.size(),
        x + (ax - dax) + (bx2 - dbx),
        y + (ay - day) + (by2 - dby),
        -bx2,
        -by2,
        -(ax - ax2),
        -(ay - ay2),
    )
    return [s0, s1, s2]


def _generate2d(out: list[int], start: int, x: int, y: int, ax: int, ay: int, bx: int, by: int, width: int) -> None:
    w = abs(ax + ay)
    h = abs(bx + by)
    if h == 1:
        _emit_line(out, start, x, y, width, _sign(ax), _sign(ay), w)
        return
    if w == 1:
        _emit_line(out, start, x, y, width, _sign(bx), _sign(by), h)
        return
    for s in _split(start, x, y, ax, ay, bx, by):
        _generate2d(out, s.start, s.x, s.y, s.ax, s.ay, s.bx, s.by, width)


def gilbert_order(width: int, height: int) -> list[int]:
    """Gilbert 曲线经过的像素下标序列（长度 = width*height）。"""
    width = int(width)
    height = int(height)
    order = [0] * (width * height)
    if width >= height:
        _generate2d(order, 0, 0, 0, width, 0, 0, height, width)
    else:
        _generate2d(order, 0, 0, 0, 0, height, width, 0, width)
    return order


def tomato_map(width: int, height: int) -> list[int]:
    """小番茄的置换表：map[j] = 第 j 个像素取自原图的哪个下标（加密方向）。"""
    n = width * height
    order = gilbert_order(width, height)
    offset = int(math.floor(GOLDEN * n + 0.5)) % n
    mapping = [0] * n
    for i in range(n):
        mapping[order[(i + offset) % n]] = order[i]
    return mapping


def scramble_bytes(raw: bytes, width: int, height: int, decrypt: bool = False) -> bytes:
    """对 RGBA 原始字节做小番茄混淆/还原（每像素 4 字节）。"""
    n = width * height
    if len(raw) != n * 4:
        raise ValueError(f"像素数据长度不对：期望 {n * 4}，实际 {len(raw)}")
    mapping = tomato_map(width, height)
    out = bytearray(len(raw))
    if decrypt:
        # 逆置换：dst[i] = src[mapping[i]]
        for i in range(n):
            d = i * 4
            out[d : d + 4] = raw[mapping[i] * 4 : mapping[i] * 4 + 4]
    else:
        # 正置换：dst[mapping[i]] = src[i]
        for i in range(n):
            d = mapping[i] * 4
            out[d : d + 4] = raw[i * 4 : i * 4 + 4]
    return bytes(out)


def scramble_image(data: bytes, decrypt: bool = False) -> bytes:
    """PNG 字节 → 小番茄混淆后的 PNG 字节（无损；解密则反向）。"""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    width, height = img.size
    out = scramble_bytes(img.tobytes(), width, height, decrypt=decrypt)
    new = Image.frombytes("RGBA", (width, height), out)
    buf = io.BytesIO()
    new.save(buf, "PNG")
    return buf.getvalue()


def _selftest() -> int:
    """往返测试：混淆再还原必须逐像素相同。"""
    from PIL import Image

    ok = 0
    for (w, h) in ((1, 1), (2, 2), (3, 3), (7, 5), (16, 9), (64, 48), (100, 37)):
        img = Image.new("RGBA", (w, h))
        px = []
        for y in range(h):
            for x in range(w):
                px.append(((x * 7 + y) % 256, (y * 11 + x) % 256, (x ^ y) % 256, 255))
        img.putdata(px)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        original = buf.getvalue()

        enc = scramble_image(original)
        dec = scramble_image(enc, decrypt=True)
        a = Image.open(io.BytesIO(original)).convert("RGBA").tobytes()
        b = Image.open(io.BytesIO(dec)).convert("RGBA").tobytes()
        same = a == b
        n = w * h
        order = gilbert_order(w, h)
        perm_ok = sorted(order) == list(range(n))
        print(f"  {w}x{h}: 往返 {'OK' if same else 'FAIL'} | 曲线是排列 {'OK' if perm_ok else 'FAIL'} | 混淆与原图不同 {enc != original}")
        if same and perm_ok:
            ok += 1
    print(f"\n  {ok} 组通过")
    return 0 if ok == 7 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
