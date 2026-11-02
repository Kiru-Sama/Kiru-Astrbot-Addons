# -*- coding: utf-8 -*-
"""monitor 的内容解析（纯函数，不依赖 AstrBot）。

从 main.py 搬出来的第二批（2026-11-02）：把一条消息的 content 拆成
「正文 / 系统提醒 / 图片列表」，并清洗空白。

为什么值得单独放 + 测：这是面板「上下文」页显示什么的核心逻辑，
它错一点，用户看到的就是"图没送到模型""正文里一坨路径"。
"""

from __future__ import annotations

import hashlib
import os
import re

# [Image Attachment: path F:\...\media_image_xxx.jpg]
IMG_ATTACH_RE = re.compile(r"\[Image Attachment:\s*path\s*([^\]]*)\]")
# [Image Attachment in quoted message: path F:\...\media_image_xxx.jpg]
QUOTED_IMG_RE = re.compile(
    r"\[Image Attachment in quoted message:\s*path\s*([^\]]*)\]"
)
# <system_reminder>User ID: ..., Nickname: ...</system_reminder>
SYS_REMINDER_RE = re.compile(r"<system_reminder>(.*?)</system_reminder>", re.S)


def extract_images(raw: str) -> tuple[str, list[dict[str, str]]]:
    """挖出图片标记、摘掉，返回 (清洗后文本, 图片列表)。

    两种标记互不误伤：直接发的图 vs 引用消息里的图。
    同一路径只登记一次（按 sha1 前 16 位去重）。
    """
    if not raw:
        return "", []
    images: list[dict[str, str]] = []
    found = [(p, False) for p in IMG_ATTACH_RE.findall(raw)] + [
        (p, True) for p in QUOTED_IMG_RE.findall(raw)
    ]
    for path, quoted in found:
        p = path.strip()
        if not p:
            continue
        k = hashlib.sha1(p.encode("utf-8", "ignore")).hexdigest()[:16]
        if any(im["k"] == k for im in images):
            continue
        images.append(
            {
                "k": k,
                "n": os.path.basename(p),
                "p": p,  # 后端内部用来找原图；发给前端时会被去掉
                "q": quoted,
            }
        )
    text = IMG_ATTACH_RE.sub("", raw)
    text = QUOTED_IMG_RE.sub("", text)
    return text, images


def split_content(raw: str) -> tuple[str, str, list[dict[str, str]]]:
    """把一条消息拆成（正文, 系统提醒, 图片列表）。"""
    if not raw:
        return "", "", []
    text, images = extract_images(raw)
    reminders = [r.strip() for r in SYS_REMINDER_RE.findall(raw) if r.strip()]
    text = SYS_REMINDER_RE.sub("", text)
    text = text.replace("[图片]", "").replace("🖼", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = text.strip()
    if not text.strip("\n \t"):
        text = ""
    return text, "\n".join(reminders), images


def tidy_text(text: str) -> str:
    """清洗纯文本（保留图片占位、把提醒块换成一行 📋）。"""
    if not text:
        return ""
    text = SYS_REMINDER_RE.sub(
        lambda m: "\n📋 " + m.group(1).strip() + "\n", text
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()
