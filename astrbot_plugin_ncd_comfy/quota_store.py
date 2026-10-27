# -*- coding: utf-8 -*-
"""限流状态的原子读写。

只做文件层，不认识 QuotaState；这样主流程和离线测试都能直接用。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

__all__ = ["backup_bad", "load_state", "save_state"]


def load_state(path: Path) -> dict | None:
    """读状态；文件不存在/为空返回 None，内容坏了先备份再抛 ValueError。"""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        backup_bad(path)
        raise ValueError(f"限流状态不是 UTF-8: {exc}") from exc
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        backup_bad(path)
        raise ValueError(f"限流状态不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        backup_bad(path)
        raise ValueError("限流状态根节点不是对象")
    return data


def save_state(path: Path, data: dict) -> None:
    """先写同目录临时文件，再 os.replace；避免写一半把旧档顶掉。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def backup_bad(path: Path) -> Path | None:
    """坏档留一份带时间戳的副本，方便事后查；失败不抛。"""
    path = Path(path)
    try:
        dst = path.with_name(f"{path.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, dst)
        return dst
    except OSError:
        return None
