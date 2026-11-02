# -*- coding: utf-8 -*-
"""群聊主动发言的判定逻辑（纯函数，可离线测）。

规则（用户定的）：
  · 24 小时内有人主动 @ 过 bot 的群 → 每隔 1 小时主动说一句
  · 24~48 小时内被 @ 过的群       → 每隔 3 小时主动说一句
  · 超过 48 小时没被 @ 的群       → 不主动发言
  · 凌晨 2:00~7:00 一律不主动发言

状态（每个群的「最后被 @ 时间」「最后主动发言时间」）由调用方存盘，
这里只负责「现在该不该说、间隔是多少」。
"""

from __future__ import annotations

import random

#: 24h 内 → 1 小时一次；48h 内 → 3 小时一次
TIER_FAST_HOURS = 24.0
TIER_SLOW_HOURS = 48.0
INTERVAL_FAST = 3600.0
INTERVAL_SLOW = 10800.0

#: 默认话术（不调 LLM，零成本；可在配置里覆盖）
DEFAULT_LINES = (
    "有人在吗，我在这儿闲着",
    "突然冒个泡，今天群里好安静",
    "我还在，有事喊我",
    "路过一下，顺便看看大家在聊什么",
    "有点无聊，想画点什么吗",
)


def in_quiet_hours(hour: int, quiet_start: int = 2, quiet_end: int = 7) -> bool:
    """当前小时是否在「不主动发言」时段（支持跨零点，如 23 → 7）。"""
    h = int(hour) % 24
    qs, qe = int(quiet_start) % 24, int(quiet_end) % 24
    if qs == qe:
        return False
    if qs < qe:
        return qs <= h < qe
    return h >= qs or h < qe


def interval_for(last_mention_ts: float, now_ts: float) -> float:
    """根据「多久没被 @」返回主动发言间隔；0 = 不再主动发言。"""
    if last_mention_ts <= 0:
        return 0.0
    age_hours = (now_ts - last_mention_ts) / 3600.0
    if age_hours < 0:
        age_hours = 0.0
    if age_hours <= TIER_FAST_HOURS:
        return INTERVAL_FAST
    if age_hours <= TIER_SLOW_HOURS:
        return INTERVAL_SLOW
    return 0.0


def should_speak(
    last_mention_ts: float,
    last_spoken_ts: float,
    now_ts: float,
    hour: int,
    quiet_start: int = 2,
    quiet_end: int = 7,
) -> bool:
    """现在该不该在这个群主动发言。"""
    if in_quiet_hours(hour, quiet_start, quiet_end):
        return False
    gap = interval_for(last_mention_ts, now_ts)
    if gap <= 0:
        return False
    if last_spoken_ts <= 0:
        # 从没主动说过：只要在活跃窗口内、且距被 @ 至少一个间隔
        return (now_ts - last_mention_ts) >= gap
    return (now_ts - last_spoken_ts) >= gap


def pick_line(lines=None, rng: random.Random | None = None) -> str:
    """从话术池里挑一句（空池用默认）。"""
    pool = [str(x).strip() for x in (lines or DEFAULT_LINES) if str(x).strip()]
    if not pool:
        pool = list(DEFAULT_LINES)
    r = rng or random
    return r.choice(pool)
