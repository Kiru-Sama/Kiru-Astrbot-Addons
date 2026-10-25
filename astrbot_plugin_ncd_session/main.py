# -*- coding: utf-8 -*-
"""链路会话：对话的列表 / 切换 / 命名 / 删除。

AstrBot 原生只有 `/new`（新建并切过去），没有"回头切回旧对话"的命令 ——
网页端能做，聊天里做不了。这个插件把缺的四件事补上：

  /会话 [别名 conv]        列出当前会话的所有对话（序号 · 标题 · ID 前 8 位 · 当前标记）
  /切换 3 [别名 switch]    切到第 3 个对话（也可以填 ID 前几位）
  /命名 熬夜聊猫娘 [rename] 给当前对话改标题
  /删除 3 [别名 del]       删掉第 3 个对话（当前对话不让删，先切走）

★ 全部走 AstrBot 的 conversation_manager 公开接口，不碰数据库、不写死路径。
★ 序号以 `/会话` 列出来的顺序为准（就是列表返回的顺序）。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.core.star.filter.command import GreedyStr


class NcdSessionPlugin(star.Star):
    def __init__(
        self, context: star.Context, config: dict[str, Any] | None = None
    ) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf: dict[str, Any] = config or {}

    # ------------------------------------------------------------------
    #  工具
    # ------------------------------------------------------------------
    async def _list(self, umo: str) -> list:
        try:
            return await self.context.conversation_manager.get_conversations(umo) or []
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[session] 读取对话列表失败: {exc}")
            return []

    async def _current(self, umo: str) -> str:
        try:
            return await self.context.conversation_manager.get_curr_conversation_id(umo) or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _label(conv, index: int) -> str:
        title = (getattr(conv, "title", "") or "").strip()
        return title or f"对话 {index}"

    @staticmethod
    def _short(cid: str) -> str:
        return (cid or "")[:8]

    @staticmethod
    def _cid(conv) -> str:
        """AstrBot 4.x 的 Conversation 字段是 cid，老版本插件里叫 conversation_id。"""
        return str(
            getattr(conv, "cid", None)
            or getattr(conv, "conversation_id", None)
            or ""
        )

    def _resolve(self, convs: list, token: str) -> str | None:
        """把「3」或「a1b2c3d4」解析成 conversation_id。"""
        token = (token or "").strip()
        if not token:
            return None
        if token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(convs):
                cid = self._cid(convs[idx - 1])
                if cid:
                    return cid
            # 不是有效序号时继续当 ID 前缀试（UUID 可能以数字开头）
        low = token.lower()
        for conv in convs:
            cid = self._cid(conv)
            if cid and cid.lower().startswith(low):
                return cid
        return None

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False))

    # ------------------------------------------------------------------
    #  命令
    # ------------------------------------------------------------------
    @filter.command("会话", alias={"conv", "convs"})
    async def list_conversations(self, event: AstrMessageEvent) -> None:
        """列出当前会话的所有对话"""
        umo = event.unified_msg_origin
        convs = await self._list(umo)
        curr = await self._current(umo)
        if not convs:
            self._reply(event, "这个会话还没有对话。发一句话就会自动建一个，或 /new 新建。")
            return
        lines = [f"📚 当前会话的对话（共 {len(convs)} 个）："]
        for i, conv in enumerate(convs, 1):
            cid = self._cid(conv)
            mark = "  ← 当前" if cid and cid == curr else ""
            lines.append(f"{i}. {self._label(conv, i)} · {self._short(cid)}{mark}")
        lines += [
            "",
            "用法：/切换 <序号> · /命名 <新标题> · /删除 <序号> · /new 新建",
        ]
        self._reply(event, "\n".join(lines))

    @filter.command("切换", alias={"switch"})
    async def switch_conversation(self, event: AstrMessageEvent, index: str = "") -> None:
        """切换到另一个对话：/切换 3 或 /切换 前几位ID"""
        umo = event.unified_msg_origin
        convs = await self._list(umo)
        if not convs:
            self._reply(event, "没有可切换的对话。先发一句话或 /new 新建。")
            return
        cid = self._resolve(convs, index)
        if not cid:
            self._reply(
                event,
                f"没找到「{index or '（空）'}」。先 /会话 看一下序号，再 /切换 <序号>。",
            )
            return
        try:
            await self.context.conversation_manager.switch_conversation(umo, cid)
        except Exception as exc:  # noqa: BLE001
            self._reply(event, f"切换失败：{type(exc).__name__}: {exc}")
            return
        idx = next(
            (i for i, c in enumerate(convs, 1)
             if self._cid(c) == cid),
            0,
        )
        title = self._label(convs[idx - 1], idx) if idx else cid[:8]
        self._reply(event, f"✅ 已切换到：{title}（{self._short(cid)}）")

    @filter.command("命名", alias={"rename"})
    async def rename_conversation(self, event: AstrMessageEvent, title: GreedyStr = "") -> None:
        """给当前对话改标题：/命名 熬夜聊猫娘"""
        umo = event.unified_msg_origin
        name = str(title or "").strip()
        if not name:
            self._reply(event, "用法：/命名 <新标题>（给当前对话起个名字）")
            return
        if len(name) > 60:
            name = name[:60]
        curr = await self._current(umo)
        if not curr:
            self._reply(event, "当前没有对话。先发一句话或 /new 新建，再命名。")
            return
        try:
            await self.context.conversation_manager.update_conversation(
                umo, conversation_id=curr, title=name
            )
        except Exception as exc:  # noqa: BLE001
            self._reply(event, f"命名失败：{type(exc).__name__}: {exc}")
            return
        self._reply(event, f"✅ 当前对话已命名为：{name}")

    @filter.command("删除", alias={"del", "convdel"})
    async def delete_conversation(self, event: AstrMessageEvent, index: str = "") -> None:
        """删除一个对话：/删除 3（当前对话不能删，先切走）"""
        umo = event.unified_msg_origin
        convs = await self._list(umo)
        if not convs:
            self._reply(event, "没有可删除的对话。")
            return
        cid = self._resolve(convs, index)
        if not cid:
            self._reply(
                event,
                f"没找到「{index or '（空）'}」。先 /会话 看一下序号，再 /删除 <序号>。",
            )
            return
        curr = await self._current(umo)
        if cid == curr:
            self._reply(
                event,
                "这是当前正在用的对话，不能删。\n先 /切换 到别的对话，或者直接 /new 新建一个，再来删它。",
            )
            return
        try:
            await self.context.conversation_manager.delete_conversation(umo, cid)
        except Exception as exc:  # noqa: BLE001
            self._reply(event, f"删除失败：{type(exc).__name__}: {exc}")
            return
        self._reply(event, f"🗑 已删除对话 {self._short(cid)}（不可恢复）")
