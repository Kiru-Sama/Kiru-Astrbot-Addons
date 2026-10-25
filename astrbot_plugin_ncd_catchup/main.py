# -*- coding: utf-8 -*-
"""链路补台（v2）：只补「真的没人回」的私聊尾巴。

v1 的两个事故（2026-10-22）：
  1. 按 12 小时回溯，把已经回过的旧消息又回了一遍 —— 只看时间，没看后面有没有机器人的回复。
  2. self_id 取不到，把自己的消息当成好友消息灌回去，变成「跟自己做私聊」。

v2 的口径：
  · 处理点（last_ts）**运行期间每 60 秒心跳推进**，所以重启后窗口≈停机时长，不是固定回溯。
  · 每个人只看历史末尾「连续来自对方、且后面没有任何机器人消息」的那一小段；
    已经有一条机器人回复挡在后面的，一律不算未回。
  · 自己发的（sender == 登录号）全部跳过；只认 sender == 好友本人 的消息。
  · 同一段多条合并成一条回复，避免连珠炮。
  · 首次运行（没有处理点）只看最近 10 分钟，绝不回溯半天。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter

try:  # AstrBot 以包形式加载
    from .inject import build_message, extract_text
except ImportError:  # 离线单测
    from inject import build_message, extract_text

OLD_STATE_PATH = Path(__file__).resolve().parent / "data" / "state.json"
# ★ 放 plugin_data：覆盖安装/从仓库同步插件时不会把处理点一起抹掉
STATE_PATH = Path(__file__).resolve().parents[2] / "plugin_data" / "ncd_catchup" / "state.json"


class NcdCatchupPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf: dict[str, Any] = config or {}
        self._task: asyncio.Task | None = None
        self._beat: asyncio.Task | None = None
        self._catching_up = False
        self._self_id: str = ""
        self._manual_last: dict[str, float] = {}

    # ------------------------------------------------------------------
    def _conf_bool(self, key: str, default: bool) -> bool:
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")

    def _conf_int(self, key: str, default: int) -> int:
        raw = self._conf.get(key)
        try:
            return int(raw) if raw not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def _load_state(self) -> dict:
        try:
            for path in (STATE_PATH, OLD_STATE_PATH):
                if path.exists():
                    return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[catchup] 读状态失败（按空处理）: {exc}")
        return {}

    def _save_state(self, state: dict) -> None:
        tmp = STATE_PATH.with_suffix(".tmp")
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, STATE_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[catchup] 写状态失败: {exc}")
            try:
                tmp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _blacklist(self) -> set[str]:
        raw = str(self._conf.get("catchup_blacklist") or "")
        return {x for x in re.split(r"[\s,，;；]+", raw) if x}

    def _skip_names(self) -> tuple[str, ...]:
        raw = str(
            self._conf.get("catchup_skip_names")
            or "我的电脑,文件传输助手,QQ团队,QQ安全中心,QQ空间"
        )
        return tuple(x.strip() for x in re.split(r"[,，;；]+", raw) if x.strip())

    def _friend_skipped(
        self, friend: dict, me: str, blocked: set[str], skip_names: tuple[str, ...]
    ) -> bool:
        uid = str(friend.get("user_id") or "")
        if not uid or uid == me or uid in blocked:
            return True
        name = str(friend.get("nickname") or friend.get("remark") or "")
        return any(word in name for word in skip_names)

    @filter.command("补台", alias={"catchup"})
    async def catchup_now(
        self, event: AstrMessageEvent, minutes: str = ""
    ) -> None:
        """手动补一次私聊：/补台 [分钟]，默认最近 30 分钟，仅管理员。"""
        if str(getattr(event, "role", "")) != "admin":
            self._reply(event, "只有管理员能手动补台。")
            return
        if self._catching_up:
            self._reply(event, "已经有一轮补台在跑，等它结束。")
            return
        try:
            default_minutes = max(1, min(240, self._conf_int("catchup_manual_minutes", 30)))
            mins = int(minutes) if str(minutes).strip() else default_minutes
        except (TypeError, ValueError):
            mins = 30
        mins = max(1, min(240, mins))
        self._catching_up = True
        try:
            result = await self._catch_up_inner(manual_minutes=mins)
        finally:
            self._catching_up = False
        if not result.get("ok"):
            self._reply(event, f"补台失败：{result.get('error') or '未知原因'}")
            return
        self._reply(
            event,
            f"🩹 手动补台完成（最近 {mins} 分钟）："
            f"扫描 {result.get('scanned', 0)} 个私聊，补了 {result.get('injected', 0)} 个。"
            + (
                f"\n跳过黑名单/系统会话 {result.get('skipped', 0)} 个。"
                if result.get("skipped")
                else ""
            ),
        )

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False))

    # ------------------------------------------------------------------
    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        if not self._conf_bool("catchup_enable", True):
            logger.info("[catchup] 已关闭（catchup_enable=false）")
            return
        delay = max(3, self._conf_int("catchup_delay", 20))
        logger.info(f"[catchup] {delay} 秒后检查停机期间的私聊…")
        self._task = asyncio.create_task(self._delayed_run(delay))
        self._beat = asyncio.create_task(self._heartbeat())

    async def _delayed_run(self, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            await self._catch_up()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[catchup] 补回车失败: {type(exc).__name__}: {exc}", exc_info=True)

    async def _heartbeat(self) -> None:
        """运行期间每 60 秒推进处理点 —— 停机后窗口 = 停机时长，而不是固定回溯。"""
        while True:
            try:
                await asyncio.sleep(60)
                if self._catching_up:
                    continue  # 补台正在跑，别把处理点推过去
                state = self._load_state()
                state["last_ts"] = int(time.time())
                self._save_state(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[catchup] 心跳失败（忽略）: {exc}")

    # ------------------------------------------------------------------
    async def _resolve_self_id(self, bot) -> str:
        """登录号：优先问协议端 get_login_info，拿不到再用 bot.self_id。"""
        try:
            info = await bot.call_action("get_login_info")
            uid = str((info or {}).get("user_id") or "")
            if uid:
                return uid
        except Exception:  # noqa: BLE001
            pass
        return str(getattr(bot, "self_id", "") or "")

    @staticmethod
    def _trailing_unanswered(messages: list[dict], uid: str, me: str) -> list[dict]:
        """从历史末尾往前收集：连续来自 uid、且后面没有机器人消息的那几条。"""
        run: list[dict] = []
        for m in reversed(messages):
            sender = str((m.get("sender") or {}).get("user_id") or m.get("user_id") or "")
            if me and sender == me:
                break  # 已经有一条机器人消息挡在后面 → 这段回过了
            if sender != uid:
                break
            run.append(m)
        run.reverse()
        return run

    async def _catch_up(self) -> None:
        self._catching_up = True
        try:
            await self._catch_up_inner()
        finally:
            self._catching_up = False

    async def _catch_up_inner(self, manual_minutes: int | None = None) -> dict:
        manual = manual_minutes is not None
        platform = self.context.get_platform("aiocqhttp")
        if platform is None:
            logger.warning("[catchup] 没找到 aiocqhttp 平台，跳过")
            return {"ok": False, "error": "没找到 aiocqhttp 平台"}
        try:
            bot = platform.get_client()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[catchup] 拿不到 OneBot 客户端: {exc}")
            return {"ok": False, "error": f"拿不到 OneBot 客户端：{exc}"}

        me = await self._resolve_self_id(bot)
        self._self_id = me
        if not me:
            logger.warning("[catchup] 拿不到登录号，为安全起见这轮不补")
            return {"ok": False, "error": "拿不到登录号，已拒绝补台"}

        now = int(time.time())
        if manual:
            minutes = max(1, min(240, int(manual_minutes or 30)))
            last_ts = now - minutes * 60
            logger.info(f"[catchup] 手动补台：最近 {minutes} 分钟")
        else:
            state = self._load_state()
            has_point = "last_ts" in state and state["last_ts"]
            if has_point:
                last_ts = int(state["last_ts"])
            else:
                minutes = max(1, self._conf_int("catchup_first_minutes", 10))
                last_ts = now - minutes * 60
                logger.info(f"[catchup] 首次运行，只看最近 {minutes} 分钟")
        logger.info(
            f"[catchup] 登录号 {me}，处理点 {time.strftime('%m-%d %H:%M:%S', time.localtime(last_ts))}"
        )

        try:
            friends = await bot.call_action("get_friend_list")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[catchup] 拉好友列表失败（协议端可能还没连上）: {exc}")
            return {"ok": False, "error": f"拉好友列表失败：{exc}"}
        if not isinstance(friends, list):
            logger.warning(f"[catchup] 好友列表格式异常: {type(friends).__name__}")
            return {"ok": False, "error": "好友列表格式异常"}

        max_friends = max(1, self._conf_int("catchup_max_friends", 30))
        count = max(1, self._conf_int("catchup_count", 20))
        gap = max(0.0, float(self._conf_int("catchup_gap_ms", 1500)) / 1000.0)
        max_rounds = max(
            1,
            self._conf_int(
                "catchup_manual_max" if manual else "catchup_max_rounds",
                3 if manual else 10,
            ),
        )
        cooldown = max(0, self._conf_int("catchup_manual_cooldown", 600))
        blocked = self._blacklist() if manual else set()
        skip_names = self._skip_names() if manual else ()

        injected = 0
        scanned = 0
        skipped = 0
        for friend in friends[:max_friends]:
            uid = str(friend.get("user_id") or "")
            if self._friend_skipped(friend, me, blocked, skip_names):
                skipped += 1
                continue
            if manual and uid in self._manual_last and now - self._manual_last[uid] < cooldown:
                skipped += 1
                continue
            scanned += 1
            try:
                hist = await bot.call_action("get_friend_msg_history", user_id=int(uid), count=count)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[catchup] 取 {uid} 历史失败（跳过）: {exc}")
                continue
            messages = (hist or {}).get("messages") or []
            if not messages:
                continue
            run = self._trailing_unanswered(messages, uid, me)
            run = [m for m in run if int(m.get("time") or 0) > last_ts]
            if not run:
                continue
            text = "\n".join(t for t in (extract_text(m) for m in run) if t).strip()
            if not text:
                continue
            last = run[-1]
            try:
                msg = build_message(
                    self_id=me,
                    user_id=uid,
                    nickname=str((last.get("sender") or {}).get("nickname") or uid),
                    text=text,
                    message_id=str(last.get("message_id") or ""),
                    timestamp=int(last.get("time") or now),
                )
                platform.commit_event(platform.create_event(msg))
                injected += 1
                if manual:
                    self._manual_last[uid] = now
                    if len(self._manual_last) > 200:
                        for old_uid in sorted(self._manual_last, key=self._manual_last.get)[:-100]:
                            self._manual_last.pop(old_uid, None)
                logger.info(f"[catchup] 补 {uid}（{len(run)} 条合并）：{text[:40]}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[catchup] 补 {uid} 失败: {type(exc).__name__}: {exc}")
            if injected >= max_rounds:
                logger.info(f"[catchup] 达到单次补台上限 {max_rounds}，剩下的下次再说")
                break
            await asyncio.sleep(gap)

        if manual:
            logger.info(f"[catchup] 手动补台完成：扫描 {scanned}，补 {injected}，跳过 {skipped}")
            return {
                "ok": True,
                "injected": injected,
                "scanned": scanned,
                "skipped": skipped,
                "since": last_ts,
                "manual": True,
            }

        state["last_ts"] = now
        state["last_run"] = now
        self._save_state(state)
        logger.info(
            f"[catchup] 完成：补了 {injected} 个私聊"
            if injected
            else "[catchup] 停机期间没有需要补的私聊"
        )
        return {"ok": True, "injected": injected, "scanned": scanned, "skipped": skipped}

    async def terminate(self) -> None:
        for task in (self._task, self._beat):
            if task and not task.done():
                task.cancel()
