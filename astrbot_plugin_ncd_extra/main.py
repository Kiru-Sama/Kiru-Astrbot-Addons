# -*- coding: utf-8 -*-
"""链路扩展：四个通用小命令，补 AstrBot 原生没做的「快捷开关」。

为什么要单独一个插件（而不是塞进工具箱）：
  · 这四个都是【对话层面】的东西（压缩历史 / 工具气泡 / token 用量 / 模型拿到时间），
    和工具箱那些「检索、表情、画图」不是一类，混在一起以后谁都不敢动；
  · 它们全部只依赖 AstrBot 公开 API，没有任何本机路径、密钥、私有服务，
    单独一个目录最容易被别人直接拿走复用。

命令：
  /compact [保留轮数]      把当前对话压成「摘要 + 最近 N 轮」，默认 5 轮
  /tooldisplay on|off|status  工具调用气泡开关（🔨 调用工具: xxx）
  /token consumption       当前对话的 token 用量（累计 + 上下文估算）
  /get time [on|off]       让模型拿到真实时间（注入 system_reminder）

设计口径（重要，别照抄原生配置）：
  ★ /tooldisplay 和 /get time 都是【本插件自己实现】的，不去改 AstrBot 的
    provider_settings.show_tool_use_status / datetime_system_prompt。
    原因：这两个值在 pipeline 初始化时被拷进 InternalAgentSubStage 的属性里，
    运行时改配置【不会生效】，只有 WebUI 保存配置触发 reload_pipeline_scheduler
    才行 —— 插件拿不到那个句柄。所以本插件走 hook 自己发气泡、自己注入时间，
    改完立刻生效、重载也不用。
  ★ 如果哪天在 WebUI 里把原生开关打开了，本插件会自动让路（不重复发）。

配置项见同目录 `_conf_schema.json`；AstrBot 会把值存到
`data/config/astrbot_plugin_ncd_extra_config.json`。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest

# 发独立消息（工具气泡）要用 MessageChain，api.message_components 里没有它
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

try:  # AstrBot 以包形式加载
    from . import files as attach_files
except ImportError:  # 离线单测
    import files as attach_files


# AstrBot 自带的轮次切分 / 渲染；私有路径，拿不到就用下面的本地实现顶上。
try:  # pragma: no cover - 依赖 AstrBot 版本
    from astrbot.core.agent.context.round_utils import (
        rounds_to_text as _astr_rounds_to_text,
        split_into_rounds as _astr_split_into_rounds,
    )
except Exception:  # noqa: BLE001
    _astr_rounds_to_text = None
    _astr_split_into_rounds = None


WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 摘要用的 system prompt。要求逐条写死，避免模型自由发挥写成抒情文。
SUMMARY_SYSTEM_PROMPT = (
    "你是一个对话压缩器。把用户给出的多轮对话历史压成一份「可直接续聊」的摘要，用中文。"
    "只输出摘要正文，不要开场白、不要解释你在做什么、不要加代码块围栏。"
)

SUMMARY_RULES = (
    "要求：\n"
    "1. 必须保留：用户的目标与偏好、已经确认的结论、进行中任务的当前进度与下一步、"
    "关键事实与数字（名字/型号/路径/参数/金额）、尚未解决的问题。\n"
    "2. 必须丢弃：寒暄与客套、重复表达、工具调用的原始返回（只留结论）。\n"
    "3. 用要点列表，按主题分组，不要按时间流水账。\n"
    "4. 事实为准：历史里没写的东西不要补，不确定的标注「未确认」。\n"
    "5. 字数控制在 {limit} 字以内。\n"
)


# --------------------------------------------------------------------------- #
# 纯函数区：不碰 AstrBot 运行时，方便离线单测
# --------------------------------------------------------------------------- #
def as_bool(value: Any, default: bool = False) -> bool:
    """把配置里的 bool / "on" / "开" 统一成 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "开", "开启", "是", "启用"}:
            return True
        if text in {"0", "false", "no", "off", "关", "关闭", "否", "禁用"}:
            return False
    return default


def parse_onoff(text: str) -> bool | None:
    """on/off 解析；认不出来返回 None（调用方给用法提示）。"""
    word = (text or "").strip().lower()
    if word in {"on", "1", "true", "yes", "开", "开启", "启用", "打开"}:
        return True
    if word in {"off", "0", "false", "no", "关", "关闭", "禁用", "关掉"}:
        return False
    return None


def estimate_tokens(text: str) -> int:
    """按 AstrBot 自己的估算口径（中文 0.6 / 其他 0.3 token 每字符）估 token。"""
    if not text:
        return 0
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(chinese * 0.6 + (len(text) - chinese) * 0.3)


def record_text(record: dict) -> str:
    """一条历史记录 → 用于估算 token 的文本。"""
    if not isinstance(record, dict):
        return str(record or "")
    return json.dumps(record, ensure_ascii=False)


def history_tokens(history: list[dict]) -> int:
    return sum(estimate_tokens(record_text(item)) for item in history or [])


def split_rounds(history: list[dict]) -> list[list[dict]]:
    """按「每条 user 消息开一轮」切分；AstrBot 有实现就直接用它的。"""
    if _astr_split_into_rounds is not None:
        return _astr_split_into_rounds(history)
    rounds: list[list[dict]] = []
    current: list[dict] = []
    for segment in history or []:
        role = segment.get("role") if isinstance(segment, dict) else "?"
        if role == "user" and current:
            rounds.append(current)
            current = []
        current.append(segment)
    if current:
        rounds.append(current)
    return rounds


def rounds_text(rounds: list[list[dict]]) -> str:
    """轮次 → 给摘要模型看的纯文本。"""
    if _astr_rounds_to_text is not None:
        return _astr_rounds_to_text(rounds)
    lines: list[str] = []
    for index, rnd in enumerate(rounds, 1):
        lines.append(f"--- Round {index} ---")
        for segment in rnd:
            role = segment.get("role", "?") if isinstance(segment, dict) else "?"
            content = ""
            if isinstance(segment, dict):
                content = segment.get("content") or segment.get("tool_calls") or ""
            lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def chunk_rounds(rounds: list[list[dict]], max_chars: int) -> list[list[list[dict]]]:
    """把轮次分组，每组渲染后不超过 max_chars（单轮超长也不切，单独成组）。"""
    limit = max(1, int(max_chars))
    chunks: list[list[list[dict]]] = []
    current: list[list[dict]] = []
    used = 0
    for rnd in rounds:
        size = len(rounds_text([rnd]))
        if current and used + size > limit:
            chunks.append(current)
            current, used = [], 0
        current.append(rnd)
        used += size
    if current:
        chunks.append(current)
    return chunks


def flatten_rounds(rounds: list[list[dict]]) -> list[dict]:
    return [segment for rnd in rounds for segment in rnd]


def build_compact_history(summary: str, recent_rounds: list[list[dict]]) -> list[dict]:
    """压缩后的历史：摘要占一对 user/assistant，后面接原文保留的最近几轮。

    ★ 摘要必须是 user+assistant 成对，不能只塞一条 system：
      大多数 provider 会忽略中途的 system 消息，模型就"失忆"了。
    """
    head = [
        {"role": "user", "content": f"【此前对话摘要】\n{summary.strip()}"},
        {"role": "assistant", "content": "好的，我已了解此前的对话内容，我们接着继续。"},
    ]
    return head + flatten_rounds(recent_rounds)


def tool_bubble_text(
    name: str,
    args: dict | None = None,
    *,
    show_args: bool = True,
    arg_chars: int = 90,
) -> str:
    """工具调用气泡正文，格式对齐 AstrBot 原生（🔨 调用工具: xxx）。"""
    head = f"🔨 调用工具: {name or 'unknown'}"
    if not show_args or not args:
        return head
    try:
        brief = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        brief = str(args)
    brief = " ".join(brief.split())
    if len(brief) > arg_chars:
        brief = brief[:arg_chars].rstrip() + "…"
    return f"{head}\n   ⤷ 参数: {brief}"


def tool_result_text(name: str, result: Any, *, limit: int = 70) -> str:
    """工具返回气泡正文，格式对齐 AstrBot 原生（📎 返回结果: ...）。"""
    text = "" if result is None else str(result)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    head = f"🔨 调用工具: {name or 'unknown'}"
    return f"{head}\n📎 返回结果: {text}" if text else head


def format_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:  # noqa: BLE001
        return str(value)


def weekday_cn(moment: datetime) -> str:
    return WEEKDAY_CN[moment.weekday()]


# --------------------------------------------------------------------------- #
# 插件本体
# --------------------------------------------------------------------------- #
class NcdExtraPlugin(star.Star):
    def __init__(
        self, context: star.Context, config: dict[str, Any] | None = None
    ) -> None:
        super().__init__(context, config)
        self.context = context
        # Star.__init__ 自己不保存 config，得我们存
        self._conf: dict[str, Any] = config or {}

    # ---------------------------- 配置读写 ---------------------------- #
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self._conf.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except Exception:  # noqa: BLE001
            return default

    def _save_cfg(self, **values: Any) -> None:
        """写内存 + 落盘。AstrBot 的 AstrBotConfig 有 save_config；普通 dict 就只写内存。"""
        for key, value in values.items():
            try:
                self._conf[key] = value
            except Exception:  # noqa: BLE001
                logger.warning(f"[extra] 配置写入失败（内存）：{key}")
        save = getattr(self._conf, "save_config", None)
        if callable(save):
            try:
                save(dict(values))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[extra] 配置落盘失败：{exc}")

    # ---------------------------- 时区 ---------------------------- #
    def _tz_name(self, umo: str | None = None) -> str:
        try:
            name = self.context.get_config(umo=umo).get("timezone") or ""
        except Exception:  # noqa: BLE001
            name = ""
        return str(name).strip()

    def _now(self, umo: str | None = None) -> tuple[datetime, str]:
        """当前时间 + 时区名。配置的 timezone 认不出来就退回本机时区。"""
        name = self._tz_name(umo)
        if name:
            try:
                return datetime.now(ZoneInfo(name)), name
            except Exception:  # noqa: BLE001
                logger.debug(f"[extra] 时区 {name} 无法解析，改用本机时区")
        moment = datetime.now().astimezone()
        return moment, (moment.tzname() or "local")

    # ---------------------------- 原生开关探测 ---------------------------- #
    def _native_setting(self, umo: str | None, key: str) -> bool:
        """读 AstrBot 自己的 provider_settings（用于让路，不用于控制自己）。"""
        try:
            settings = self.context.get_config(umo=umo).get("provider_settings", {})
            return as_bool(settings.get(key, False))
        except Exception:  # noqa: BLE001
            return False

    # ====================================================================== #
    # /compact —— 保留最近 N 轮，其余压成摘要
    # ====================================================================== #
    @filter.permission_type(filter.PermissionType.SHARED_GROUP_ADMIN)
    @filter.command("compact")
    async def compact(self, event: AstrMessageEvent, args: GreedyStr):
        """压缩当前对话：保留最近 N 轮原文，更早的交给模型压成摘要。"""
        umo = event.unified_msg_origin
        keep = self._cfg_int("compact_keep_rounds", 5)
        raw = str(args or "").strip()
        if raw:
            try:
                keep = int(raw.split()[0])
            except (TypeError, ValueError):
                yield event.plain_result(
                    f"用法：/compact [保留轮数]\n当前默认保留 {keep} 轮。"
                )
                return
        if keep < 1:
            yield event.plain_result("保留轮数至少是 1。")
            return
        result = await self._do_compact(event, umo, keep)
        yield event.plain_result(result)

    async def _do_compact(self, event: AstrMessageEvent, umo: str, keep: int) -> str:
        runner = self._runner_type(umo)
        if runner and runner != "local":
            return (
                f"当前用的是第三方 Agent（{runner}），对话历史在对方服务器上，"
                "本地压不了。"
            )

        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return "拿不到 AstrBot 的对话管理器，压缩失败。"

        conversation_id = await manager.get_curr_conversation_id(umo)
        if not conversation_id:
            return "当前会话还没有对话（先聊一句再压）。"
        conversation = await manager.get_conversation(umo, conversation_id)
        if conversation is None:
            return "找不到当前对话记录，压缩失败。"

        history = self._load_history(conversation)
        rounds = split_rounds(history)
        min_rounds = max(keep + 1, self._cfg_int("compact_min_rounds", 8))
        if len(rounds) <= keep:
            return (
                f"当前只有 {len(rounds)} 轮，不超过要保留的 {keep} 轮，不用压。"
            )
        if len(rounds) < min_rounds and len(rounds) - keep < 2:
            return (
                f"当前 {len(rounds)} 轮，压完只剩 {keep} 轮，收益太小"
                f"（少于每条 {min_rounds} 轮不压）。真要压：/compact {len(rounds) - 1}"
            )

        old_rounds, recent_rounds = rounds[:-keep], rounds[-keep:]
        old_chars = len(rounds_text(old_rounds))
        summary = await self._summarize_rounds(umo, old_rounds)
        fallback_note = ""
        if not summary:
            # 摘要失败就退化成「只保留最近 N 轮」，别把用户卡在这里
            summary = (
                f"（自动摘要失败，早前 {len(old_rounds)} 轮已丢弃，"
                "只有最近几轮可用。）"
            )
            fallback_note = "\n⚠️ 摘要生成失败，这次只做了截断。"

        new_history = build_compact_history(summary, recent_rounds)
        try:
            await manager.update_conversation(
                umo,
                conversation_id,
                history=new_history,
                token_usage=0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[extra] 写回压缩后的历史失败：{exc}")
            return f"压缩结果写入失败：{exc}"

        return (
            f"🗜️ 已压缩对话 {conversation_id[:8]}…\n"
            f"· 原有 {len(rounds)} 轮 → 保留最近 {keep} 轮\n"
            f"· 前 {len(old_rounds)} 轮（{format_int(old_chars)} 字符）→ "
            f"摘要 {len(summary)} 字\n"
            f"· 历史 {len(history)} 条 → {len(new_history)} 条"
            f"{fallback_note}"
        )

    def _runner_type(self, umo: str | None) -> str:
        try:
            cfg = self.context.get_config(umo=umo) or {}
            return str((cfg.get("agent_runner") or {}).get("runner_type") or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _load_history(conversation: Any) -> list[dict]:
        raw = getattr(conversation, "history", None)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("[extra] 对话历史解析失败")
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def _summarize_rounds(self, umo: str, rounds: list[list[dict]]) -> str:
        """把若干轮压成一段摘要；太长就分批压，再压一遍。失败返回空串。"""
        provider = self._provider(umo)
        if provider is None:
            logger.warning("[extra] 没有可用 provider，跳摘要")
            return ""

        chunk_chars = max(2000, self._cfg_int("compact_chunk_chars", 12000))
        summary_chars = max(200, self._cfg_int("compact_summary_chars", 1200))
        chunks = chunk_rounds(rounds, chunk_chars)
        pieces: list[str] = []
        for index, chunk in enumerate(chunks, 1):
            text = rounds_text(chunk)
            piece = await self._ask_summary(provider, text, summary_chars)
            if not piece:
                return ""
            if len(chunks) > 1:
                logger.info(f"[extra] 第 {index}/{len(chunks)} 批摘要完成")
            pieces.append(piece)

        if len(pieces) == 1:
            return pieces[0]
        merged = "\n\n".join(f"片段 {i}：\n{p}" for i, p in enumerate(pieces, 1))
        # 二次压缩；还超长就分批发给模型再合一次（最多两轮）
        if len(merged) <= chunk_chars:
            second = await self._ask_summary(provider, merged, summary_chars)
            return second or merged
        second_pieces: list[str] = []
        sub_chunks = [merged[i : i + chunk_chars] for i in range(0, len(merged), chunk_chars)]
        for sub in sub_chunks:
            piece = await self._ask_summary(provider, sub, summary_chars)
            if not piece:
                return merged[:summary_chars]
            second_pieces.append(piece)
        return "\n\n".join(second_pieces)

    async def _ask_summary(self, provider: Any, text: str, limit: int) -> str:
        prompt = f"{SUMMARY_RULES.format(limit=limit)}\n<历史>\n{text}\n</历史>"
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=SUMMARY_SYSTEM_PROMPT,
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            logger.warning("[extra] 摘要请求超时")
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[extra] 摘要请求失败：{exc}")
            return ""
        content = (getattr(response, "completion_text", "") or "").strip()
        if content.startswith("```"):
            # 模型偶尔会套代码块围栏
            content = content.strip("`")
            content = content.split("\n", 1)[-1].strip() if "\n" in content else content
        return content[: max(limit * 2, limit)]

    def _provider(self, umo: str | None) -> Any:
        getter = getattr(self.context, "get_using_provider", None)
        if not callable(getter):
            return None
        try:
            return getter(umo=umo)
        except TypeError:
            try:
                return getter()
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

    # ====================================================================== #
    # /tooldisplay —— 工具调用气泡开关
    # ====================================================================== #
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tooldisplay")
    async def tooldisplay(self, event: AstrMessageEvent, args: GreedyStr):
        """开关工具调用气泡：/tooldisplay on|off|status"""
        word = str(args or "").strip()
        enabled = as_bool(self._cfg("tooldisplay", False), False)
        if not word or word.lower() in {"status", "状态"}:
            yield event.plain_result(self._tooldisplay_status(enabled, event))
            return
        target = parse_onoff(word)
        if target is None:
            yield event.plain_result("用法：/tooldisplay on|off|status")
            return
        self._save_cfg(tooldisplay=target)
        tip = ""
        if target and self._native_setting(event.unified_msg_origin, "show_tool_use_status"):
            tip = "\n注：AstrBot 原生工具气泡已开启，本插件会自动让路，不会重复发。"
        yield event.plain_result(
            f"工具调用气泡：{'已开启 ✅' if target else '已关闭 ⛔'}{tip}"
        )

    def _tooldisplay_status(self, enabled: bool, event: AstrMessageEvent | None) -> str:
        native = False
        if event is not None:
            native = self._native_setting(event.unified_msg_origin, "show_tool_use_status")
        lines = [
            f"工具调用气泡：{'开启 ✅' if enabled else '关闭 ⛔'}",
            f"显示参数：{'是' if as_bool(self._cfg('tooldisplay_show_args', True), True) else '否'}"
            f" · 显示返回：{'是' if as_bool(self._cfg('tooldisplay_show_result', False)) else '否'}",
        ]
        if native:
            lines.append("AstrBot 原生开关：已开启（本插件自动让路）")
        lines.append("切换：/tooldisplay on 或 /tooldisplay off")
        return "\n".join(lines)

    @filter.llm_tool(name="extend_turns")
    async def extend_turns(self, event: AstrMessageEvent, rounds: int = 5) -> str:
        """申请追加工具调用轮次：这一轮会立刻继续，不用重来。

        ★ 什么时候用：一轮里要做的事多（画图 + 发语音 + 表情包 + 查东西），
          快撞到轮次上限时，先调它给自己加额度。
        ★ 加起来立刻生效：核心循环在到上限时先看这个额度，有就抬上限继续跑。

        Args:
            rounds(int): 追加几轮，1~20，默认 5。
        """
        try:
            n = int(rounds or 5)
        except (TypeError, ValueError):
            n = 5
        n = max(1, min(20, n))
        event.set_extra("ncd_extra_steps", n)
        return f"已追加 {n} 轮工具调用额度，这一轮会立刻继续（不用重来）。"

    @filter.llm_tool(name="read_attachment")
    async def read_attachment(
        self, event: AstrMessageEvent, max_chars: int = 4000, offset: int = 0
    ) -> str:
        """读取这条消息（或被引用的消息）里附带的【文件】，抽成文字给你看。

        ★ 使用时机：用户在消息里发了或引用了一个文件，问"这里面写了什么"、
          "帮我看下这个文件"、"总结一下这份文档"。
        ★ 支持：txt / md / json / csv / 代码 等文本，pdf、docx、xlsx、xls。
        ★ 图片不用这个（你直接能看到图）。
        ★ 不认识的格式会尽量兜底：ZIP 类容器解出内部文本、二进制抠可读片段；
          音视频会明确告诉你读不了（没开语音识别）。
        ★ 长文档可以分段读，别一次把上下文塞爆。

        Args:
            max_chars(int): 这一段最多返回多少字。默认 4000，上限 12000。
            offset(int): 从第几个字开始读。默认 0；文件很长时用上次返回的
                "下一段从 N 开始" 里的 N 继续读。
        """
        comp = attach_files.find_file_component(event.get_messages() or [])
        if comp is None:
            return "这条消息里没有附带文件（图片可以直接看，不用读）。"
        name = str(getattr(comp, "name", "") or "文件")
        url = str(getattr(comp, "url", "") or "")
        local = str(getattr(comp, "file", "") or "")
        try:
            limit = max(200, min(12000, int(max_chars or 4000)))
            start = max(0, int(offset or 0))
            if local and attach_files.Path(local).is_file():
                path = attach_files.Path(local)
            else:
                path = attach_files.download(url, attach_files.temp_dir())
            full = attach_files.extract(path, 10**9, 0)
            text = full[start:start + limit]
        except attach_files.FileError as exc:
            return f"读文件失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            return f"读文件失败：{type(exc).__name__}: {exc}"
        head = f"文件「{name}」（{path.stat().st_size:,} 字节"
        if start:
            head += f"，第 {start} 字起"
        head += "）的内容：\n"
        tail = ""
        end = start + len(text)
        if end < len(full):
            tail = (
                f"\n\n……还有 {len(full) - end:,} 字没显示。"
                f"下一段用 offset={end} 继续读。"
            )
        return head + text + tail

    @filter.on_using_llm_tool()
    async def _on_tool_bubble(self, event: AstrMessageEvent, tool: Any, tool_args: Any):
        """在工具真正执行前发一条气泡。"""
        if not as_bool(self._cfg("tooldisplay", False)):
            return
        if self._native_setting(event.unified_msg_origin, "show_tool_use_status"):
            return  # 原生已经在发，别重复
        # 原生开了「显示返回」时会等结果一起发，这边同理
        if as_bool(self._cfg("tooldisplay_show_result", False)):
            return
        name = getattr(tool, "name", "") or "unknown"
        text = tool_bubble_text(
            name,
            tool_args if isinstance(tool_args, dict) else None,
            show_args=as_bool(self._cfg("tooldisplay_show_args", True), True),
        )
        await self._send_bubble(event, text)

    @filter.on_llm_tool_respond()
    async def _on_tool_result_bubble(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: Any,
        tool_result: Any,
    ):
        """可选：工具返回后补一条带结果的摘要。"""
        if not as_bool(self._cfg("tooldisplay", False)):
            return
        if not as_bool(self._cfg("tooldisplay_show_result", False)):
            return
        if self._native_setting(event.unified_msg_origin, "show_tool_use_status"):
            return
        name = getattr(tool, "name", "") or "unknown"
        result_text = self._result_text(tool_result)
        limit = max(20, self._cfg_int("tooldisplay_result_chars", 70))
        await self._send_bubble(event, tool_result_text(name, result_text, limit=limit))

    @staticmethod
    def _result_text(tool_result: Any) -> str:
        """mcp CallToolResult → 纯文本（优先取 content 里的 text）。"""
        if tool_result is None:
            return ""
        content = getattr(tool_result, "content", None)
        if isinstance(content, (list, tuple)):
            parts: list[str] = []
            for item in content:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
            if parts:
                return "\n".join(parts)
        return str(tool_result)

    async def _send_bubble(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain(type="tool_call").message(text))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[extra] 工具气泡发送失败：{exc}")

    # ====================================================================== #
    # /token consumption —— token 用量
    # ====================================================================== #
    @filter.command("token")
    async def token_usage(self, event: AstrMessageEvent, args: GreedyStr):
        """列出当前对话的 token 用量：/token consumption"""
        word = str(args or "").strip().lower()
        if word and word not in {"consumption", "usage", "用量", "消耗", "用量消耗"}:
            yield event.plain_result("用法：/token consumption（等价：/token）")
            return
        yield event.plain_result(await self._token_report(event))

    async def _token_report(self, event: AstrMessageEvent) -> str:
        umo = event.unified_msg_origin
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return "拿不到 AstrBot 的对话管理器，无法统计。"
        conversation_id = await manager.get_curr_conversation_id(umo)
        if not conversation_id:
            return "当前会话还没有对话（先聊一句再看用量）。"
        conversation = await manager.get_conversation(umo, conversation_id)
        if conversation is None:
            return "找不到当前对话记录。"

        history = self._load_history(conversation)
        rounds = split_rounds(history)
        context_tokens = history_tokens(history)
        last_usage = int(getattr(conversation, "token_usage", 0) or 0)

        lines = [
            "📊 当前对话 token 用量",
            f"· 对话 {conversation_id[:8]}… · {len(rounds)} 轮 / {len(history)} 条消息",
            f"· 上下文估算 ≈ {format_int(context_tokens)} tokens"
            + (f"（上一次请求回报 {format_int(last_usage)}）" if last_usage else ""),
        ]

        stats = await self._stat_sums(conversation_id)
        if stats is None:
            lines.append("· 累计用量：查不到（provider_stats 表不可用）")
        elif stats["calls"] == 0:
            lines.append("· 累计用量：还没有完成的请求记录")
        else:
            total = stats["input"] + stats["cached"] + stats["output"]
            lines.append(
                f"· 累计 {stats['calls']} 次请求："
                f"输入 {format_int(stats['input'] + stats['cached'])}"
                f"（其中缓存 {format_int(stats['cached'])}）"
                f"｜输出 {format_int(stats['output'])}"
                f"｜合计 {format_int(total)}"
            )
            lines.append(
                f"· 平均每次 {format_int(total // max(1, stats['calls']))} tokens"
            )

        model = self._model_name(umo)
        if model:
            lines.append(f"· 模型 {model}")
        lines.append("· 太长就压：/compact")
        return "\n".join(lines)

    async def _stat_sums(self, conversation_id: str) -> dict | None:
        """按对话聚合 provider_stats；表不存在时返回 None。"""
        db = None
        try:
            db = self.context.get_db()
        except Exception:  # noqa: BLE001
            return None
        if db is None:
            return None
        try:
            from astrbot.core.db.po import ProviderStat
            from sqlalchemy import func, select
            from sqlmodel import col
        except Exception:  # noqa: BLE001
            return None
        query = (
            select(
                func.count(col(ProviderStat.id)).label("calls"),
                func.coalesce(func.sum(ProviderStat.token_input_other), 0).label("input"),
                func.coalesce(func.sum(ProviderStat.token_input_cached), 0).label("cached"),
                func.coalesce(func.sum(ProviderStat.token_output), 0).label("output"),
            )
            .where(
                col(ProviderStat.agent_type) == "internal",
                col(ProviderStat.conversation_id) == conversation_id,
            )
        )
        try:
            async with db.get_db() as session:
                row = (await session.execute(query)).one()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[extra] 查询 provider_stats 失败：{exc}")
            return None
        return {
            "calls": int(getattr(row, "calls", 0) or 0),
            "input": int(getattr(row, "input", 0) or 0),
            "cached": int(getattr(row, "cached", 0) or 0),
            "output": int(getattr(row, "output", 0) or 0),
        }

    def _model_name(self, umo: str | None) -> str:
        provider = self._provider(umo)
        if provider is None:
            return ""
        config = getattr(provider, "provider_config", None) or {}
        try:
            return str(config.get("model") or config.get("model_name") or "")
        except Exception:  # noqa: BLE001
            return ""

    # ====================================================================== #
    # /get time —— 让模型拿到真实时间
    # ====================================================================== #
    @filter.command("get")
    async def get_cmd(self, event: AstrMessageEvent, args: GreedyStr):
        """用法：/get time [on|off]"""
        parts = str(args or "").split()
        if not parts or parts[0].lower() not in {"time", "时间", "now"}:
            yield event.plain_result("用法：/get time [on|off]\n例：/get time、/get time on")
            return
        enabled = as_bool(self._cfg("time_inject", False))
        if len(parts) == 1:
            yield event.plain_result(self._time_status(event, enabled))
            return
        target = parse_onoff(parts[1])
        if target is None:
            yield event.plain_result("只能接 on / off：/get time on|off")
            return
        self._save_cfg(time_inject=target)
        tip = ""
        if target and self._native_setting(event.unified_msg_origin, "datetime_system_prompt"):
            tip = "\n注：AstrBot 原生已开启时间注入，本插件会自动让路。"
        moment, tzname = self._now(event.unified_msg_origin)
        yield event.plain_result(
            f"模型获取时间：{'已开启 ✅' if target else '已关闭 ⛔'}\n"
            f"当前是 {moment:%Y-%m-%d %H:%M:%S}（{weekday_cn(moment)}，{tzname}）{tip}"
        )

    def _time_status(self, event: AstrMessageEvent, enabled: bool) -> str:
        moment, tzname = self._now(event.unified_msg_origin)
        native = self._native_setting(event.unified_msg_origin, "datetime_system_prompt")
        lines = [
            f"模型获取时间：{'开启 ✅' if enabled else '关闭 ⛔'}",
            f"当前时间：{moment:%Y-%m-%d %H:%M:%S}（{weekday_cn(moment)}，{tzname}）",
        ]
        if native:
            lines.append("AstrBot 原生 datetime_system_prompt：已开启（本插件自动让路）")
        lines.append("切换：/get time on 或 /get time off")
        return "\n".join(lines)

    @filter.on_llm_request()
    async def _inject_time(
        self, event: AstrMessageEvent, request: ProviderRequest
    ) -> None:
        """每轮把当前时间塞进系统提示词末尾。"""
        if not as_bool(self._cfg("time_inject", False)):
            return
        if self._native_setting(event.unified_msg_origin, "datetime_system_prompt"):
            return  # 原生已经在 system_reminder 里给了
        moment, tzname = self._now(event.unified_msg_origin)
        block = (
            "<system_reminder>"
            f"当前时间：{moment:%Y-%m-%d %H:%M:%S}（{weekday_cn(moment)}，{tzname}）"
            "</system_reminder>"
        )
        try:
            base = getattr(request, "system_prompt", "") or ""
            request.system_prompt = f"{base}\n\n{block}" if base else block
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[extra] 注入时间失败：{exc}")
