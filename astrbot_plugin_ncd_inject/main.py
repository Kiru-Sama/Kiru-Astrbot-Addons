# -*- coding: utf-8 -*-
"""对话提示注入：在指定会话的下一轮里，于系统提示词末尾接一句“念头”。

只改当次请求的 `req.system_prompt`，不写进对话历史；整轮（含工具循环）都带，
回复发出后消费掉。句子的格式由用户定死：`嗯，或许我可以{内容}`，不加任何标记。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.web import json_response, request
try:
    from astrbot.core.agent.message import TextPart
except Exception:  # noqa: BLE001
    TextPart = None  # type: ignore[assignment]

PLUGIN_DIR = Path(__file__).resolve().parent
STATE_PATH = PLUGIN_DIR.parents[2] / "data" / "plugin_data" / "ncd_inject" / "state.json"
PREFIX = "嗯，或许我可以"


def _now() -> float:
    return time.time()


class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self._conf: dict = config if isinstance(config, dict) else {}
        self._sessions: dict[str, dict] = {}
        self._pending: dict[str, list[dict]] = {}
        self._load()
        self._register_apis()

    # ---------------- 配置 ----------------
    def _ttl(self) -> float:
        try:
            return max(1, int(self._conf.get("ttl_minutes", 30))) * 60.0
        except Exception:  # noqa: BLE001
            return 1800.0

    def _max_pending(self) -> int:
        try:
            return max(1, int(self._conf.get("max_pending", 5)))
        except Exception:  # noqa: BLE001
            return 5

    def _enabled(self) -> bool:
        raw = self._conf.get("enabled", True)
        if isinstance(raw, str):
            return raw.strip().lower() not in ("false", "0", "no", "off")
        return bool(raw)

    # ---------------- 持久化 ----------------
    def _load(self) -> None:
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                data = json.load(fh) or {}
            self._sessions = dict(data.get("sessions") or {})
            self._pending = {
                k: list(v) for k, v in (data.get("pending") or {}).items()
            }
        except Exception:  # noqa: BLE001
            self._sessions, self._pending = {}, {}

    def _save(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"sessions": self._sessions, "pending": self._pending},
                    fh,
                    ensure_ascii=False,
                )
            os.replace(tmp, STATE_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[inject] 保存状态失败: {exc}")

    # ---------------- 会话记录 ----------------
    @staticmethod
    def _label(event: AstrMessageEvent) -> str:
        try:
            name = event.get_sender_name()
        except Exception:  # noqa: BLE001
            name = ""
        gid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            gid = ""
        if gid:
            try:
                group = getattr(getattr(event, "message_obj", None), "group", None)
                gname = str(getattr(group, "group_name", "") or "")
            except Exception:  # noqa: BLE001
                gname = ""
            return f"{gname or '群' + gid} · {name}".strip(" ·")
        return f"私聊 · {name}".strip(" ·")

    def _track(self, event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return ""
        self._sessions[umo] = {"label": self._label(event), "ts": _now()}
        self._save()
        return umo

    def _prune(self, umo: str) -> None:
        ttl = self._ttl()
        now = _now()
        items = [x for x in (self._pending.get(umo) or []) if now - x.get("ts", 0) < ttl]
        if items:
            self._pending[umo] = items
        else:
            self._pending.pop(umo, None)

    # ---------------- 钩子 ----------------
    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        umo = self._track(event)
        if not umo or not self._enabled():
            return
        self._prune(umo)
        queue = self._pending.get(umo) or []
        if not queue:
            return
        item = queue[0]
        text = f"{PREFIX}{item.get('text', '')}"
        mode = str(self._conf.get("mode", "assistant") or "assistant").strip().lower()
        try:
            if mode == "system":
                base = (req.system_prompt or "").rstrip()
                req.system_prompt = f"{base}\n{text}" if base else text
            elif mode == "user":
                # 紧贴用户消息追加（会被并进用户那条，模型当成用户说的话）
                if TextPart is None:
                    raise RuntimeError("拿不到 TextPart")
                if not getattr(event, "_ncd_inject_user_done", False):
                    req.extra_user_content_parts.append(TextPart(text=text))
                    setattr(event, "_ncd_inject_user_done", True)
            else:
                # assistant：作为一条独立的“她自己说过/想过”的上下文。
                # ★ 先把历史里以前注入过的旧念头剔掉，避免一轮轮累积占 token。
                def _is_old_thought(msg: dict) -> bool:
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        return False
                    c = msg.get("content")
                    if isinstance(c, str):
                        return c.startswith(PREFIX)
                    if isinstance(c, list):
                        return any(
                            isinstance(x, dict)
                            and str(x.get("text") or "").startswith(PREFIX)
                            for x in c
                        )
                    return False

                req.contexts = [m for m in (req.contexts or []) if not _is_old_thought(m)]
                req.contexts.append({"role": "assistant", "content": text})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[inject] 注入失败: {exc}")
            return
        item["injected_at"] = _now()
        logger.info(f"[inject] 已注入({mode}) → {umo}: {text[:40]}")
        self._save()

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        queue = self._pending.get(umo) or []
        if not queue:
            return
        kept = []
        consumed = 0
        for item in queue:
            if item.get("injected_at") and not consumed:
                consumed += 1
                continue
            kept.append(item)
        if kept:
            self._pending[umo] = kept
        else:
            self._pending.pop(umo, None)
        if consumed:
            self._save()
            logger.info(f"[inject] 已消费 {consumed} 条 → {umo}")

    # ---------------- Web API ----------------
    def _register_apis(self) -> None:
        try:
            name = PLUGIN_DIR.name
            self.context.register_web_api(
                f"/{name}/inject/sessions", self.api_sessions, ["GET"], "最近活跃会话"
            )
            self.context.register_web_api(
                f"/{name}/inject/pending", self.api_pending, ["GET"], "待注入 / 已注入"
            )
            self.context.register_web_api(
                f"/{name}/inject/inject", self.api_inject, ["POST"], "注入一条提示"
            )
            self.context.register_web_api(
                f"/{name}/inject/cancel", self.api_cancel, ["POST"], "撤回一条待注入"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[inject] 注册 API 失败: {exc}")

    async def api_sessions(self):
        rows = [
            {"umo": k, "label": v.get("label", ""), "ts": v.get("ts", 0)}
            for k, v in self._sessions.items()
        ]
        rows.sort(key=lambda x: x["ts"], reverse=True)
        return json_response({"items": rows[:50]})

    async def api_pending(self):
        ttl = self._ttl()
        now = _now()
        rows = []
        for umo, items in self._pending.items():
            for it in items:
                rows.append(
                    {
                        "id": it.get("id", ""),
                        "umo": umo,
                        "label": (self._sessions.get(umo) or {}).get("label", ""),
                        "text": it.get("text", ""),
                        "ts": it.get("ts", 0),
                        "injected": bool(it.get("injected_at")),
                        "expires_in": max(0, int(ttl - (now - it.get("ts", now)))),
                    }
                )
        rows.sort(key=lambda x: x["ts"], reverse=True)
        return json_response({"items": rows[:100]})

    async def api_inject(self):
        body = await request.json({}) or {}
        umo = str(body.get("umo") or "").strip()
        text = str(body.get("text") or "").strip()
        if not umo or not text:
            return json_response({"ok": False, "error": "缺少会话或内容"})
        if len(text) > 500:
            text = text[:500]
        self._prune(umo)
        queue = self._pending.setdefault(umo, [])
        if len(queue) >= self._max_pending():
            return json_response({"ok": False, "error": "这个会话排队太多了"})
        queue.append({"id": uuid.uuid4().hex[:8], "text": text, "ts": _now()})
        self._save()
        return json_response({"ok": True, "queued": len(queue)})

    async def api_cancel(self):
        body = await request.json({}) or {}
        target = str(body.get("id") or "").strip()
        for umo in list(self._pending):
            queue = self._pending[umo]
            kept = [x for x in queue if x.get("id") != target]
            if len(kept) != len(queue):
                if kept:
                    self._pending[umo] = kept
                else:
                    self._pending.pop(umo, None)
                self._save()
                return json_response({"ok": True})
        return json_response({"ok": False, "error": "没找到这条"})
