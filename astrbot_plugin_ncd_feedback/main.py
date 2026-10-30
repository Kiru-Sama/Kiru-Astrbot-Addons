# -*- coding: utf-8 -*-
"""反馈通道：群友想要某种画风 / 缺底模 / 奇怪的小要求，都能提交，面板里统一看。

三种入口：
    /反馈 画风 想要像素风的 LoRA          （命令，普通群友）
    bot 自己调 feedback_submit 工具        （用户懒得打命令时说一句就行）
    面板里点「已处理 / 删除」              （管理侧）

为什么单独一个插件：
    反馈是「别人提的、我们要看」的数据，和出图/监控是两回事；
    单独插件可以独立启停，也不会因为 comfy 重载把反馈弄丢。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.web import request
from astrbot.core.star.filter.command import GreedyStr

PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_DIR_NAME = PLUGIN_DIR.name
DATA_DIR = PLUGIN_DIR.parents[2] / "data" / "plugin_data" / "ncd_feedback"
STORE = DATA_DIR / "items.jsonl"

STATUS_LABEL = {"new": "待处理", "done": "已处理", "dropped": "已忽略"}


class NcdFeedbackPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf: dict[str, Any] = config or {}
        self._items: list[dict] | None = None
        self._register_web()

    # ------------------------------------------------------------------
    #  配置与存储
    # ------------------------------------------------------------------
    def _cfg_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._conf.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        val = self._conf.get(key, default)
        return val if isinstance(val, bool) else default

    def _categories(self) -> list[str]:
        raw = str(self._conf.get("feedback_categories") or "画风,底模,功能,bug,其他")
        cats = [c.strip() for c in raw.replace("，", ",").split(",") if c.strip()]
        return cats or ["其他"]

    def _load(self) -> list[dict]:
        if self._items is not None:
            return self._items
        items: list[dict] = []
        try:
            if STORE.is_file():
                for line in STORE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("id"):
                        items.append(obj)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[feedback] 读取反馈失败: {exc}")
        self._items = items
        return items

    def _save_all(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STORE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for it in self._items or []:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
            tmp.replace(STORE)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[feedback] 保存反馈失败: {exc}")

    def _add(self, event: AstrMessageEvent | None, text: str, category: str) -> dict:
        items = self._load()
        item = {
            "id": uuid.uuid4().hex[:8],
            "ts": time.time(),
            "category": category or "其他",
            "text": text.strip()[:800],
            "status": "new",
            "note": "",
            "umo": str(getattr(event, "unified_msg_origin", "") or "") if event else "",
            "group": self._group_name(event) if event else "",
            "sender_id": str(getattr(event, "get_sender_id", lambda: "")() or "") if event else "",
            "sender": self._sender_name(event) if event else "",
        }
        items.append(item)
        self._trim()
        self._save_all()
        logger.info(f"[feedback] 新反馈 {item['id']} [{item['category']}] {item['text'][:60]}")
        return item

    def _trim(self) -> None:
        items = self._items or []
        limit = max(20, self._cfg_int("feedback_max_items", 500))
        if len(items) <= limit:
            return
        # 先删已处理的，再按时间删最老的
        done = [it for it in items if it.get("status") != "new"]
        keep = [it for it in items if it.get("status") == "new"]
        for it in done:
            if len(keep) + len(done) <= limit:
                break
            done.remove(it)
        merged = keep + done
        if len(merged) > limit:
            merged.sort(key=lambda x: x.get("ts", 0))
            merged = merged[-limit:]
        self._items = merged

    @staticmethod
    def _group_name(event: AstrMessageEvent | None) -> str:
        if event is None:
            return ""
        try:
            gid = event.get_group_id()
        except Exception:  # noqa: BLE001
            return ""
        if not gid:
            return ""
        try:
            g = event.get_group()
            if g is not None and getattr(g, "group_name", ""):
                return str(g.group_name)[:30]
        except Exception:  # noqa: BLE001
            pass
        return str(gid)

    @staticmethod
    def _sender_name(event: AstrMessageEvent | None) -> str:
        if event is None:
            return ""
        try:
            return str(event.get_sender_name() or "")[:20]
        except Exception:  # noqa: BLE001
            return ""

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            return str(event.get_sender_id()) in {str(x) for x in (cfg.get("admins_id") or [])}
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    #  命令
    # ------------------------------------------------------------------
    @filter.command("反馈", alias={"feedback", "建议"})
    async def cmd_feedback(self, event: AstrMessageEvent, body: GreedyStr = "") -> None:
        """提交一条反馈：/反馈 [分类] 内容"""
        text = str(body or "").strip()
        if not text:
            cats = "、".join(self._categories())
            yield event.plain_result(
                "用法：/反馈 [分类] 想说的话\n"
                f"分类可以是：{cats}（不写就归「其他」）\n"
                "例：/反馈 画风 想要像素风的 LoRA"
            )
            return
        if event.get_group_id() and not self._cfg_bool("feedback_allow_group", True):
            yield event.plain_result("群里关掉了反馈，私聊我再说吧。")
            return

        category = "其他"
        cats = self._categories()
        first, _, rest = text.partition(" ")
        if first in cats and rest.strip():
            category, text = first, rest.strip()
        elif first.lower() in {c.lower() for c in cats} and rest.strip():
            category, text = first, rest.strip()

        item = self._add(event, text, category)
        await self._notify_admin(item)
        yield event.plain_result(
            f"收到啦，编号 #{item['id']}（{item['category']}）。\n"
            "会在反馈面板里排上，处理完就不用再提了。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("反馈列表", alias={"反馈待办"})
    async def cmd_list(self, event: AstrMessageEvent) -> None:
        """看最近 10 条待处理反馈"""
        items = [it for it in self._load() if it.get("status") == "new"]
        items.sort(key=lambda x: x.get("ts", 0), reverse=True)
        if not items:
            yield event.plain_result("没有待处理的反馈。")
            return
        lines = [f"📮 待处理反馈（共 {len(items)} 条）："]
        for it in items[:10]:
            when = time.strftime("%m-%d %H:%M", time.localtime(it.get("ts", 0)))
            lines.append(f"#{it['id']} [{it['category']}] {when} · {it['text'][:60]}")
        lines.append("")
        lines.append("处理：/反馈处理 <编号>　忽略：/反馈忽略 <编号>")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("反馈处理", alias={"反馈完成"})
    async def cmd_done(self, event: AstrMessageEvent, fid: str = "") -> None:
        """把某条反馈标记为已处理：/反馈处理 3f2a1b9c"""
        yield event.plain_result(self._set_status(str(fid).strip(), "done"))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("反馈忽略")
    async def cmd_drop(self, event: AstrMessageEvent, fid: str = "") -> None:
        """把某条反馈标记为已忽略"""
        yield event.plain_result(self._set_status(str(fid).strip(), "dropped"))

    def _set_status(self, fid: str, status: str) -> str:
        if not fid:
            return "要给个编号，比如 /反馈处理 3f2a1b9c"
        for it in self._load():
            if it.get("id") == fid or it.get("id", "").startswith(fid):
                it["status"] = status
                it["done_at"] = time.time()
                self._save_all()
                return f"#{it['id']} 已标记为「{STATUS_LABEL.get(status, status)}」。"
        return f"没找到编号 {fid} 的反馈。"

    # ------------------------------------------------------------------
    #  LLM 工具：用户说一句「帮我记个建议」时，bot 直接提交
    # ------------------------------------------------------------------
    @filter.llm_tool(name="feedback_submit")
    async def tool_submit(
        self,
        event: AstrMessageEvent,
        text: str,
        category: str = "",
    ) -> str:
        """提交一条用户反馈/建议（想画的风格缺 LoRA、缺底模、奇怪的小要求等）。

        什么时候用：用户明确表达「想要/希望能/能不能加」这类愿望，且和出图能力相关；
        或者用户直接说「帮我记一下 / 提个建议 / 反馈一下」。
        ★ 用户只是普通聊天抱怨时不要调用；不要替用户编造需求。

        Args:
            text(string): 反馈内容，用用户自己的话说清想要什么。
            category(string): 分类，可选：画风 / 底模 / 功能 / bug / 其他；留空=其他。
        """
        body = str(text or "").strip()
        if not body:
            return "没写内容，没提交。"
        cat = str(category or "").strip()
        if cat not in self._categories():
            cat = "其他"
        item = self._add(event, body, cat)
        await self._notify_admin(item)
        return (
            f"已记录反馈 #{item['id']}（{item['category']}）。"
            "回复用户时用一句话确认即可，不要长篇复述。"
        )

    async def _notify_admin(self, item: dict) -> None:
        if not self._cfg_bool("feedback_notify_admin", False):
            return
        try:
            cfg = self.context.get_config()
            admins = [str(x) for x in (cfg.get("admins_id") or [])]
        except Exception:  # noqa: BLE001
            admins = []
        if not admins:
            return
        # ★ 真正的私聊推送需要管理员会话的 umo，目前没有可靠来源，所以只落一条日志
        logger.warning(
            f"[feedback] 新反馈 #{item['id']} [{item['category']}] "
            f"{item['text'][:60]}（管理员 {admins}）"
        )

    # ------------------------------------------------------------------
    #  Web API（面板用）
    # ------------------------------------------------------------------
    def _register_web(self) -> None:
        routes = [
            ("list", self.api_list, ["POST"], "反馈列表（POST 便于带筛选参数）"),
            ("stats", self.api_stats, ["GET"], "反馈统计"),
            ("resolve", self.api_resolve, ["POST"], "标记已处理/忽略"),
            ("delete", self.api_delete, ["POST"], "删除反馈"),
        ]
        for name, fn, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_DIR_NAME}/ncd-feedback/{name}", fn, methods, desc
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[feedback] 注册接口 /{name} 失败: {exc}")

    async def api_list(self):
        items = list(self._load())
        try:
            body = await request.json({}) or {}
        except Exception:  # noqa: BLE001
            body = {}
        status = str((body or {}).get("status") or "").strip()
        keyword = str((body or {}).get("q") or "").strip().lower()
        try:
            limit = int((body or {}).get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        if status:
            items = [it for it in items if it.get("status") == status]
        if keyword:
            items = [
                it for it in items
                if keyword in str(it.get("text", "")).lower()
                or keyword in str(it.get("category", "")).lower()
                or keyword in str(it.get("sender", "")).lower()
            ]
        items.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return {"ok": True, "total": len(self._load()), "items": items[:limit]}

    async def api_stats(self):
        items = self._load()
        by_status: dict[str, int] = {}
        by_cat: dict[str, int] = {}
        for it in items:
            by_status[it.get("status", "new")] = by_status.get(it.get("status", "new"), 0) + 1
            by_cat[it.get("category", "其他")] = by_cat.get(it.get("category", "其他"), 0) + 1
        return {
            "ok": True, "total": len(items),
            "by_status": by_status, "by_category": by_cat,
            "newest": max((it.get("ts", 0) for it in items), default=0),
        }

    async def api_resolve(self):
        try:
            body = await request.json({}) or {}
        except Exception:  # noqa: BLE001
            body = {}
        fid = str((body or {}).get("id") or "").strip()
        status = str((body or {}).get("status") or "done").strip()
        if status not in ("new", "done", "dropped"):
            status = "done"
        return {"ok": True, "message": self._set_status(fid, status)}

    async def api_delete(self):
        try:
            body = await request.json({}) or {}
        except Exception:  # noqa: BLE001
            body = {}
        fid = str((body or {}).get("id") or "").strip()
        items = self._load()
        left = [it for it in items if it.get("id") != fid]
        if len(left) == len(items):
            return {"ok": False, "message": f"没找到 {fid}"}
        self._items = left
        self._save_all()
        return {"ok": True, "message": f"已删除 {fid}"}
