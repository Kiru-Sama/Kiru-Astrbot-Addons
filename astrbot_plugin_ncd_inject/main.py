# -*- coding: utf-8 -*-
"""对话提示注入：在指定会话的下一轮里，于系统提示词末尾接一句“念头”。

只改当次请求的 `req.system_prompt`，不写进对话历史；整轮（含工具循环）都带，
回复发出后消费掉。句子的格式由用户定死：`嗯，或许我可以{内容}`，不加任何标记。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.request
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

# 头像：拉成 data URL 塞进接口，面板里就不必再去外网取图
# （浏览器扩展/CSP/代理都可能拦外链，服务端这边是通的）
_AVATAR_TTL = 6 * 3600
_AVATAR_FAIL_TTL = 600
_AVATAR_LIMIT = 2 * 1024 * 1024
_AVATAR_CACHE: dict[str, tuple[float, str, bool]] = {}


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
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sid = ""
        if sid:
            return sid
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        raw = getattr(sender, "user_id", "")
        return str(raw) if raw not in (None, "") else ""

    @classmethod
    def _session_info(cls, event: AstrMessageEvent) -> dict:
        """记下能区分会话的最小信息：类型、号码、名字。"""
        try:
            name = str(event.get_sender_name() or "")
        except Exception:  # noqa: BLE001
            name = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            gid = ""
        gname = ""
        if gid:
            try:
                group = getattr(getattr(event, "message_obj", None), "group", None)
                gname = str(getattr(group, "group_name", "") or "")
            except Exception:  # noqa: BLE001
                gname = ""
            label = f"{gname or '群' + gid} · {name}".strip(" ·")
        else:
            label = f"私聊 · {name}".strip(" ·")
        try:
            platform = str(event.get_platform_id() or "")
        except Exception:  # noqa: BLE001
            platform = ""
        return {
            "label": label,
            "platform": platform,
            "kind": "group" if gid else "private",
            "uid": cls._sender_id(event),
            "name": name,
            "gid": gid,
            "gname": gname,
        }

    def _track(self, event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return ""
        info = self._session_info(event)
        info["ts"] = _now()
        self._sessions[umo] = info
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

    @staticmethod
    def _parse_umo(umo: str) -> dict:
        """umo = 平台:消息类型:会话ID。拆不开的（早期脏数据）返回空。"""
        parts = str(umo or "").split(":", 2)
        if len(parts) != 3 or not all(parts):
            return {}
        return {"platform": parts[0], "mtype": parts[1], "chat_id": parts[2]}

    @staticmethod
    def _split_old_label(label: str) -> tuple[str, str]:
        """老记录只存了「群名 · 昵称」/「私聊 · 昵称」，拆回来补号码。"""
        text = str(label or "").strip()
        if text == "私聊":
            return "", ""
        if text.startswith("私聊 · "):
            return "", text[len("私聊 · ") :].strip()
        if " · " in text:
            head, tail = text.rsplit(" · ", 1)
            return head.strip(), tail.strip()
        return text, ""

    @staticmethod
    def _qq(value: str) -> str:
        text = str(value or "").strip()
        if text.isdigit() and 4 <= len(text) <= 12:
            return text
        return ""

    @classmethod
    def _avatar(cls, kind: str, number: str) -> str:
        qq = cls._qq(number)
        if not qq:
            return ""
        if kind == "group":
            return f"https://p.qlogo.cn/gh/{qq}/{qq}/100"
        return f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100"

    @staticmethod
    def _download_avatar(url: str) -> bytes:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (AstrBot ncd_inject avatar)"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ctype = str(resp.headers.get("Content-Type") or "")
            if not ctype.startswith("image/"):
                return b""
            raw = resp.read(_AVATAR_LIMIT + 1)
        return raw if len(raw) <= _AVATAR_LIMIT else b""

    @classmethod
    async def _avatar_value(cls, kind: str, number: str) -> str:
        """头像优先返回 data URL；拉不到就退回外链，让浏览器再试一次。"""
        url = cls._avatar(kind, number)
        if not url:
            return ""
        now = _now()
        hit = _AVATAR_CACHE.get(url)
        if hit:
            ttl = _AVATAR_TTL if hit[2] else _AVATAR_FAIL_TTL
            if now - hit[0] < ttl:
                return hit[1]
        value = url
        try:
            raw = await asyncio.to_thread(cls._download_avatar, url)
            if raw:
                mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                value = "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[inject] 拉头像失败 {url}: {exc}")
        _AVATAR_CACHE[url] = (now, value, value != url)
        return value

    async def api_sessions(self):
        rows = []
        for umo, meta in self._sessions.items():
            if not isinstance(meta, dict):
                continue
            parts = self._parse_umo(umo)
            if not parts:
                continue  # 脏 umo（如 "u"）选不了，别让它占列表
            kind = str(meta.get("kind") or "")
            if kind not in ("group", "private"):
                kind = "group" if parts["mtype"] == "GroupMessage" else "private"
            gid = str(meta.get("gid") or "")
            uid = str(meta.get("uid") or "")
            gname = str(meta.get("gname") or "")
            name = str(meta.get("name") or "")
            if not gname or not name:
                head, tail = self._split_old_label(meta.get("label", ""))
                if kind == "group":
                    gname = gname or head
                    name = name or tail
                else:
                    name = name or tail
            if kind == "group":
                gid = gid or parts["chat_id"]
            else:
                uid = uid or parts["chat_id"]
            number = gid if kind == "group" else uid
            rows.append(
                {
                    "umo": umo,
                    "label": meta.get("label", ""),
                    "ts": meta.get("ts", 0),
                    "platform": parts["platform"],
                    "kind": kind,
                    "uid": uid,
                    "name": name,
                    "gid": gid,
                    "gname": gname,
                    "avatar": "",
                    "_avatar_kind": "group" if kind == "group" else "user",
                    "_avatar_number": number,
                }
            )
        rows.sort(key=lambda x: x["ts"], reverse=True)
        items = rows[:50]
        meta = {
            row["umo"]: (
                str(row.pop("_avatar_kind", "")),
                str(row.pop("_avatar_number", "")),
            )
            for row in items
        }
        sem = asyncio.Semaphore(8)

        async def _fill(row: dict) -> None:
            kind, number = meta[row["umo"]]
            async with sem:
                try:
                    row["avatar"] = await self._avatar_value(kind, number)
                except Exception:  # noqa: BLE001
                    row["avatar"] = ""

        # 头像只是锦上添花：最多等 4 秒，没拉到的先用外链，别把列表拖死
        tasks = [asyncio.create_task(_fill(row)) for row in items]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=4.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for row in items:
            if not row["avatar"]:
                row["avatar"] = self._avatar(*meta[row["umo"]])
        return json_response({"items": items})

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
