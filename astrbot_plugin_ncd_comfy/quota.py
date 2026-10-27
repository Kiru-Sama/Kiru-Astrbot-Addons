# -*- coding: utf-8 -*-
"""绘图无感限流的纯逻辑层。

不依赖 AstrBot / ComfyUI，方便离线测试。口径（2026-10-26 与 Kirusama 确认）：

- 只统计私聊成功送达的图；群聊、管理员、失败/取消/超时都不计数。
- 窗口按北京时间 08:00 切；中国无夏令时，用固定 +8。
- “超过”一律是严格大于：30 不触发、31 触发；50 不降质、51 降质；70 不封、71 封。
- 限流池/画幅池用单日榜；同分在位者优先，只有严格超过才替换。
- 30 是触发线，不是硬上限：触发后只限前 5 名，第 6 名以后照常画。
- 50/70 只作用私聊；群聊有审核，按产品口径不参与额度。
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone

# 中国没有夏令时，固定 +8 比 ZoneInfo 更稳（Windows 上不一定有 tzdata）。
WINDOW_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_WINDOW_HOUR = 8

__all__ = [
    "DEFAULT_WINDOW_HOUR",
    "WINDOW_TZ",
    "QueueDecision",
    "QuotaConfig",
    "QuotaEngine",
    "QuotaSnapshot",
    "QuotaState",
    "RankEntry",
    "RecordResult",
    "format_duration",
    "format_local",
    "format_window_label",
    "window_key",
    "window_start",
]


def window_start(ts: float, hour: int = DEFAULT_WINDOW_HOUR) -> int:
    """返回 ts 所在统计窗口的起点（最近一个 hour:00）的 epoch 秒。"""
    hour = int(hour)
    if not 0 <= hour <= 23:
        raise ValueError(f"window hour 必须在 0~23: {hour!r}")
    local = datetime.fromtimestamp(float(ts), WINDOW_TZ)
    shifted = local - timedelta(hours=hour)
    start = datetime.combine(shifted.date(), dtime(hour=hour), tzinfo=WINDOW_TZ)
    return int(start.timestamp())


def window_key(ts: float, hour: int = DEFAULT_WINDOW_HOUR) -> str:
    """窗口标识，用窗口起点那天的日期，例如 2026-10-26。"""
    start = window_start(ts, hour)
    return datetime.fromtimestamp(start, WINDOW_TZ).strftime("%Y-%m-%d")


def format_local(ts: float, fmt: str = "%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(float(ts), WINDOW_TZ).strftime(fmt)


def format_window_label(ts: float, hour: int = DEFAULT_WINDOW_HOUR) -> str:
    start = window_start(ts, hour)
    return f"{format_local(start)} → {format_local(start + 86400)}"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时"
    if minutes:
        return f"{minutes} 分钟"
    return f"{seconds} 秒"


@dataclass
class QuotaConfig:
    """阈值都放在这里，方便面板/配置热更新；默认值就是方案里的数字。"""

    window_hour: int = DEFAULT_WINDOW_HOUR
    global_enabled: bool = True
    global_limit: int = 30
    throttle_top: int = 5
    canvas_top: int = 3
    canvas_max_side: int = 1080
    queue_limited: int = 1
    queue_normal: int = 3
    interval_base: int = 120
    interval_step: int = 10
    degrade_after: int = 50
    degrade_steps: int = 20
    ban_after: int = 70
    ban_hours: int = 36
    bypass_ids: tuple[str, ...] = ()
    counted_jobs_limit: int = 2000

    def __post_init__(self) -> None:
        raw = self.bypass_ids or ()
        if isinstance(raw, str):
            raw = (raw,)
        self.bypass_ids = tuple(str(x) for x in raw)


@dataclass
class QuotaState:
    """可 JSON 化的状态；禁生图和累计张数不随日窗口清空。"""

    window_start: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    total_counts: dict[str, int] = field(default_factory=dict)
    last_success: dict[str, float] = field(default_factory=dict)
    last_end: dict[str, float] = field(default_factory=dict)
    throttle_pool: list[str] = field(default_factory=list)
    canvas_pool: list[str] = field(default_factory=list)
    reminded: set[str] = field(default_factory=set)
    counted_jobs: list[str] = field(default_factory=list)
    counted_set: set[str] = field(default_factory=set, repr=False, compare=False)
    bans: dict[str, float] = field(default_factory=dict)
    # 发言统计：日计数、累计、最后一次发言时间（同分先到先排）
    speech_counts: dict[str, int] = field(default_factory=dict)
    speech_totals: dict[str, int] = field(default_factory=dict)
    speech_last: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "window_start": int(self.window_start),
            "counts": {str(k): int(v) for k, v in self.counts.items()},
            "total_counts": {str(k): int(v) for k, v in self.total_counts.items()},
            "last_success": {str(k): float(v) for k, v in self.last_success.items()},
            "last_end": {str(k): float(v) for k, v in self.last_end.items()},
            "throttle_pool": [str(x) for x in self.throttle_pool],
            "canvas_pool": [str(x) for x in self.canvas_pool],
            "reminded": sorted(str(x) for x in self.reminded),
            "counted_jobs": [str(x) for x in self.counted_jobs],
            "bans": {str(k): float(v) for k, v in self.bans.items()},
            "speech_counts": {
                str(k): int(v) for k, v in self.speech_counts.items()
            },
            "speech_totals": {
                str(k): int(v) for k, v in self.speech_totals.items()
            },
            "speech_last": {
                str(k): float(v) for k, v in self.speech_last.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "QuotaState":
        data = data or {}
        jobs = [str(x) for x in (data.get("counted_jobs") or [])]
        return cls(
            window_start=int(data.get("window_start") or 0),
            counts={str(k): int(v) for k, v in (data.get("counts") or {}).items()},
            total_counts={
                str(k): int(v) for k, v in (data.get("total_counts") or {}).items()
            },
            last_success={
                str(k): float(v) for k, v in (data.get("last_success") or {}).items()
            },
            last_end={str(k): float(v) for k, v in (data.get("last_end") or {}).items()},
            throttle_pool=[str(x) for x in (data.get("throttle_pool") or [])],
            canvas_pool=[str(x) for x in (data.get("canvas_pool") or [])],
            reminded={str(x) for x in (data.get("reminded") or [])},
            counted_jobs=jobs,
            counted_set=set(jobs),
            bans={str(k): float(v) for k, v in (data.get("bans") or {}).items()},
            speech_counts={
                str(k): int(v)
                for k, v in (data.get("speech_counts") or {}).items()
            },
            speech_totals={
                str(k): int(v)
                for k, v in (data.get("speech_totals") or {}).items()
            },
            speech_last={
                str(k): float(v)
                for k, v in (data.get("speech_last") or {}).items()
            },
        )


@dataclass(frozen=True)
class RecordResult:
    counted: bool
    duplicate: bool
    daily_count: int
    total_count: int
    reminder_due: bool
    ban_triggered: bool
    ban_until: float | None
    degraded: bool


@dataclass(frozen=True)
class QuotaSnapshot:
    """一次“出队/查询”的快照；运行中的任务不再看新值。"""

    user_id: str
    private: bool
    admin: bool
    daily_count: int
    total_count: int
    rank: int | None
    global_count: int
    global_limit: int
    global_enabled: bool
    global_triggered: bool
    throttled: bool
    interval_seconds: int
    spacing_remaining: int
    queue_cap: int | None
    canvas_max_side: int | None
    degraded: bool
    banned: bool
    ban_until: float | None


@dataclass(frozen=True)
class QueueDecision:
    allowed: bool
    reason: str
    cap: int | None
    snapshot: QuotaSnapshot


@dataclass(frozen=True)
class RankEntry:
    """面板/审计用的日榜一行；排行按名次排好。"""

    user_id: str
    rank: int
    count: int
    total_count: int
    last_success: float | None
    throttled: bool
    canvas_limited: bool
    degraded: bool
    banned: bool
    ban_until: float | None
    interval_seconds: int
    queue_cap: int | None


class QuotaEngine:
    """日榜 + 前 5/前 3 池 + 50/70 的纯状态机。"""

    def __init__(
        self,
        config: QuotaConfig | None = None,
        state: QuotaState | None = None,
        now_fn=None,
    ) -> None:
        self.config = config or QuotaConfig()
        self.state = state or QuotaState()
        self.now_fn = now_fn or time.time
        now = float(self.now_fn())
        self._ensure_window(now)

    # ------------------------------------------------------------------
    #  时间 / 窗口
    # ------------------------------------------------------------------
    def _now(self, now: float | None) -> float:
        return float(self.now_fn() if now is None else now)

    def _ensure_window(self, now: float) -> None:
        ws = window_start(now, self.config.window_hour)
        if self.state.window_start != ws:
            # 换窗口：日榜、池、提醒、幂等都清；禁生图和累计张数保留。
            self.state.window_start = ws
            self.state.counts.clear()
            self.state.last_success.clear()
            self.state.last_end.clear()
            self.state.throttle_pool.clear()
            self.state.canvas_pool.clear()
            self.state.reminded.clear()
            self.state.counted_jobs.clear()
            self.state.counted_set.clear()
            self.state.speech_counts.clear()
            self.state.speech_last.clear()
        self._prune_bans(now)
        # 这里顺带重建池：配置开关、禁生图到期、读档后都能保持一致。
        self._maintain_pools(now)

    def _prune_bans(self, now: float) -> None:
        expired = [u for u, until in self.state.bans.items() if until <= now]
        for user_id in expired:
            self.state.bans.pop(user_id, None)

    def current_window_key(self, now: float | None = None) -> str:
        return window_key(self._now(now), self.config.window_hour)

    def current_window_label(self, now: float | None = None) -> str:
        return format_window_label(self._now(now), self.config.window_hour)

    # ------------------------------------------------------------------
    #  排名 / 池
    # ------------------------------------------------------------------
    def _is_banned(self, user_id: str, now: float) -> bool:
        until = self.state.bans.get(user_id)
        return bool(until and until > now)

    def _order(self, previous: list[str], users: set[str]) -> list[str]:
        """按（张数降序，在位者优先，同分更早达到优先，id）排序。

        在位者优先只对“张数相同”生效：张数更高的挑战者仍然排前面。
        """
        counts = self.state.counts
        last = self.state.last_success
        prev = [u for u in dict.fromkeys(previous) if u in users]
        pos = {u: i for i, u in enumerate(prev)}

        def key(user_id: str):
            return (
                -int(counts.get(user_id, 0)),
                0 if user_id in pos else 1,
                pos.get(user_id, float(last.get(user_id, 0.0))),
                user_id,
            )

        return sorted(users, key=key)

    def _maintain_pool(
        self, previous: list[str], size: int, now: float
    ) -> list[str]:
        if size <= 0:
            return []
        eligible = {
            u
            for u, count in self.state.counts.items()
            if count > 0 and not self._is_banned(u, now)
        }
        return self._order(previous, eligible)[:size]

    def _pools_active(self) -> bool:
        """30 是触发线：只有严格超过才让池生效；关闭时池也清空。"""
        if not self.config.global_enabled:
            return False
        return sum(int(v) for v in self.state.counts.values()) > int(
            self.config.global_limit
        )

    def _maintain_pools(self, now: float) -> None:
        if not self._pools_active():
            # 没触发时不存在“在位者”，触发那一刻按当时的到达时间重新建池。
            self.state.throttle_pool = []
            self.state.canvas_pool = []
            return
        self.state.throttle_pool = self._maintain_pool(
            self.state.throttle_pool, int(self.config.throttle_top), now
        )
        self.state.canvas_pool = self._maintain_pool(
            self.state.canvas_pool, int(self.config.canvas_top), now
        )

    def rebuild_pools(self, now: float | None = None) -> tuple[list[str], list[str]]:
        """从计数重建池（读档/测试用），在位关系按传入的旧池保留。"""
        now = self._now(now)
        self._ensure_window(now)
        return list(self.state.throttle_pool), list(self.state.canvas_pool)

    def _rank_map(self) -> dict[str, int]:
        # 排名展示包含被禁生图的人（他们还在日榜上），只是不进池。
        users = {u for u, count in self.state.counts.items() if count > 0}
        order = self._order(self.state.throttle_pool, users)
        return {u: i + 1 for i, u in enumerate(order)}

    def _interval_for(self, user_id: str) -> int:
        if user_id not in self.state.throttle_pool:
            return 0
        n = self.state.throttle_pool.index(user_id) + 1
        return max(
            0,
            int(self.config.interval_base) - n * int(self.config.interval_step),
        )

    def ranking(self, now: float | None = None) -> list[RankEntry]:
        """日榜（含被禁生图的人），按名次升序；面板和审计用。"""
        now = self._now(now)
        self._ensure_window(now)
        active = self._pools_active()
        rows: list[RankEntry] = []
        for user_id, rank in sorted(self._rank_map().items(), key=lambda kv: kv[1]):
            count = int(self.state.counts.get(user_id, 0))
            banned = self._is_banned(user_id, now)
            throttled = bool(
                active and not banned and user_id in self.state.throttle_pool
            )
            canvas = bool(
                active and not banned and user_id in self.state.canvas_pool
            )
            rows.append(
                RankEntry(
                    user_id=user_id,
                    rank=rank,
                    count=count,
                    total_count=int(self.state.total_counts.get(user_id, 0)),
                    last_success=self.state.last_success.get(user_id),
                    throttled=throttled,
                    canvas_limited=canvas,
                    degraded=count > int(self.config.degrade_after),
                    banned=banned,
                    ban_until=(self.state.bans.get(user_id) if banned else None),
                    interval_seconds=(
                        self._interval_for(user_id) if throttled else 0
                    ),
                    queue_cap=(
                        0
                        if banned
                        else (
                            int(self.config.queue_limited)
                            if throttled
                            else int(self.config.queue_normal)
                        )
                    ),
                )
            )
        return rows

    def dashboard(self, now: float | None = None) -> dict:
        """面板一次取全：窗口、全局、池、日榜、禁生图名单。"""
        now = self._now(now)
        self._ensure_window(now)
        count = sum(int(v) for v in self.state.counts.values())
        limit = int(self.config.global_limit)
        percent = (
            min(100, int(round(count * 100 / limit))) if limit > 0 else 0
        )
        bans = [
            {
                "user_id": user_id,
                "until": until,
                "remaining": max(0, int(until - now)),
            }
            for user_id, until in sorted(
                self.state.bans.items(), key=lambda kv: kv[1]
            )
        ]
        return {
            "window": {
                "start": self.state.window_start,
                "end": self.state.window_start + 86400,
                "key": window_key(now, self.config.window_hour),
                "label": format_window_label(now, self.config.window_hour),
                "remaining": max(
                    0, self.state.window_start + 86400 - int(now)
                ),
            },
            "global": {
                "count": count,
                "limit": limit,
                "enabled": bool(self.config.global_enabled),
                "triggered": self._pools_active(),
                "percent": percent,
            },
            "pools": {
                "throttle": list(self.state.throttle_pool),
                "canvas": list(self.state.canvas_pool),
            },
            "ranking": [asdict(row) for row in self.ranking(now)],
            "bans": bans,
            "totals": {
                "users": len(self.state.total_counts),
                "images": sum(int(v) for v in self.state.total_counts.values()),
            },
        }

    # ------------------------------------------------------------------
    #  记录
    # ------------------------------------------------------------------
    def _remember_job(self, job_id: str) -> None:
        if not job_id:
            return
        self.state.counted_jobs.append(job_id)
        self.state.counted_set.add(job_id)
        limit = max(0, int(self.config.counted_jobs_limit))
        while len(self.state.counted_jobs) > limit:
            old = self.state.counted_jobs.pop(0)
            self.state.counted_set.discard(old)

    def record_success(
        self,
        user_id: str,
        job_id: str = "",
        *,
        private: bool = True,
        admin: bool = False,
        now: float | None = None,
    ) -> RecordResult:
        """图已成功送达后调用；只有私聊、非管理员的成功才计数。"""
        now = self._now(now)
        self._ensure_window(now)
        user_id = str(user_id or "")
        job_id = str(job_id or "")
        if not user_id:
            return RecordResult(
                counted=False,
                duplicate=False,
                daily_count=0,
                total_count=0,
                reminder_due=False,
                ban_triggered=False,
                ban_until=None,
                degraded=False,
            )
        bypass = bool(admin or user_id in self.config.bypass_ids)
        daily = int(self.state.counts.get(user_id, 0))
        total = int(self.state.total_counts.get(user_id, 0))

        if bypass or not private:
            return RecordResult(
                counted=False,
                duplicate=False,
                daily_count=daily,
                total_count=total,
                reminder_due=False,
                ban_triggered=False,
                ban_until=None,
                degraded=False,
            )

        if job_id and job_id in self.state.counted_set:
            return RecordResult(
                counted=False,
                duplicate=True,
                daily_count=daily,
                total_count=total,
                reminder_due=False,
                ban_triggered=False,
                ban_until=self.state.bans.get(user_id),
                degraded=daily > int(self.config.degrade_after),
            )

        daily += 1
        total += 1
        self.state.counts[user_id] = daily
        self.state.total_counts[user_id] = total
        self.state.last_success[user_id] = now
        self._remember_job(job_id)
        self._maintain_pools(now)

        degraded = daily > int(self.config.degrade_after)
        reminder_due = bool(degraded and user_id not in self.state.reminded)
        if reminder_due:
            self.state.reminded.add(user_id)

        ban_until = self.state.bans.get(user_id)
        ban_triggered = False
        if daily > int(self.config.ban_after) and not (ban_until and ban_until > now):
            ban_until = now + max(0, int(self.config.ban_hours)) * 3600
            self.state.bans[user_id] = ban_until
            ban_triggered = True
            # 被禁生图的人不再占限流/画幅名额。
            self._maintain_pools(now)

        return RecordResult(
            counted=True,
            duplicate=False,
            daily_count=daily,
            total_count=total,
            reminder_due=reminder_due,
            ban_triggered=ban_triggered,
            ban_until=ban_until,
            degraded=degraded,
        )

    def record_end(
        self,
        user_id: str,
        *,
        private: bool = True,
        admin: bool = False,
        now: float | None = None,
    ) -> None:
        """任务结束（成功/失败/超时）后调用，用于算下一次的间隔。"""
        now = self._now(now)
        self._ensure_window(now)
        user_id = str(user_id or "")
        if not user_id:
            return
        if admin or not private or user_id in self.config.bypass_ids:
            return
        self.state.last_end[user_id] = now

    def record_speech(
        self,
        user_id: str,
        *,
        admin: bool = False,
        now: float | None = None,
    ) -> int:
        """记一条发言；管理员/白名单不计。返回今日条数。"""
        now = self._now(now)
        self._ensure_window(now)
        user_id = str(user_id or "")
        if not user_id or admin or user_id in self.config.bypass_ids:
            return 0
        daily = int(self.state.speech_counts.get(user_id, 0)) + 1
        self.state.speech_counts[user_id] = daily
        self.state.speech_totals[user_id] = (
            int(self.state.speech_totals.get(user_id, 0)) + 1
        )
        self.state.speech_last[user_id] = now
        return daily

    def speech_stats(
        self, user_id: str, now: float | None = None
    ) -> tuple[int, int, int | None]:
        """返回 (今日条数, 累计条数, 今日名次)；没发言名次为 None。"""
        now = self._now(now)
        self._ensure_window(now)
        user_id = str(user_id or "")
        daily = int(self.state.speech_counts.get(user_id, 0))
        total = int(self.state.speech_totals.get(user_id, 0))
        if daily <= 0:
            return 0, total, None
        users = [u for u, c in self.state.speech_counts.items() if c > 0]
        users.sort(
            key=lambda u: (
                -int(self.state.speech_counts.get(u, 0)),
                float(self.state.speech_last.get(u, 0.0)),
                u,
            )
        )
        try:
            rank = users.index(user_id) + 1
        except ValueError:
            rank = None
        return daily, total, rank

    # ------------------------------------------------------------------
    #  快照 / 准入
    # ------------------------------------------------------------------
    def snapshot(
        self,
        user_id: str,
        *,
        private: bool = True,
        admin: bool = False,
        now: float | None = None,
    ) -> QuotaSnapshot:
        now = self._now(now)
        self._ensure_window(now)
        user_id = str(user_id)
        bypass = bool(admin or user_id in self.config.bypass_ids)

        daily = int(self.state.counts.get(user_id, 0))
        total = int(self.state.total_counts.get(user_id, 0))
        rank = None if bypass else self._rank_map().get(user_id)

        global_count = sum(int(v) for v in self.state.counts.values())
        triggered = bool(
            self.config.global_enabled
            and global_count > int(self.config.global_limit)
        )

        ban_until = self.state.bans.get(user_id)
        banned = bool(
            private and not bypass and ban_until and ban_until > now
        )
        throttled = bool(
            private
            and not bypass
            and not banned
            and triggered
            and user_id in self.state.throttle_pool
        )
        canvas = bool(
            private
            and not bypass
            and not banned
            and triggered
            and user_id in self.state.canvas_pool
        )

        interval = self._interval_for(user_id) if throttled else 0

        spacing = 0
        if interval:
            last_end = float(self.state.last_end.get(user_id, 0.0))
            if last_end >= self.state.window_start:
                spacing = max(0, int(math.ceil(last_end + interval - now)))

        if bypass or not private:
            queue_cap = None
        elif banned:
            queue_cap = 0
        elif throttled:
            queue_cap = int(self.config.queue_limited)
        else:
            queue_cap = int(self.config.queue_normal)

        return QuotaSnapshot(
            user_id=user_id,
            private=private,
            admin=bypass,
            daily_count=daily,
            total_count=total,
            rank=rank,
            global_count=global_count,
            global_limit=int(self.config.global_limit),
            global_enabled=bool(self.config.global_enabled),
            global_triggered=triggered,
            throttled=throttled,
            interval_seconds=interval,
            spacing_remaining=spacing,
            queue_cap=queue_cap,
            canvas_max_side=(
                int(self.config.canvas_max_side) if canvas else None
            ),
            degraded=bool(
                private
                and not bypass
                and daily > int(self.config.degrade_after)
            ),
            banned=banned,
            ban_until=(ban_until if banned else None),
        )

    def check_enqueue(
        self,
        user_id: str,
        pending: int,
        *,
        private: bool = True,
        admin: bool = False,
        now: float | None = None,
    ) -> QueueDecision:
        """提交时只查队列上限；间隔等到出队才查，避免提交被无谓拒绝。"""
        snap = self.snapshot(user_id, private=private, admin=admin, now=now)
        pending = max(0, int(pending))
        if snap.banned:
            return QueueDecision(False, "banned", 0, snap)
        if snap.queue_cap is None:
            return QueueDecision(True, "bypass", None, snap)
        if pending >= snap.queue_cap:
            return QueueDecision(False, "queue_full", snap.queue_cap, snap)
        return QueueDecision(True, "ok", snap.queue_cap, snap)

    # ------------------------------------------------------------------
    #  持久化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return self.state.to_dict()

    @classmethod
    def from_dict(
        cls,
        data: dict | None,
        config: QuotaConfig | None = None,
        now_fn=None,
    ) -> "QuotaEngine":
        return cls(
            config=config,
            state=QuotaState.from_dict(data),
            now_fn=now_fn,
        )
