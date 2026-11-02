# -*- coding: utf-8 -*-
"""monitor 的路径探测（纯函数，root 参数化，可在 tmp 目录上测）。

从 main.py 搬出来的第三批（2026-11-02）：
  detect_snowluma_root / find_identity_db / find_onebot_config / first_match / is_lm_unavailable

关键改动：把「AstrBot 根目录 / SnowLuma 目录」变成参数，
这样测试可以造一棵假树，不用在真机上跑。
"""

from __future__ import annotations

import glob
import os


def detect_snowluma_root(astrbot_root: str) -> str:
    """在 AstrBot 安装目录附近找 SnowLuma，找不到返回空串。

    只试固定几个候选位置，绝不全盘遍历。
    """
    base = os.path.dirname(astrbot_root)
    candidates = [
        os.path.join(base, "DATA", "components", "SnowLuma"),
        os.path.join(base, "SnowLuma"),
        os.path.join(astrbot_root, "DATA", "components", "SnowLuma"),
        os.path.join(os.path.dirname(base), "DATA", "components", "SnowLuma"),
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    return ""


def is_lm_unavailable(text: str) -> bool:
    """这句是不是 AstrBot 的「无模型可用」兜底文案。"""
    return "available chat models are unavailable" in (text or "")


def first_match(directory: str, pattern: str) -> str:
    """在目录里按 pattern 找第一个匹配的文件，找不到返回空串。"""
    try:
        hits = sorted(glob.glob(os.path.join(directory, pattern)))
        return hits[0] if hits else ""
    except Exception:  # noqa: BLE001
        return ""


def find_identity_db(sl_dir: str) -> str:
    """在 SnowLuma 的 data/<账号>/ 下挑身份库，挑【最新改动的那个】。

    为什么不是 sorted()[0]：换号后旧账号的库排前面、且不再更新，
    会永远读到旧号（群里查不到名字）。
    """
    try:
        hits = glob.glob(os.path.join(sl_dir, "data", "*", "snowluma_identity.db"))
        best = ""
        best_mtime = -1.0
        for path in hits:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
        return best
    except Exception:  # noqa: BLE001
        return ""


def find_onebot_config(sl_dir: str) -> str:
    """在 SnowLuma 的 config/ 下挑 OneBot 配置，挑【最新改动的那个】。"""
    try:
        hits = glob.glob(os.path.join(sl_dir, "config", "onebot_*.json"))
        best = ""
        best_mtime = -1.0
        for path in hits:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
        # 一个都没写成功时退回老逻辑
        return best or first_match(os.path.join(sl_dir, "config"), "onebot_*.json")
    except Exception:  # noqa: BLE001
        return ""
