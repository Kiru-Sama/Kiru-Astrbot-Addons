"""会话级人格切换（/人格）。

设计取舍（为什么是这么做的）：
  · 不自己维护「会话 → 人格」映射表，而是写 AstrBot 原生的会话强制人格
    （`session_service_config.persona_id`，scope=umo）——它就是「自定义规则」
    页面看到的那份配置，写进去之后 AstrBot 的 agent runner、对话、面板全部一致；
  · 人格列表直接读 `context.persona_manager.personas_v3`，与 AstrBot 原生一致；
  · 不缓存、不轮询：每次读当前值，改完立即生效，重启后自动保持（配置在 DB 里）。

命令：
  /人格              列出所有人格，标出当前会话用的那个
  /人格 <名字>       把当前会话（群聊或私聊）切到该人格
  /人格 默认          取消强制人格，回到全局默认
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

try:  # AstrBot 内部的会话级配置存储（就是「自定义规则」那份）
    from astrbot.core import sp
except Exception:  # pragma: no cover - 老版本兜底
    sp = None

CONFIG_KEY = "session_service_config"
RESET_WORDS = ("默认", "default", "自动", "auto", "清除", "重置", "clear", "reset")


class NcdPersonaPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        # Star 自己不保存 config，得我们存
        self._conf = config or {}

    # ------------------------------------------------------------------
    #  配置
    # ------------------------------------------------------------------
    def _conf_bool(self, key: str, default: bool = False) -> bool:
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")

    # ------------------------------------------------------------------
    #  AstrBot 原生数据
    # ------------------------------------------------------------------
    def _personas(self) -> list[dict]:
        """所有人格（AstrBot 原生 personas_v3；每项至少有 name）。"""
        pm = getattr(self.context, "persona_manager", None)
        items = getattr(pm, "personas_v3", None) or []
        out: list[dict] = []
        for item in items:
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                out.append(item)
        return out

    def _current_persona(self, cfg: dict) -> str:
        return str((cfg or {}).get("persona_id") or "").strip()

    async def _read_cfg(self, umo: str) -> dict:
        if sp is None:
            return {}
        try:
            value = await sp.get_async(
                scope="umo", scope_id=umo, key=CONFIG_KEY, default={}
            )
            return value if isinstance(value, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona] 读取会话配置失败: {exc}")
            return {}

    async def _write_cfg(self, umo: str, cfg: dict) -> bool:
        if sp is None:
            return False
        try:
            await sp.put_async(
                scope="umo", scope_id=umo, key=CONFIG_KEY, value=cfg
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona] 写入会话配置失败: {exc}")
            return False

    def _match(self, name: str, items: list[dict]) -> tuple[dict | None, list[dict]]:
        """按名字精确/包含匹配；返回 (目标, 多个候选)。"""
        key = name.strip().lower()
        if not key:
            return None, []
        exact = [p for p in items if str(p.get("name") or "").strip().lower() == key]
        if len(exact) == 1:
            return exact[0], []
        partial = [p for p in items if key in str(p.get("name") or "").strip().lower()]
        if len(partial) == 1:
            return partial[0], []
        return None, partial

    # ------------------------------------------------------------------
    #  命令
    # ------------------------------------------------------------------
    @filter.command("人格", alias={"persona", "换人格"})
    async def persona_cmd(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        """查看或切换当前会话的人格。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            yield event.plain_result("拿不到会话标识，稍后再试。")
            return
        items = self._personas()
        if not items:
            yield event.plain_result("没有读到任何人格（先去 AstrBot 的「人格设定」里创建一个）。")
            return

        name = str(arg or "").strip()
        cfg = await self._read_cfg(umo)
        current = self._current_persona(cfg)

        # ① 没给参数 → 列清单
        if not name or name in ("列表", "list", "ls", "帮助", "help", "?"):
            lines = ["🎭 可用人格："]
            for p in items:
                nm = str(p.get("name") or "").strip()
                mark = " ← 当前会话" if nm == current else ""
                lines.append(f"· {nm}{mark}")
            lines.append("")
            lines.append("切换：/人格 名字　恢复默认：/人格 默认")
            yield event.plain_result("\n".join(lines))
            return

        # ② 恢复默认
        if name in RESET_WORDS:
            if "persona_id" in cfg:
                cfg.pop("persona_id", None)
                if not await self._write_cfg(umo, cfg):
                    yield event.plain_result("恢复失败：写不进会话配置，看日志。")
                    return
            yield event.plain_result("已恢复默认人格（本会话不再强制指定）。")
            return

        # ③ 切换
        target, hits = self._match(name, items)
        if target is None:
            if hits:
                names = "、".join(str(p.get("name") or "") for p in hits[:8])
                yield event.plain_result(f"「{name}」匹配到多个：{names}\n写全一点再试。")
            else:
                yield event.plain_result(f"没有名为「{name}」的人格。发 /人格 看列表。")
            return

        target_name = str(target.get("name") or "").strip()
        cfg["persona_id"] = target_name
        if not await self._write_cfg(umo, cfg):
            yield event.plain_result("切换失败：写不进会话配置，看日志。")
            return
        logger.info(f"[persona] 会话 {umo} 人格切换为 {target_name}")
        yield event.plain_result(f"本会话人格已切换为「{target_name}」。")
