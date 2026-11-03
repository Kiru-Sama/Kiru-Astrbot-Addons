# -*- coding: utf-8 -*-
"""群聊主动发言的判定逻辑（纯函数，可离线测）。

用户定的规则（2026-11-03 更新）：
  · 24 小时内有人主动 @ 过 bot 的群 → 每次间隔随机 10~90 分钟
  · 24~48 小时内被 @ 过的群       → 每次间隔随机 180~240 分钟
  · 超过 48 小时没被 @ 的群       → 不主动发言
  · 凌晨 2:00~7:00 一律不主动发言

"不像机器人"的判断（参考 smart_wake 的两级漏斗）：
  · 第一级：随机间隔到期（本模块）
  · 第二级：到期后看群聊上下文，由 LLM 决定「说不说、说什么」（main.py）
  · 不打断：群里最近 N 分钟有人说话 → 这次跳过（顺延到下一轮）
  · 不重复：挑话术时避开最近说过的几句

状态（每个群的「最后被 @ 时间」「下次允许时间」）由调用方存盘。
"""

from __future__ import annotations

import random

#: 两个档位的活跃窗口
TIER_FAST_HOURS = 24.0
TIER_SLOW_HOURS = 48.0

#: 24h 档：10~90 分钟；48h 档：180~240 分钟
FAST_GAP_RANGE = (600.0, 5400.0)
SLOW_GAP_RANGE = (10800.0, 14400.0)

#: 群里最近这么多秒内有人说话，就不要插嘴
DEFAULT_MIN_GAP_SEC = 600.0

#: 默认话术（LLM 不可用时的兜底；可在配置里覆盖）
DEFAULT_LINES = (
    "有人在吗，我在这儿闲着",
    "突然冒个泡，今天群里好安静",
    "我还在，有事喊我",
    "路过一下，顺便看看大家在聊什么",
    "有点无聊，想画点什么吗",
    "（悄悄探个头）今天没人聊天吗",
    "刚睡醒，脑子还是糊的",
    "群里的空气好安静，我都不敢出声",
    "有没有人想看我画点什么",
    "我在这里趴一会儿，有事戳我",
    "刚才想说话，又忘了要说什么",
    "刷到一半停下来了，想看看群里有没有热闹",
    "今天也是安静的一天呢",
    "我是不是有点太安静了",
    "有人来陪我说句话吗，就一句",
    "（尾巴晃了两下）",
    "不知道大家现在在干嘛",
    "路过路过，不打扰你们",
    "想听点新鲜事，谁来讲讲",
    "我又来了，这次不吵你们",
    "群里静悄悄的，我数了三只羊",
    "有人吗？没有的话我继续发呆",
    "今天心情不错，想多说两句",
    "不小心又趴下了，起来看看",
    "刚画完一张，手有点酸",
    "有没有人想聊点别的",
    "我在这里，不吵不闹",
    "大家今天都忙吗",
    "冒个泡，证明我还在",
    "安静得能听见我自己的呼吸",
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


def tier_for(last_mention_ts: float, now_ts: float) -> str:
    """'fast'（24h 内）/ 'slow'（24~48h）/ ''（不再发言）。"""
    if last_mention_ts <= 0:
        return ""
    age_hours = max(0.0, (now_ts - last_mention_ts) / 3600.0)
    if age_hours <= TIER_FAST_HOURS:
        return "fast"
    if age_hours <= TIER_SLOW_HOURS:
        return "slow"
    return ""


def next_gap(
    last_mention_ts: float,
    now_ts: float,
    fast_range: tuple[float, float] = FAST_GAP_RANGE,
    slow_range: tuple[float, float] = SLOW_GAP_RANGE,
    rng: random.Random | None = None,
) -> float:
    """下一次主动发言的间隔（秒）。0 = 不再发言；分档后在该档范围内随机。"""
    tier = tier_for(last_mention_ts, now_ts)
    if not tier:
        return 0.0
    low, high = fast_range if tier == "fast" else slow_range
    low, high = max(0.0, float(low)), max(0.0, float(high))
    if high < low:
        low, high = high, low
    r = rng or random
    return r.uniform(low, high)


def should_speak(
    next_at_ts: float,
    now_ts: float,
    hour: int,
    quiet_start: int = 2,
    quiet_end: int = 7,
    recent_activity_ts: float = 0.0,
    min_gap_sec: float = DEFAULT_MIN_GAP_SEC,
) -> bool:
    """现在该不该进入「要不要说话」的判定。

    next_at_ts: 上次发言/跳过时排定的下次允许时间（0 = 还没排）
    recent_activity_ts: 这个群最近一条消息的时间（0 = 不知道）
    min_gap_sec: 最近这么多秒内有人说话就不插嘴
    """
    if in_quiet_hours(hour, quiet_start, quiet_end):
        return False
    if next_at_ts <= 0 or now_ts < next_at_ts:
        return False
    if recent_activity_ts and min_gap_sec > 0:
        if (now_ts - float(recent_activity_ts)) < float(min_gap_sec):
            return False  # 群里正热闹，别插嘴
    return True


def pick_line(
    lines=None,
    recent=None,
    rng: random.Random | None = None,
) -> str:
    """从话术池里挑一句；尽量避开最近说过的几句。"""
    pool = [str(x).strip() for x in (lines or DEFAULT_LINES) if str(x).strip()]
    if not pool:
        pool = list(DEFAULT_LINES)
    recent_set = {str(x).strip() for x in (recent or []) if str(x).strip()}
    fresh = [x for x in pool if x not in recent_set]
    r = rng or random
    return r.choice(fresh or pool)


def parse_decision(raw: str) -> str:
    """解析 LLM 的决定：'SKIP' / 空 → 不说话；否则返回要说的话。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    head = text.lstrip("：:，,。.！! \n\t").upper()
    if head.startswith("SKIP") or head.startswith("不说") or head.startswith("跳过"):
        return ""
    # 去掉模型可能加的引号/前缀
    for prefix in ("发言：", "发言:", "说话：", "说话:", "回复：", "回复:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    text = text.strip().strip('"').strip("“”").strip()
    return text[:120]
