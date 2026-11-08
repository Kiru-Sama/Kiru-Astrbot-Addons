"""更自然的对话 —— 动态检索层（AstrBot 插件）

定位：
    人格（静态）负责「我是谁」；本插件（动态）负责「怎么说更自然」。
    它本身不提供人格，而是把 /人格 列表里的「更自然的对话beta」这一项
    变成「动态检索模式」：选中它时，每轮用用户这句话去历史语料里
    检索 5 条最像的原话，作为 few-shot 追加到 system_prompt。

数据：
    电脑端 build_natural_pack.py 生成的 natural_pack_<id>.json
    放到 plugin_data/ncd_natural/packs/ 下。

命令：
    /自然            看状态（当前包、条数、检索条数）
    /自然 重载        重新读取数据包 + 重建人格提示词
    /自然 包 <id>     切换使用哪个数据包
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import time
from collections import deque
from pathlib import Path

from astrbot.api import logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_DIR_NAME = PLUGIN_DIR.name
PERSONA_ID = "更自然的对话beta"  # 旧版通用项（已废弃）
SUFFIX = " 更自然的对话beta"
SUFFIX_ALL = " 更自然的对话 集合"
PERSONA_TAG = "natural_beta"

# 插件目录里的 chatlog.py（从 qq-clone 项目复用）
sys.path.insert(0, str(PLUGIN_DIR))
from chatlog import Retriever  # noqa: E402

DEFAULT_PROMPT = """你是「{name}」，不是 AI 助手，是我认识的那个人。现在你在 QQ 上跟我聊天。

{profile}

# 规矩
1. 只输出你要发出去的消息本身。连发几条就用换行分开，每条都短。
2. 不写旁白、不写动作、不加括号解释、不用 markdown、不列点、不总结。
3. 长度跟上面的原话一致，大部分时候 2~12 个字，别长篇大论。
4. 不用敬语和书面语，该带"啊 / 吧 / 呢 / 哈 / 咯"就带上。
5. 永远别承认自己在模仿、也别提到 AI 和模型。被追问就随口敷衍过去，继续当「{name}」。
6. 别主动结束话题，也别一直追问，自然点。"""


@register(
    "astrbot_plugin_ncd_natural",
    "本地自用",
    "更自然的对话：选中人格「更自然的对话beta」后，每轮从语料里检索原话做 few-shot。",
    "1.0.0",
)
class NaturalChat(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._packs: dict[str, dict] = {}
        self._retrievers: dict[str, Retriever] = {}
        self._current: str = ""
        self._persona_ready = False
        # 兼容层：记住每个会话「最近一次用的普通人格」，选中特殊项时叠在它上面
        self._prev: dict[str, str] = {}
        self._prev_loaded = False
        # 独立总开关（插件级，不是人格级）
        self._enabled = False
        self._state_loaded = False
        # 调参（页面可改，覆盖插件配置）
        self._k = 0
        self._max_chars = 0
        # 每轮运行记录（页面看动态）
        self._rounds: deque = deque(maxlen=30)

    # ------------------------------------------------------------------ 路径
    def _data_dir(self) -> Path:
        root = PLUGIN_DIR.parents[1]  # data/plugins/<name> -> data
        return root / "data" / "plugin_data" / "ncd_natural"

    def _pack_dir(self) -> Path:
        # 插件自带 packs/ 优先（随插件包分发），其次 plugin_data
        local = PLUGIN_DIR / "packs"
        if local.is_dir():
            return local
        return self._data_dir() / "packs"

    # ------------------------------------------------------------------ 配置
    def _conf_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _conf_str(self, key: str, default: str = "") -> str:
        return str(self.config.get(key, default) or "").strip()

    # ------------------------------------------------------------------ 数据
    def _ensure_all_pack(self, pack_dir: Path) -> None:
        """集合包自动维护：packs 里任何单包比 all 新 → 重新合并。"""
        singles = [
            p for p in pack_dir.glob("natural_pack_*.json")
            if p.stem != "natural_pack_all"
        ]
        if not singles:
            return
        all_path = pack_dir / "natural_pack_all.json"
        if all_path.is_file():
            try:
                newest = max(p.stat().st_mtime for p in singles)
                if all_path.stat().st_mtime >= newest:
                    return
            except OSError:
                pass

        pairs: list = []
        total = 0
        profiles: list[dict] = []
        for path in sorted(singles):
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            pairs.extend(d.get("pairs") or [])
            profiles.append(d.get("profile") or {})
            total += int(d.get("total_messages") or 0)
        if not pairs:
            return

        n = sum(int(pr.get("count") or 0) for pr in profiles) or 1
        merged = {
            "schema": 1,
            "id": "all",
            "name": "集合",
            "me": "我",
            "total_messages": total,
            "profile": {
                "count": n,
                "avg_len": round(
                    sum(float(pr.get("avg_len") or 0) * int(pr.get("count") or 0) for pr in profiles) / n, 1
                ),
                "no_punct_pct": round(
                    sum(float(pr.get("no_punct_pct") or 0) * int(pr.get("count") or 0) for pr in profiles) / n
                ),
                "particles": list(
                    dict.fromkeys(x for pr in profiles for x in (pr.get("particles") or []))
                )[:10],
                "phrases": list(
                    dict.fromkeys(x for pr in profiles for x in (pr.get("phrases") or []))
                )[:10],
            },
            "pairs": pairs,
            "source": "（所有单包自动合并）",
        }
        try:
            tmp = all_path.with_name(all_path.name + ".tmp")
            tmp.write_text(
                json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, all_path)
            logger.info(f"[natural] 集合包已自动合并：{len(pairs)} 组")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[natural] 集合包合并失败: {exc}")

    def _load_packs(self) -> None:
        self._packs.clear()
        self._retrievers.clear()
        pack_dir = self._pack_dir()
        if pack_dir.is_dir():
            self._ensure_all_pack(pack_dir)
        if not pack_dir.is_dir():
            logger.info(f"[natural] 还没有数据包目录：{pack_dir}")
            return
        for path in sorted(pack_dir.glob("natural_pack_*.json")):
            try:
                pack = json.loads(path.read_text(encoding="utf-8"))
                pid = str(pack.get("id") or path.stem)
                pairs = pack.get("pairs") or []
                if not pairs:
                    continue
                self._packs[pid] = pack
                self._retrievers[pid] = Retriever(pairs)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[natural] 读取数据包失败 {path.name}: {exc}")

        if not self._current or self._current not in self._packs:
            # ★ 默认用「集合」（全部数据合并）
            self._current = (
                "all" if "all" in self._packs
                else (self._conf_str("natural_pack_id") or (
                    next(iter(self._packs)) if self._packs else ""))
            )
        logger.info(
            f"[natural] 已加载 {len(self._packs)} 个数据包"
            + (f"（当前：{self._current}）" if self._current else "")
        )

    @staticmethod
    def _profile_lines(pack: dict | None) -> list[str]:
        """把数据包的风格统计转成提示词行（运行时注入用）。"""
        if not pack:
            return []
        profile = pack.get("profile") or {}
        lines = [
            f"- 一共 {profile.get('count', 0)} 条消息，平均每条 {profile.get('avg_len', 0)} 个字",
            f"- 大概 {profile.get('no_punct_pct', 0)}% 的消息结尾不带句号",
        ]
        if profile.get("particles"):
            lines.append("- 语气词/口头禅：" + "、".join(profile["particles"][:8]))
        if profile.get("phrases"):
            lines.append("- 常出现的短语：" + "、".join(profile["phrases"][:8]))
        return lines

    def _persona_prompt(self) -> str:
        """人格项只放通用规矩；「是谁 / 什么风格 / 说过什么」由插件每轮动态注入。"""
        return DEFAULT_PROMPT.format(name="对方", profile="").replace("\n\n\n", "\n\n")

    async def _sync_duplicate_personas(self) -> None:
        """给每个人格创建「X 更自然的对话beta」副本（幂等）。

        · 副本 = 原人格的 system_prompt 原样拷贝
        · 选中副本时，插件才注入动态内容；原人格完全不受影响
        """
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            logger.warning("[natural] 拿不到 persona_manager")
            return
        try:
            items = list(pm.personas_v3 or [])
        except Exception:  # noqa: BLE001
            items = []
        existing = {}
        for item in items:
            nm = str(item.get("name") or item.get("persona_id") or "")
            if nm:
                existing[nm] = item
        # 旧版的通用人格「更自然的对话beta」不再使用，删掉避免混淆
        if PERSONA_ID in existing:
            try:
                await pm.delete_persona(PERSONA_ID)
                logger.info(f"[natural] 已删除旧人格 {PERSONA_ID}")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[natural] 删除旧人格失败: {exc}")
        made = 0
        for name, item in list(existing.items()):
            if name.endswith(SUFFIX) or name.endswith(SUFFIX_ALL) or name.endswith(" 更自然的对话") or name == PERSONA_ID:
                continue
            prompt = str(item.get("system_prompt") or "")
            # 只保留「X 更自然的对话」一个副本；旧的 beta / 集合 副本删掉
            for old_name in (name + SUFFIX, name + SUFFIX_ALL, name + " 更自然的对话 更自然的对话"):
                if old_name in existing:
                    try:
                        await pm.delete_persona(old_name)
                    except Exception:  # noqa: BLE001
                        pass
            dup = name + " 更自然的对话"
            if dup in existing:
                try:
                    await pm.update_persona(persona_id=dup, system_prompt=prompt)
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                await pm.create_persona(persona_id=dup, system_prompt=prompt)
                made += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[natural] 创建副本失败 {dup}: {exc}")
        logger.info(f"[natural] 人格副本同步完成（新建 {made} 个）")

    # ------------------------------------------------------------------ Web 界面
    def _register_web(self) -> None:
        try:
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-natural/status",
                self.api_status,
                ["GET"],
                "更自然的对话：开关 / 数据包 / 参数",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-natural/toggle",
                self.api_toggle,
                ["POST"],
                "更自然的对话：切换总开关",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-natural/config",
                self.api_config,
                ["POST"],
                "更自然的对话：调参",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[natural] 注册 Web API 失败: {exc}")

    async def api_toggle(self):
        from astrbot.api.web import request as _req

        try:
            body = await _req.json({}) or {}
        except Exception:  # noqa: BLE001
            body = {}
        want = body.get("enabled")
        if want is None:
            self._load_state()
            self._enabled = not self._enabled
        else:
            self._enabled = bool(want)
        self._save_state()
        logger.info(f"[natural] 总开关 → {self._enabled}")
        return {"ok": True, "enabled": self._enabled}

    async def api_config(self):
        from astrbot.api.web import request as _req

        try:
            body = await _req.json({}) or {}
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("k") is not None:
            try:
                self._k = max(1, min(10, int(body.get("k"))))
            except (TypeError, ValueError):
                pass
        if body.get("max_chars") is not None:
            try:
                self._max_chars = max(200, min(8000, int(body.get("max_chars"))))
            except (TypeError, ValueError):
                pass
        self._save_state()
        logger.info(f"[natural] 调参 k={self._eff_k()} max_chars={self._eff_max_chars()}")
        return {"ok": True, "k": self._eff_k(), "max_chars": self._eff_max_chars()}

    async def api_status(self):
        self._load_state()
        return {
            "enabled": self._enabled,
            "current": self._current,
            "k": self._eff_k(),
            "max_chars": self._eff_max_chars(),
            "rounds": list(self._rounds)[:20],
            "persona": PERSONA_ID,
            "packs": self._pack_summary(),
            "pack_dir": str(self._pack_dir()),
        }

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        self._load_packs()
        self._load_state()
        self._register_web()
        await self._sync_duplicate_personas()

    # ------------------------------------------------------------------ 总开关 / 状态
    def _state_file(self) -> Path:
        return self._data_dir() / "state.json"

    def _load_state(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        self._enabled = bool(self.config.get("natural_enabled", False))
        try:
            raw = json.loads(self._state_file().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if "enabled" in raw:
                    self._enabled = bool(raw.get("enabled"))
                if raw.get("k") is not None:
                    self._k = int(raw.get("k"))
                if raw.get("max_chars") is not None:
                    self._max_chars = int(raw.get("max_chars"))
        except Exception:  # noqa: BLE001
            pass

    def _save_state(self) -> None:
        try:
            path = self._state_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"enabled": self._enabled, "k": self._k, "max_chars": self._max_chars},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[natural] 开关保存失败: {exc}")

    def _eff_k(self) -> int:
        v = self._k or self._conf_int("natural_k", 5)
        return max(1, min(10, int(v)))

    def _eff_max_chars(self) -> int:
        v = self._max_chars or self._conf_int("natural_max_chars", 1200)
        return max(200, int(v))

    def _pack_summary(self) -> list[dict]:
        out = []
        for pid, pack in self._packs.items():
            out.append({
                "id": pid,
                "name": pack.get("name") or pid,
                "pairs": len(pack.get("pairs") or []),
                "messages": pack.get("total_messages"),
                "kb": round(len(json.dumps(pack, ensure_ascii=False)) / 1024),
            })
        return out

    # ------------------------------------------------------------------ 兼容层
    def _prev_file(self) -> Path:
        return self._data_dir() / "prev_persona.json"

    def _prev_load(self) -> None:
        if self._prev_loaded:
            return
        self._prev_loaded = True
        try:
            raw = json.loads(self._prev_file().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._prev = {str(k): str(v) for k, v in raw.items() if v}
        except Exception:  # noqa: BLE001
            pass

    def _prev_save(self) -> None:
        try:
            path = self._prev_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self._prev, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[natural] 记忆人格保存失败: {exc}")

    def _persona_prompt_by_id(self, persona_id: str) -> str:
        pm = getattr(self.context, "persona_manager", None)
        if pm is None or not persona_id:
            return ""
        try:
            for item in pm.personas_v3:
                if str(item.get("name")) == persona_id:
                    return str(item.get("system_prompt") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ------------------------------------------------------------------ 读会话人格
    async def _session_persona(self, umo: str) -> str:
        try:
            cfg = (
                await sp.get_async(
                    scope="umo",
                    scope_id=str(umo),
                    key="session_service_config",
                    default={},
                )
                or {}
            )
            return str(cfg.get("persona_id") or "")
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ 注入
    @staticmethod
    def _mode_of(persona: str) -> tuple[str, str]:
        """「X 更自然的对话beta」→ (X, "single")；「X 更自然的对话 集合」→ (X, "all")。"""
        name = str(persona or "").strip()
        if name.endswith(SUFFIX_ALL):
            return name[: -len(SUFFIX_ALL)].strip(), "all"
        if name.endswith(SUFFIX):
            return name[: -len(SUFFIX)].strip(), "single"
        # ★ 2026-11-09：不带 beta 的写法也认（「X 更自然的对话」）
        if name.endswith(" 更自然的对话"):
            return name[: -len(" 更自然的对话")].strip(), "single"
        return "", ""

    @staticmethod
    def _base_of(persona: str) -> str:
        return NaturalChat._mode_of(persona)[0]

    @filter.on_llm_request()
    async def inject_examples(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        self._load_state()
        if not self._enabled:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        persona = await self._session_persona(umo)
        base, mode = self._mode_of(persona)
        if not base:
            return

        pack_id = "all" if mode == "all" else self._current
        retriever = self._retrievers.get(pack_id)
        if retriever is None:
            return

        text = str(getattr(event, "message_str", "") or "").strip()
        k = self._eff_k()
        try:
            examples = retriever.search(text, k)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[natural] 检索失败: {exc}")
            return
        if not examples:
            return

        lines = []
        for ex in examples:
            ctx = str(ex.get("context") or "").strip()
            reply = str(ex.get("reply") or "").strip()
            if not reply:
                continue
            lines.append(f"{ctx}\n你:{reply}" if ctx else f"你:{reply}")
        if not lines:
            return

        pack_now = self._packs.get(pack_id) or {}
        prof_lines = self._profile_lines(pack_now)
        block = ""
        if prof_lines:
            block += (
                "\n\n# 你平时说话的样子\n"
                + "\n".join(prof_lines)
            )
        block += "\n\n# 你以前真实发过的话（照这个感觉说，别照抄）\n" + "\n".join(lines)

        max_chars = self._eff_max_chars()
        if len(block) > max_chars:
            block = block[:max_chars] + "…"

        try:
            # ★ 2026-11-09：动态内容放到「用户消息」前，system_prompt 保持静态
            #   （缓存前缀不变，命中率不会因为每轮追加而掉光）
            req.prompt = block.strip() + "\n\n" + (req.prompt or "")
            self._rounds.appendleft({
                "ts": time.time(), "kind": "inject", "umo": umo,
                "persona": persona, "k": len(lines), "chars": len(block),
                "preview": lines[0][:80] if lines else "", "pack": self._current,
                "full": block,
            })
            logger.info(
                f"[natural] 注入 {len(lines)} 条原话（用户消息前）→ {umo}（{pack_id}）"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[natural] 注入失败: {exc}")

    # ------------------------------------------------------------------ 分条发送
    @filter.on_decorating_result()
    async def split_burst(self, event: AstrMessageEvent) -> None:
        """特殊人格下：插件自己发（不带引用/AT），多行拆成多条，间隔 0.4~1.1 秒。"""
        self._load_state()
        if not self._enabled:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        persona = await self._session_persona(umo)
        if not self._mode_of(persona)[0]:
            return

        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return

        lines: list[str] = []
        for comp in chain:
            text = getattr(comp, "text", None)
            if isinstance(text, str) and text.strip():
                for ln in text.split("\n"):
                    ln = ln.strip()
                    if ln:
                        lines.append(ln)
        if not lines:
            return
        # ★ 2026-11-09：只有多行（真正的连发）才接管；
        #   单行交给 AstrBot 正常发（避免把命令回执也拦下来导致重复）
        if len(lines) <= 1:
            return

        # 自己发——绕过 AstrBot 后面的「引用/AT 回复」装饰。
        # ★ stop_event 不足以阻止 respond.stage，必须清空 result.chain，
        #   否则 AstrBot 还会把原文再发一次（实测重复 2 次）
        event.stop_event()
        try:
            result.chain = []
        except Exception:  # noqa: BLE001
            pass
        self._rounds.appendleft({
            "ts": time.time(), "kind": "send", "umo": umo,
            "k": len(lines[:4]), "preview": lines[0][:80] if lines else "",
        })
        logger.info(f"[natural] 自发送 {len(lines)} 条（无引用）→ {umo}")
        await self._send_burst(umo, lines)

    async def _send_burst(self, umo: str, lines: list[str]) -> None:
        for i, line in enumerate(lines[:4]):
            if i:
                await asyncio.sleep(random.uniform(0.4, 1.1))
            try:
                await asyncio.wait_for(
                    self.context.send_message(umo, MessageChain(chain=[Plain(line)])),
                    timeout=20,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[natural] 发送失败: {exc}")
                return

    # ------------------------------------------------------------------ 命令
    @filter.command("自然", alias={"自然对话", "更自然的对话"})
    async def natural_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """查看/切换更自然的对话数据包。"""
        raw = str(arg or "").strip()
        msg = str(getattr(event, "message_str", "") or "").strip()
        stripped = re.sub(r"^[/／]?(?:自然对话|更自然的对话|自然)\s*", "", msg).strip()
        if stripped:
            raw = stripped
        parts = [p for p in re.split(r"[\s\u3000]+", raw) if p]
        head = parts[0] if parts else ""

        if not head or head in ("状态", "status", "?"):
            self._load_state()
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            persona = await self._session_persona(umo)
            pack = self._packs.get(self._current) or {}
            lines = [
                f"更自然的对话：{'开' if self._enabled else '关'}（总开关）",
                f"当前会话人格：{persona or '默认'}"
                + ("（特殊项已选中）" if persona == PERSONA_ID else ""),
                f"数据包：{self._current or '无'}"
                + (f"（{pack.get('name')}，{len(pack.get('pairs') or [])} 组）" if pack else ""),
                "已录入：",
            ]
            for row in self._pack_summary():
                mark = " ←" if row["id"] == self._current else ""
                lines.append(
                    f"  · {row['id']}：{row['name']}，{row['pairs']} 组，{row['kb']} KB{mark}"
                )
            lines.append("开/关：/自然 开｜关 ｜ 切包：/自然 包 <id> ｜ 重载：/自然 重载")
            yield event.plain_result("\n".join(lines))
            return

        if head in ("开", "on", "开启", "打开", "启用"):
            self._load_state()
            self._enabled = True
            self._save_state()
            yield event.plain_result(
                "更自然的对话已开启。\n"
                "在 /人格 里选「更自然的对话beta」时才会实际注入原话。"
            )
            return

        if head in ("关", "off", "关闭", "关掉", "禁用"):
            self._load_state()
            self._enabled = False
            self._save_state()
            yield event.plain_result("更自然的对话已关闭（人格照常，只是不再注入原话）。")
            return

        if head in ("重载", "reload", "刷新"):
            self._load_packs()
            await self._sync_duplicate_personas()
            yield event.plain_result(
                f"已重载：{len(self._packs)} 个数据包"
                + (f"，当前 {self._current}" if self._current else "")
            )
            return

        if head in ("包", "pack") and len(parts) > 1:
            target = parts[1]
            if target not in self._packs:
                yield event.plain_result(
                    f"没有叫「{target}」的包。可选：{', '.join(self._packs) or '无'}"
                )
                return
            self._current = target
            yield event.plain_result(f"已切换到数据包：{target}")
            return

        yield event.plain_result(
            "用法：/自然 ｜ /自然 开｜关 ｜ /自然 重载 ｜ /自然 包 <id>"
        )

    async def terminate(self) -> None:
        """插件卸载：不动人格（用户自己切走即可）。"""
        logger.info("[natural] 已卸载")
