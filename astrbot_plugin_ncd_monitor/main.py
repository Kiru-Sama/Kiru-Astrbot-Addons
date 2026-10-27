"""端到端链路监控插件

把四个环节的数据整合到一个页面：
    宿主机  →  LM Studio  →  AstrBot  →  SnowLuma(QQ)

本插件【不修改任何 AstrBot 源码文件】，压缩摘要的捕获靠运行时给
LLMSummaryCompressor 包一层（插件卸载即恢复）。

数据来源：
    · 压缩摘要      运行时包装 LLMSummaryCompressor.__call__
    · token 用量    @filter.on_llm_response 的 LLMResponse.usage
    · 请求信息      @filter.on_llm_request 的 ProviderRequest
    · 模型/上下文    AstrBot 配置
    · 宿主机         psutil + nvidia-smi
    · SnowLuma      HTTP 探测 + 读它的 OneBot 配置/身份库
    · LM Studio     HTTP 探测

★ 环境相关的东西（LM Studio 端口、SnowLuma 端口和目录）全部可以在
  **AstrBot 面板 → 插件 → 链路监控 → 配置** 里改，不用动代码。
  schema 见同目录 `_conf_schema.json`；留空则就近自动探测，再不行用内置缺省值。
"""

from __future__ import annotations

import asyncio
import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import contextvars
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from astrbot.api import logger, sp, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.web import json_response

# 压缩摘要注入上下文时的特征前缀（见 astrbot/core/agent/context/compressor.py）
SUMMARY_PREFIX = "Our previous history conversation summary:"
SUMMARY_ACK = "Acknowledged the summary of our previous conversation history."

# ---------------------------------------------------------------------------
#  环境相关：全部可以从【插件配置】里改，不用动代码
# ---------------------------------------------------------------------------
#  在 AstrBot 面板 → 插件 → 链路监控 → 配置 里填（schema 见同目录
#  `_conf_schema.json`，AstrBot 会把值存到 data/config/<插件目录名>_config.json）。
#
#  下面这几个只是【缺省值】，用于配置留空时的兜底：
DEFAULT_LM_STUDIO_BASE = "http://127.0.0.1:1234"
DEFAULT_SNOWLUMA_BASE = "http://127.0.0.1:5099"

# AstrBot 安装目录（= 插件文件往上四层：<AstrBot>\data\plugins\<插件>\main.py）
ASTRBOT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def _detect_snowluma_root() -> str:
    """在 AstrBot 安装目录附近找 SnowLuma，找不到返回空串。

    ★ 只试【固定几个候选位置】，绝不全盘遍历 —— 全盘搜会把事件循环拖死，
      而且用户自己填一下配置比等它扫盘快得多。

    覆盖的常见布局（以 NapCat 桌面版为例，AstrBot 和 DATA 是同级）：
        <base>\\ASTRBOT                       ← AstrBot 根
        <base>\\DATA\\components\\SnowLuma     ← 就是它
    """
    base = os.path.dirname(ASTRBOT_ROOT)
    candidates = [
        os.path.join(base, "DATA", "components", "SnowLuma"),
        os.path.join(base, "SnowLuma"),
        os.path.join(ASTRBOT_ROOT, "DATA", "components", "SnowLuma"),
        os.path.join(os.path.dirname(base), "DATA", "components", "SnowLuma"),
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    return ""


def _is_lm_unavailable(text: str) -> bool:
    """这句是不是 AstrBot 的「无模型可用」兜底文案。

    ★ 精确匹配 tool_loop_agent_runner.py:649 的原文。单独抽成函数是为了能测：
      包装器本身是 async generator、闭包着原函数，不方便在测试里驱动。
      而"认不认得出这句话"正是包装器唯一需要判断的东西。

    注意别和另一个分支混了（tool_loop_agent_runner.py:642）：
        "All chat models failed: <类型>: <消息>"   ← 这个带真异常，不需要记
    """
    return "available chat models are unavailable" in (text or "")


def _first_match(directory: str, pattern: str) -> str:
    """在目录里按 pattern 找第一个匹配的文件，找不到返回空串。

    为什么要"找"而不是写死文件名：不同人的 QQ 号不一样，
    OneBot 配置文件叫 `onebot_<QQ号>.json`、身份库在 `data\\<QQ号>\\` 下。
    """
    try:
        import glob

        hits = sorted(glob.glob(os.path.join(directory, pattern)))
        return hits[0] if hits else ""
    except Exception:  # noqa: BLE001
        return ""


def _find_identity_db(sl_dir: str) -> str:
    """在 SnowLuma 的 data\\<账号>\\ 下挑身份库，挑【最新改动的那个】。

    ★ 实测踩过（2026-10-14）：原来用的是 `_first_match(data\\*, snowluma_identity.db)`，
      而 `_first_match` 是 `sorted()[0]` —— 于是换号之后**永远读到旧账号那个库**
      （旧号登出后就不再更新，里面只有 13 个群），新账号进的群一个都查不到名字，
      面板上就只剩下「群 <群号>」。

      活跃账号的身份库一直在写，所以"最新改动的那个"就是当前账号的。
      想完全确定，可以在插件配置里填 `snowluma_identity_db` 显式指定。
    """
    try:
        import glob

        hits = glob.glob(os.path.join(sl_dir, "data", "*", "snowluma_identity.db"))
        best = ""
        best_mtime = -1.0
        for path in hits:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
        return best
    except Exception:  # noqa: BLE001
        return ""


def _find_onebot_config(sl_dir: str) -> str:
    """在 SnowLuma 的 config\\ 下挑 OneBot 配置，挑【最新改动的那个】。

    ★ 和 `_find_identity_db` 是同一个坑，这里当时漏改了（2026-10-17 实测复现）：
      原来用 `_first_match(config, "onebot_*.json")`，它是 `sorted()[0]`，
      而文件名是 `onebot_<QQ号>.json` —— 号小的那个（也就是换号前的旧账号）
      永远排在前面，于是换号之后面板一直展示【旧账号】的 OneBot 端点：

        · has_token 说有   （旧号配了 token，新号是空的）
        · host 显示 127.0.0.1（新号是 0.0.0.0）
        · ws_clients 显示 0 （新号明明有 1 条 AstrBot 的连接）

      三个数全错，排查连接问题时会被它带偏。活跃账号的配置会随设置变动而更新，
      所以"最新改动的那个"就是当前账号的。要完全确定，
      可以在插件配置里填 `snowluma_onebot_config` 显式指定。
    """
    try:
        import glob

        hits = glob.glob(os.path.join(sl_dir, "config", "onebot_*.json"))
        best = ""
        best_mtime = -1.0
        for path in hits:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
        # 一个都没写成功时退回老逻辑，总比空着强
        return best or _first_match(os.path.join(sl_dir, "config"), "onebot_*.json")
    except Exception:  # noqa: BLE001
        return ""

# 插件目录名。前端桥接 SDK 拼出来的请求路径是
#     /api/v1/plugins/extensions/<插件目录名>/<endpoint>
# 后端 _match_registered_web_api() 拿【整段】去和注册的路由做 fullmatch，
# 所以注册路由必须把插件目录名作为第一段带上，否则会返回"未找到该路由"。
# 这里从 __file__ 推导，目录改名也不会失效。
PLUGIN_DIR_NAME = os.path.basename(os.path.dirname(os.path.abspath(__file__)))

MAX_RECORDS = 300


# ---------------------------------------------------------------------------
#  运行时包装的全局状态
# ---------------------------------------------------------------------------
#  这三个包装（Provider.text_chat / LLMSummaryCompressor.__call__ /
#  EstimateTokenCounter.count_tokens）都写在【类】上，但闭包会捕获【实例】。
#  这个组合有个隐蔽的坑：
#
#    star_manager.reload() 的流程是  terminate()  →  star_map 清空  →  load()
#    而 load() 会【新建插件实例】。类上的包装函数还在、闭包还指着旧实例；
#    标记又只写不删，新实例在 __init__ 里看到标记就直接 return。
#
#  结果：新实例注册了 6 条路由，但它自己的 context_snapshots / summaries
#  永远是空的 —— 面板右栏「发给模型的完整上下文」和「压缩摘要」静默失效，
#  【零报错】，只有重启 AstrBot 进程能恢复。
#  而 README / 安装说明推荐的"点刷新图标重载"恰好就会走到这条路。
#
#  两道措施，互为补充：
#    ① _ACTIVE 派发 —— 包装永远把数据写进【当前活着的实例】，与重载无关
#    ② _WRAPS + terminate() 还原 —— 干净卸载，让"禁用即恢复原样"这句话成立
_ACTIVE: "Main | None" = None

# _env() 的初始化锁。它不是给 asyncio 用的，是给【线程】用的：
# api_overview 把两个探测函数丢进 run_in_executor，会并发调 _env()。
# 详见 _env() 里的说明。
_ENV_LOCK = threading.Lock()

# 被我们换掉的原函数：(宿主, 属性名, 原函数, 标记名)。卸载时按它还原。
# ★ 必须放【模块级】：包装是类级别的全局状态，记录也必须是全局的 ——
#   放实例上的话，重载时新实例的 terminate() 还原不了旧实例打上的包装。
# ★ 宿主可以是类，也可以是模块对象（知识库那个入口是模块级函数）——
#   _unwrap_all 用的是 setattr(宿主, 属性名, 原函数)，两者都支持。
_WRAPS: list[tuple[Any, str, Any, str]] = []

# 探活缓存时长（秒）。面板每 5 秒刷一次 /overview，每次都会真去 HTTP 探
# LM Studio 和 SnowLuma —— 实测一天 1.7 万次 GET /v1/models、7 MB 日志。
# 「加载了几个模型」这种信息，20 秒的新鲜度完全够用。
_PROBE_CACHE_SECONDS = 20.0

# 【这一轮调用是不是压缩器发起的】。
# ★ 用 ContextVar 而不是模块级布尔量：两个会话可能一个在压缩、另一个在主回复，
#   全局量会互相串（压缩的流会写进别人的实时块，或者普通回复被误当压缩）。
#   ContextVar 是【每个任务一份】，asyncio 里天然隔离。
_COMPRESSING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ncd_monitor_compressing", default=False
)

# 【当前正在发请求的那个会话】。on_llm_request 里设置，Provider 包装里读取。
# ★ 为什么需要它：Provider 的包装只看得到 kwargs，看不到 event，
#   于是"这次调用属于哪个会话"只能去猜。以前猜的方式是"全局那唯一一条待配对
#   记录"—— 两个会话交错（群里一条 + 私聊一条）时就会配错人。
#   ContextVar 是每个任务一份，asyncio 里天然隔离。
_CURRENT_UMO: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ncd_monitor_current_umo", default=""
)

# 待配对记录的存活上限（秒）。请求发了但响应没回来时防止无限堆积。
_PENDING_TTL = 600.0


# ---------------------------------------------------------------------------
#  全局并发闸门
# ---------------------------------------------------------------------------
#  AstrBot 只有【按会话】的锁（session_lock.py），不同会话是完全并行的。
#  并发数一高，两件事会出问题：① 本地模型只有一个，全挤在一起每轮都变慢；
#  ② 面板上的单槽缓冲（实时输出）会串字。
#
#  这里在 OnWaitingLLMRequestEvent（internal.py:217，拿会话锁【之前】）插一道闸：
#  最多 max_concurrent 个会话同时往下走，其余的 FIFO 排队。
#
#  ★★ 硬约束：这道闸插在"回复链路"正中间，它出错 = 机器人哑巴。
#     所以整段 fail-open —— 任何异常、以及排队超时，一律【直接放行】并打 WARNING。
#     宁可并发超限，也不能不回复。
class _Gate:
    """全局并发闸门：最多 limit 个并发，其余 FIFO 排队。"""

    def __init__(self) -> None:
        self.limit = 2
        # umo -> {"label", "since", "released"}
        self.active: dict[str, dict[str, Any]] = {}
        # FIFO：[{"umo", "label", "since", "ticket"}]
        self.queued: list[dict[str, Any]] = []
        self._ticket = 0
        self._cv: asyncio.Condition | None = None
        self._cv_loop: Any = None
        self.hold_timeout = 300.0
        self.force_released = 0
        # 令牌制：acquire 发一个 token，release 交回 token。
        # ★ 为什么要令牌而不是"按 umo 减计数"：释放点有两个
        #   （on_llm_response 和 after_message_sent），同一轮它们【都会】被调用。
        #   按 umo 减计数的话第二次调用会把计数多减一次、把槽提前放掉；
        #   令牌制下第二次调用找不到 token，直接忽略，天然幂等。
        self._token_seq = 0
        self._token_owner: dict[int, str] = {}

    def _condition(self) -> asyncio.Condition:
        """惰性建 Condition，并绑定到当前事件循环。

        ★ 不能在 __init__ 里建：插件加载时可能还没有 loop。
        ★ loop 换了要重建（AstrBot 重启/测试里多次 asyncio.run）。
        """
        loop = asyncio.get_running_loop()
        if self._cv is None or self._cv_loop is not loop:
            self._cv = asyncio.Condition()
            self._cv_loop = loop
            # 换了 loop，旧的 active/queued 都属于上一个时空，清掉
            self.active.clear()
            self.queued.clear()
        return self._cv

    def _new_token(self, umo: str) -> int:
        self._token_seq += 1
        token = self._token_seq
        self._token_owner[token] = umo
        return token

    async def acquire(self, umo: str, label: str) -> int:
        """占一个槽，返回一个释放用的 token。满了就排队等。

        异常一律放行（fail-open）—— 这道闸插在回复链路上，宁可超限也不能不回复。

        ★ 同一个会话【不重复占槽】：闸门挂在会话锁【之前】（internal.py:217），
          同一会话的第二条消息会先过闸、再卡在自己的会话锁上。若它又占一个槽，
          那个槽就是"有人在用但什么都没干"，后面别的会话等于白等。
        """
        try:
            cv = self._condition()
            async with cv:
                held = self.active.get(umo)
                if held is not None:
                    token = self._new_token(umo)
                    held.setdefault("tokens", set()).add(token)
                    return token

                if self.limit <= 0 or len(self.active) < self.limit:
                    token = self._new_token(umo)
                    self.active[umo] = {
                        "label": label,
                        "since": time.time(),
                        "tokens": {token},
                    }
                    return token
                # 排队：拿号，按号序准入（FIFO）
                self._ticket += 1
                entry = {
                    "umo": umo,
                    "label": label,
                    "since": time.time(),
                    "ticket": self._ticket,
                }
                self.queued.append(entry)
                logger.info(
                    f"[ncd-monitor] 并发已满（{len(self.active)}/{self.limit}），"
                    f"排队：{label} 队列长度={len(self.queued)}"
                )
                try:
                    while True:
                        # 前面没有别人 + 有空槽 → 放行
                        head = self.queued[0] if self.queued else None
                        if (
                            head is entry
                            and len(self.active) < self.limit
                        ):
                            break
                        await asyncio.wait_for(
                            cv.wait(), timeout=max(1.0, self.hold_timeout)
                        )
                except TimeoutError:
                    logger.warning(
                        f"[ncd-monitor] 排队超时（{self.hold_timeout:.0f}s），"
                        f"直接放行以免卡死回复：{label}"
                    )
                finally:
                    if entry in self.queued:
                        self.queued.remove(entry)
                token = self._new_token(umo)
                self.active[umo] = {
                    "label": label,
                    "since": time.time(),
                    "tokens": {token},
                }
                return token
        except Exception as exc:  # noqa: BLE001
            # ★ fail-open：闸门坏了也必须让请求过
            logger.warning(f"[ncd-monitor] 并发闸门异常，直接放行: {exc}")
            token = self._new_token(umo)
            self.active.setdefault(
                umo,
                {"label": label, "since": time.time(), "tokens": set()},
            )["tokens"].add(token)
            return token

    async def release(self, token: int | None) -> None:
        """交回一个 token。该会话的 token 全交回了才真的释放槽位。

        ★ 幂等：交回不存在的 token（例如两个释放点都调了一次）直接忽略。
        """
        if token is None:
            return
        try:
            cv = self._condition()
            async with cv:
                umo = self._token_owner.pop(token, None)
                if umo is None:
                    return  # 已经释放过了
                held = self.active.get(umo)
                if held is not None:
                    held.setdefault("tokens", set()).discard(token)
                    if not held["tokens"]:
                        self.active.pop(umo, None)
                cv.notify_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 释放并发槽失败（忽略）: {exc}")
            umo = self._token_owner.pop(token, None)
            if umo:
                self.active.pop(umo, None)

    async def reap_stale(self) -> list[str]:
        """强释放"占着不放"的槽（响应始终没回来时兜底）。返回被释放的 umo。

        ★ 调用时机：每来一条新消息、以及面板每秒轮询时各一次。
          这样"某个会话的响应丢了"最多卡到下一条消息进来就自愈，
          不会一直占着槽位让后面的人排死。
        """
        try:
            cv = self._condition()
            async with cv:
                now = time.time()
                stale = [
                    umo
                    for umo, info in list(self.active.items())
                    if now - info.get("since", now) > self.hold_timeout
                ]
                for umo in stale:
                    self.active.pop(umo, None)
                    self.force_released += 1
                    logger.warning(
                        f"[ncd-monitor] 并发槽超过 {self.hold_timeout:.0f}s 未释放，"
                        f"强制回收：{umo}（响应可能没回来）"
                    )
                    # ★ 必须连带清掉这些 token 的归属。否则：
                    #   插件侧 _gate_tokens[umo] 还留着陈旧 token，而 _release_gate
                    #   是按 FIFO 取一个交回 —— 它会先交出陈旧的那个（空操作），
                    #   新请求的 token 就永远释放不掉，槽位【再次泄漏】。
                    #   也就是"被兜底救过一次的会话，下次会再漏一次"。
                    for tok in [
                        t for t, owner in list(self._token_owner.items())
                        if owner == umo
                    ]:
                        self._token_owner.pop(tok, None)
                if stale:
                    cv.notify_all()
                return stale
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 回收僵死槽位失败（忽略）: {exc}")
            return []

    def snapshot(self) -> dict[str, Any]:
        """给面板的状态。"""
        now = time.time()
        return {
            "limit": self.limit,
            "active": [
                {
                    "umo": umo,
                    "label": info.get("label", ""),
                    "elapsed": round(now - info.get("since", now), 1),
                    # 同一会话可能有不止一条在跑（它们在会话锁上排队），
                    # 这个数字只影响 /sysinfo 的透明度，不参与限额判断
                    "count": max(1, len(info.get("tokens") or ())),
                }
                for umo, info in self.active.items()
            ],
            "queued": [
                {
                    "umo": e.get("umo", ""),
                    "label": e.get("label", ""),
                    "waited": round(now - e.get("since", now), 1),
                    "pos": idx + 1,
                }
                for idx, e in enumerate(self.queued)
            ],
            "force_released": self.force_released,
        }


_GATE = _Gate()


def _unwrap_all() -> int:
    """还原全部运行时包装，并清掉类上的标记。

    卸载/重载插件时调用。返回还原了几处。
    """
    count = 0
    while _WRAPS:
        cls, attr, original, marker = _WRAPS.pop()
        try:
            setattr(cls, attr, original)
            # ★ 不能用 cls.__dict__.pop() —— 类的 __dict__ 是 mappingproxy（只读），
            #   对它会抛 "'mappingproxy' object has no attribute 'pop'"，
            #   结果【还原失败了但标记还在】→ 新实例继续跳过包装。
            #   这个错误只在真跑一遍时才暴露（第一版就是这么写的）。
            if marker in cls.__dict__:
                delattr(cls, marker)
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 还原 {cls.__name__}.{attr} 失败: {exc}")
    return count


# ---------------------------------------------------------------------------
#  会话类型（用于按会话分流人格）
# ---------------------------------------------------------------------------

_PRIVATE_MESSAGE_TYPE = "FriendMessage"
"""AstrBot 的 MessageType.FRIEND_MESSAGE —— 私聊/好友单聊。

另外两个取值是 GroupMessage（群组）和 OtherMessage（系统消息等）。
需求要的是「私聊一套、其它一切一套」，所以只有它算"私聊"，其余全走公开那套。
"""


def _is_private_session(umo: Any) -> bool:
    """这个会话是不是私聊。

    umo 的拼法是 `平台:MessageType:会话`（AstrBot 自己也是 split(":") 解析的，
    见 conversation_mgr.py:110）。★ umo 有时是 str、有时是 MessageSession 对象，
    所以统一 str() 之后再判 —— 直接当字符串用会在对象上炸。
    """
    text = str(umo or "")
    parts = text.split(":")
    if len(parts) >= 2 and parts[1] in (
        "FriendMessage",
        "GroupMessage",
        "OtherMessage",
    ):
        return parts[1] == _PRIVATE_MESSAGE_TYPE
    # 兜底：万一以后拼法变了，别判成"全是公开"（那会让私密人格永远不生效）
    return _PRIVATE_MESSAGE_TYPE in text


# ---------------------------------------------------------------------------
#  思考过程（reasoning）的剥离
# ---------------------------------------------------------------------------

def _is_think_part(part: Any) -> bool:
    """这个内容块是不是"思考"。

    运行时可能是 ThinkPart（pydantic 对象），也可能是 dict 形态的
    {"type": "think", "think": ...} —— 两种都认（跟 _content_text 一样的教训）。
    """
    if isinstance(part, dict):
        return part.get("type") == "think"
    return getattr(part, "type", None) == "think"


def _strip_think_parts(messages: list[Any]) -> int:
    """把消息里的 ThinkPart 摘掉，返回摘掉了几段。

    ★ 只动 ThinkPart，别的都不碰：
      · 正文（TextPart）原样保留；
      · **tool_calls 必须留着** —— 摘掉它工具链就断了；
      · 摘完这条消息什么都不剩（纯思考）就整条丢弃，免得给供应商一个空壳。
    ★ 原地改传进来的那个列表（messages[:] = …）：AstrBot 在压缩决策和落盘时
      用的就是同一个列表对象，换成新对象等于没改。

    为什么要剥：推理模型吐的思考会以 ThinkPart 写进 run_context.messages，
    而那份列表既是【压缩决策的输入】又是【最后落盘的内容】——实测一条会话攒了
    10 段、合计 13673 字，全在 8192 的上下文里反复重发。
    """
    removed = 0
    kept = []
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            kept.append(msg)
            continue
        remaining = [p for p in content if not _is_think_part(p)]
        if len(remaining) != len(content):
            removed += len(content) - len(remaining)
            if not remaining and not getattr(msg, "tool_calls", None):
                continue
            msg.content = remaining
        kept.append(msg)
    if removed:
        messages[:] = kept
    return removed


def _count_think_chars(messages: list[Any]) -> int:
    """数一下这些消息里思考一共多少字（剥离前调用，用于记账）。"""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for p in content:
            if not _is_think_part(p):
                continue
            if isinstance(p, dict):
                total += len(p.get("think") or "")
            else:
                total += len(getattr(p, "think", "") or "")
    return total


# ---------------------------------------------------------------------------
#  流式 chunk 的解析
# ---------------------------------------------------------------------------

def _is_delta_chunk(chunk: Any) -> bool:
    """这个流式 chunk 是【增量片段】还是【最终完整结果】。

    ★ 依据 LLMResponse.is_chunk：
        openai_source.py:631  造增量片段时传了 is_chunk=True
        openai_source.py:831  造最终完整结果时没传 → False
      provider.py:154 的 docstring 也写明了「在生成的最后会返回一次完整的结果」。
    """
    return bool(getattr(chunk, "is_chunk", False))


# 跨插件约定：工具箱的「网页总结」调用会在 prompt 开头带上这个标记自报身份，
# 本插件据此把它排除在会话统计之外。两侧都各自定义了一份，改一处要同步另一处。
# （toolbox/main.py 的 _summarize_page 里是同一个字面量。）
_INTERNAL_REQUEST_MARKERS = ("[内部·网页总结]",)


def _is_internal_request(contexts: Any, prompt: str) -> bool:
    """这次 provider 调用是不是本插件之外的内部调用（不是某个会话的对话）。

    只看 prompt 和第一条消息的开头 —— 标记是我们自己写在最前面的，
    网页正文接在它后面，所以开头比对比全文包含更不容易误伤。
    """
    candidates = [prompt or ""]
    for m in contexts or []:
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content")
        candidates.append(_plugin_content_text(content))
        break
    for text in candidates:
        stripped = (text or "").lstrip()
        if any(stripped.startswith(mark) for mark in _INTERNAL_REQUEST_MARKERS):
            return True
    return False


def _plugin_content_text(content: Any) -> str:
    """模块级版本的 content→文本，供 _is_internal_request 使用。

    ★ 不复用 Plugin._content_text 是因为那个要 instance；这里只要「够判断前缀」
      的一小段文本，不需要它那套图片标记解析。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    return str(content)


def _has_final_text(chunk: Any) -> bool:
    """这个非 delta chunk 是不是【真的】带着完整文本的最终结果。

    只看 is_chunk=False 是不够的：那是 openai_source 的约定，换个 provider、
    或者同一家的 reasoning / 工具调用段，都可能中途吐非 delta 的 chunk。
    那种空壳一旦被当成 final，面板会安静地显示错内容。

    ★ 判据用 completion_text 和 result_chain 两条都认：
      最终结果走 completion_text（测试-流式捕获.py 里的 FakeChunk 就是照这个形状造的）；
      增量对象则把文本放在 result_chain 里。两条都空才算空壳。
    """
    text = getattr(chunk, "completion_text", None)
    if isinstance(text, str) and text.strip():
        return True
    chain = getattr(chunk, "result_chain", None)
    for comp in getattr(chain, "chain", None) or []:
        part = getattr(comp, "text", None)
        if isinstance(part, str) and part.strip():
            return True
    return False


def _chunk_text(chunk: Any) -> str:
    """取出这个增量 chunk 里【新增的那段文本】。

    ★ 为什么读 result_chain 而不是 completion_text：
      openai_source._query_stream 把 llm_response 造成【一个对象反复复用】
      （:631 创建，:664 每轮开头把 completion_text 清成 ""），
      增量文本被放进 result_chain = MessageChain([Plain(delta)])。
      只读 completion_text 的话，每个增量片段拿到的都是空串。

    ★ 同一个"复用对象"的事实还有个后果：
      不能把 chunk 对象攒起来事后统一读 —— 攒到最后大家是同一个对象。
      必须在 yield 的那一刻就把文本抠出来。
    """
    chain = getattr(chunk, "result_chain", None)
    parts: list[str] = []
    for comp in getattr(chain, "chain", None) or []:
        text = getattr(comp, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if parts:
        return "".join(parts)
    text = getattr(chunk, "completion_text", None)
    return text if isinstance(text, str) else ""


class _StreamSummary:
    """流结束时【没等到完整结果】的兜底载体。

    OpenAI 那条路一定会在最后补一个完整结果，但别的 provider 不一定。
    字段名与 LLMResponse 对齐，好让 _attach_snapshot_response 不用为流式改一行。
    """

    def __init__(self, text: str, usage: Any, tool_calls: list[str]) -> None:
        self.completion_text = text
        self.reasoning_content = None
        self.tools_call_name = tool_calls
        self.usage = usage


class Main(star.Star):
    def __init__(
        self, context: star.Context, config: dict[str, Any] | None = None
    ) -> None:
        super().__init__(context, config)
        self.context = context
        # ★ 插件配置（AstrBot 面板 → 插件 → 配置）。
        #   加载器只在【存在 _conf_schema.json】时才会用 config= 关键字实例化，
        #   否则回退成只传 context —— 所以这里必须给默认值，不能写成必填参数。
        #   Star.__init__ 自己【不保存】config，得我们存。
        self._conf: dict[str, Any] = config or {}
        # 环境解析结果缓存（配置改了要重启插件才生效，不热更新）
        self._env_cache: dict[str, str] | None = None

        # 思考剥离的累计账（面板上用来量化"思考到底吃了多少上下文"）
        self.reasoning_stripped_segments = 0
        self.reasoning_stripped_chars = 0
        # 最近几次思考原文（只给面板看；模型下一轮看不到）
        self._thinkings: deque[dict[str, Any]] = deque(maxlen=20)
        # 事件流：对话/工具/生成的关键节点（面板时间线）
        self._timeline: deque[dict[str, Any]] = deque(maxlen=200)

        self.started_at = time.time()

        # ---- 运行数据（全部在内存里，重启即清空）----
        self.summaries: deque[dict[str, Any]] = deque(maxlen=100)
        self.compression_count = 0
        self.llm_calls: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)
        self.context_series: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)
        self.last_prompt_tokens = 0
        # 全局 Token 用量累计（自插件加载）。单独累计而不是对 llm_calls 求和：
        # 那个 deque 只有 300 条，滚掉之后总数会跟着变小。
        self._usage_totals: dict[str, int] = {
            "input": 0,
            "cached": 0,
            "output": 0,
            "calls": 0,
        }
        # 最近一次请求的上下文构成（本地估算）：系统提示词 / 工具定义 / 对话消息
        self._ctx_breakdown: dict[str, Any] = {
            "system": 0,
            "tools": 0,
            "messages": 0,
            "time": 0,
            "umo": "",
        }
        # 每日 Token 账本：按天一个文件，永久保留
        self._token_day_cache: dict[str, dict[str, int]] = {}
        self._token_daily_cache: dict[str, Any] | None = None
        self._token_daily_cache_ts = 0.0
        self._token_day_lock = threading.Lock()
        # 「无模型可用」现场（见 _patch_lm_diag）：只留最近 10 次
        self._lm_unavailable: deque[dict[str, Any]] = deque(maxlen=10)

        # 主动回复（群里没 @ 它、它自己插话）的计数
        self.active_reply_count = 0
        # 上一轮请求的待配对记录：umo -> 请求侧信息，响应回来时按 umo 取。
        # ★ 这里曾经是【单槽】（dict | None），于是两个会话交错时后一条
        #   会把前一条覆盖掉 —— 先发的那条响应回来就配到了别人的
        #   group_name / is_active_reply / 起始时间。实测表现：
        #   群里的主动回复在面板上【没有「主动」标记、来源只剩群号没有群名】，
        #   而且耗时会被算成"自己的响应时刻 − 别人的请求时刻"（实测 10.1 秒，
        #   正好等于那个差值）。报告里记的 4.3 就是它。
        self._pending: dict[str, dict[str, Any]] = {}
        # 最近一次真正发出去的请求前缀指纹（umo -> 指纹）。
        # ★ 只读诊断：不参与任何请求构造，只用来回答"缓存断在哪一段"。
        self._prefix_by_umo: dict[str, dict[str, Any]] = {}
        # 最近一次请求的 umo：contextvar 在个别 provider/线程上下文里会丢，
        # 用它给前缀指纹兜底，避免缓存诊断整块空白。
        self._last_umo = ""
        # 群号 -> 群名，来自 SnowLuma 的身份库（惰性加载）
        self._group_names: dict[str, str] = {}
        self._group_names_loaded = False
        # 上次读到时的库文件 mtime —— 变了才重读，群改名不用重载插件
        self._group_names_mtime: float = 0.0

        # 每次真实发给模型的完整上下文快照（由 Provider.text_chat 包装捕获）
        # ★ 落盘保存：重载插件会把内存清空，表现成面板「还没有抓到请求」，
        #   而其实刚才还有一堆。存到 plugin_data（**不能存插件目录**——
        #   那里会被打包分享出去，里面全是聊天内容）。
        self.context_snapshots: deque[dict[str, Any]] = deque(maxlen=40)
        self._snapshot_seq = 0
        self._snap_cache = os.path.join(
            ASTRBOT_ROOT, "data", "plugin_data", "ncd_monitor", "contexts.json"
        )
        self._snap_saved_at = 0.0
        self._load_snapshots()

        # 实时输出缓冲：模型【正在逐字生成】的内容，供右栏底部那块显示。
        # 开流式（streaming_response）后，主对话走 text_chat_stream，
        # 这里就是那条路上边收边攒的文本。
        #
        # ★ 按【会话】分路（umo -> 缓冲）。以前是全局唯一一块，两个会话
        #   同时生成时会被互相覆盖、文字逐字混在一起。见 _live_key 的说明。
        self._lives: dict[str, dict[str, Any]] = {}
        # 实时输出的自增序号（只用来排序，不用时间戳 —— 见 _live_start）
        self._live_seq = 0

        # 本会话持有的并发 token（umo -> [token, ...]）。
        # 两个释放点（on_llm_response / after_message_sent）都从这里取一个交回，
        # 取不到就说明已经放过了 —— 见 _release_gate。
        self._gate_tokens: dict[str, list[int]] = {}
        # 被包装了 text_chat_stream 的类名（与 text_chat 分开记，便于排查到底是哪条没包上）
        self._patched_stream_names: list[str] = []

        # 缩略图：hash -> 原图路径（每次抓上下文时登记）
        self._thumb_paths: dict[str, str] = {}
        # 缩略图磁盘缓存目录（只算一次，之后直接读）
        self._thumb_dir = os.path.join(os.path.dirname(__file__), "thumbcache")

        # ---- 宿主机资源采样（1 秒一次，供前端画趋势图）----
        # ★ GPU 为什么不能每秒 spawn 一次 nvidia-smi：
        #   实测单次调用约 100ms，每秒一次 = 常驻吃掉约 10% 单核。
        #   所以改成【一个常驻的流式进程】nvidia-smi -lms 1000，
        #   它每秒自己吐一行，我们读 stdout 就行。
        # psutil 那侧只有约 0.9ms，随便调。
        self.sys_samples: deque[dict[str, Any]] = deque(maxlen=self.SYS_SAMPLE_KEEP)
        # 工具调用：最近事件 + 汇总统计（面板用，不进模型）
        self._tool_events: deque[dict[str, Any]] = deque(maxlen=120)
        self._tool_stats: dict[str, dict[str, float]] = {}
        self._tool_start: dict[str, float] = {}
        self._sys_task: Any = None      # asyncio.Task，惰性启动
        self._gpu_proc: Any = None      # 常驻 nvidia-smi 进程
        self._gpu_latest: dict[str, Any] | None = None

        # ---- 注册 Web API（前端用 AstrBotPluginPage.apiGet 调用）----
        # 注意路由格式：/<插件目录名>/<自定义路径>
        try:
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/overview",
                self.api_overview,
                ["GET"],
                "链路监控总览",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/history",
                self.api_history,
                ["GET"],
                "上下文用量与请求历史",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/summaries",
                self.api_summaries,
                ["GET"],
                "上下文压缩摘要列表",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/contexts",
                self.api_contexts,
                ["GET"],
                "每次发给模型的完整上下文",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/thumb/<key>",
                self.api_thumb,
                ["GET"],
                "图片缩略图（96px，按路径 hash 缓存）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/sysinfo",
                self.api_sysinfo,
                ["GET"],
                "宿主机资源（CPU/GPU/显存/内存）1 秒采样 + 趋势",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/search-stats",
                self.api_search_stats,
                ["GET"],
                "搜索 / 抓取统计（来自链路工具箱）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/state/<name>",
                self.api_state,
                ["GET"],
                "插件发布到总线的状态快照（ncd_bus/state/<name>.json）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/tools",
                self.api_tools,
                ["GET"],
                "工具调用：最近事件 + 汇总",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/thinking",
                self.api_thinking,
                ["GET"],
                "最近几次思考内容（仅面板）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/gen-thumb/<idx>",
                self.api_gen_thumb,
                ["GET"],
                "本地生成卡片的缩略图（按 recent 下标）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/timeline",
                self.api_timeline,
                ["GET"],
                "事件流：对话请求/响应 + 工具调用",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/gen-full/<idx>",
                self.api_gen_full,
                ["GET"],
                "点开大图：按 recent 下标返回原图 base64",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-monitor/audit-thumb/<idx>",
                self.api_audit_thumb,
                ["GET"],
                "审核明细缩略图（被拦的小图）",
            )
            logger.info(
                f"[ncd-monitor] 已注册 14 个 Web API，路由前缀 /{PLUGIN_DIR_NAME}/ncd-monitor/"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] 注册 Web API 失败: {exc}")

        self._patch_compressor()
        self._patch_provider()
        self._patch_token_counter()
        self._patch_runner()
        self._patch_lm_diag()
        self._patch_persona()
        self._patch_kb()
        self._patch_quote_guard()
        # ★ 把自己登记成"当前活着的实例" —— 三个包装都通过 _ACTIVE 派发，
        #   而不是把 self 捕获进闭包。重载时新实例会覆盖它，
        #   于是数据自然流向新实例（详见模块顶部 _ACTIVE 的说明）。
        global _ACTIVE
        _ACTIVE = self
        logger.info("[ncd-monitor] 链路监控插件已加载")

    # =================================================================
    #  生命周期：采样任务的启动与收尾
    # =================================================================

    async def initialize(self) -> None:
        self._ensure_sampler()

    async def terminate(self) -> None:
        """卸载/重载插件时收尾 —— 否则常驻的 nvidia-smi 会变成孤儿进程。

        ★ 这里必须显式接 CancelledError：await 一个被 cancel 的 task 抛的是
          CancelledError，它继承自 BaseException，`except Exception` 接不到 ——
          之前就是这么漏掉的：清理被跳过，nvidia-smi 留了下来。

        ★ 另外还要【还原三个运行时包装】：
          不还原的话，重载后类上活着的包装仍把数据写进旧实例，
          新实例的 context_snapshots 永远是空的 —— 右栏和压缩摘要静默失效。
          而且那样"插件禁用即恢复原样"这句话也是假的（标记只写不删）。
        """
        global _ACTIVE
        _ACTIVE = None
        restored = _unwrap_all()
        if restored:
            logger.info(f"[ncd-monitor] 已还原 {restored} 处运行时包装（卸载/重载）")

        task = self._sys_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # 预期之内
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ncd-monitor] 采样任务收尾异常: {exc}")
        proc = self._gpu_proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                # kill 之后等一下：让 subprocess transport 正常收尾。
                # （不 wait 的话管道要等 GC 才关，解释器退出时会刷一堆
                #   "Event loop is closed" / "unclosed transport" 噪音。）
                # 这里【不在】取消上下文里，所以 await 是安全的。
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ncd-monitor] 结束 nvidia-smi 失败: {exc}")
        self._sys_task = None
        self._gpu_proc = None
        srv = getattr(self, "_file_srv", None)
        if srv is not None:
            try:
                await asyncio.to_thread(srv.shutdown)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ncd-monitor] 关闭图片服务失败: {exc}")
            try:
                srv.server_close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ncd-monitor] 关闭图片服务 socket 失败: {exc}")
            self._file_srv = None
        logger.info("[ncd-monitor] 资源采样已停止，nvidia-smi 已结束")

    # =================================================================
    #  一之二、宿主机资源采样（1 秒一次）
    # =================================================================

    def _ensure_sampler(self) -> None:
        """惰性启动采样任务（幂等）。

        为什么惰性：`initialize()` 不一定在所有加载路径上被调到，
        而 /sysinfo 一定会被前端调到 —— 两条路都走这个函数，跑起来就不会重复起。
        """
        if self._sys_task is not None and not self._sys_task.done():
            return
        try:
            self._sys_task = asyncio.create_task(self._sample_system_loop())
            logger.info(
                f"[ncd-monitor] 宿主机资源采样已启动（{self.SYS_SAMPLE_INTERVAL}s 一次，"
                f"保留 {self.SYS_SAMPLE_KEEP} 个样本）"
            )
        except RuntimeError as exc:
            # 没有运行中的事件循环（比如在同步上下文里被调到）
            logger.warning(f"[ncd-monitor] 启动采样任务失败: {exc}")

    async def _gpu_stream_loop(self) -> None:
        """常驻一个 `nvidia-smi -lms 1000`，每秒读一行。

        ★ 不用「每秒 spawn 一个 nvidia-smi」：实测单次约 100ms，
          1Hz 等于常驻 10% 单核。流式模式只有进程启动那一次开销。
        进程意外退出就 5 秒后重启（跑监控不能因为 GPU 驱动抽一下就永久瞎掉）。
        """
        query = (
            "--query-gpu=name,memory.used,memory.total,"
            "utilization.gpu,temperature.gpu,power.draw"
        )
        ms = str(int(self.SYS_SAMPLE_INTERVAL * 1000))
        while True:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "nvidia-smi",
                    query,
                    "--format=csv,noheader,nounits",
                    "-lms",
                    ms,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self._gpu_proc = proc
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line:
                        continue
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 6:
                        continue
                    try:
                        self._gpu_latest = {
                            "name": parts[0],
                            "mem_used": int(parts[1]),
                            "mem_total": int(parts[2]),
                            "util": int(parts[3]),
                            "temp": int(parts[4]),
                            "power": parts[5],
                        }
                    except ValueError:
                        continue  # 某些驱动会吐 "N/A"
                await proc.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ncd-monitor] nvidia-smi 流式中断: {exc}")
            finally:
                # ★ 取消读任务【并不会】结束子进程 —— 不显式 kill 就是孤儿进程。
                #   这里只同步 kill 不 await：在 CancelledError 的清理路径里
                #   再 await 可能立刻又抛 CancelledError，反而把清理打断。
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.sleep(5)

    async def _sample_system_loop(self) -> None:
        gpu_task = asyncio.create_task(self._gpu_stream_loop())
        try:
            import psutil
        except Exception:  # noqa: BLE001
            psutil = None
            logger.warning("[ncd-monitor] 没装 psutil，CPU/内存趋势将缺失")

        # 第一次 cpu_percent 一定是 0（要两次调用才有差值），先喂一次
        if psutil is not None:
            psutil.cpu_percent(interval=None)

        try:
            while True:
                sample: dict[str, Any] = {"t": time.time()}
                try:
                    if psutil is not None:
                        vm = psutil.virtual_memory()
                        # interval=None：非阻塞，取「距上次调用」的均值。
                        # 我们正好 1 秒一次，所以这就是每秒的真实占用。
                        sample["cpu"] = psutil.cpu_percent(interval=None)
                        sample["mem"] = vm.percent
                        sample["mem_used_gb"] = round(vm.used / 1024**3, 1)
                        sample["mem_total_gb"] = round(vm.total / 1024**3, 1)
                    g = self._gpu_latest
                    if g:
                        sample["gpu"] = g["util"]
                        sample["vram"] = (
                            round(g["mem_used"] / g["mem_total"] * 100, 1)
                            if g["mem_total"]
                            else 0.0
                        )
                        sample["vram_used"] = g["mem_used"]
                        sample["vram_total"] = g["mem_total"]
                        sample["gpu_temp"] = g["temp"]
                        sample["gpu_power"] = g["power"]
                        sample["gpu_name"] = g["name"]
                    self.sys_samples.append(sample)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[ncd-monitor] 采样失败: {exc}")
                await asyncio.sleep(self.SYS_SAMPLE_INTERVAL)
        finally:
            gpu_task.cancel()


    # =================================================================
    #  一之三、让图片的 token 开销真正参与压缩判断
    # =================================================================

    def _patch_token_counter(self) -> None:
        """让图片的 token 开销算进压缩判断（否则图片永远清不掉）。

        背景（AstrBot 的两段代码互相打架）：

          ① EstimateTokenCounter 本来【是算图片的】：
                 IMAGE_TOKEN_ESTIMATE = 765     # 一张 1024x1024 图的开销
                 ...
                 elif isinstance(part, ImageURLPart):
                     total += IMAGE_TOKEN_ESTIMATE
             注释里还写着："宁可偏高触发压缩也不要偏低导致 API 报错。"

          ② 但 count_tokens() 开头有一句短路：
                 if trusted_token_usage > 0:
                     return trusted_token_usage
             而 trusted_token_usage 来自
                 conversation.token_usage
                     = 上次 LLM 响应的 usage.total
                     （见 tool_loop_agent_runner.py:816 / :870）
             这个值只有【文本】token —— 实测 60 条消息含 24 张图时，
             prompt_tokens 仍然只有 ~7500，图片完全没算进去。

        后果：
            图片不断累积 → 视觉编码负担越来越重（每次请求变慢）
            但 usage.total 不涨 → 永远到不了 82% → 压缩永远不触发
            → 图片无限堆积，而且面板上【看不出任何征兆】
              （曲线不涨、压缩次数一直是 0）

        修法：
            估算值和 API 值都算一遍，取较大的那个。这正好实现了
            AstrBot 注释里写的"宁可偏高"——纯文字时用 API 的精确值，
            有图片时估值会把图片算进去，该压缩就压缩。

        代价：
            ⚠️ 依赖 EstimateTokenCounter.count_tokens 这个方法名；
               AstrBot 大版本改名会失效（不影响主流程，只是又回到
               "图片不计入压缩判断"的老行为）。
            ✅ 插件禁用/卸载即恢复原样。
        """
        try:
            from astrbot.core.agent.context.token_counter import EstimateTokenCounter

            # 用 __dict__ 而不是 getattr：属性会继承，父类的标记会被子类读到。
            # （这个类目前没有子类，但统一写法免得以后踩坑。)
            if EstimateTokenCounter.__dict__.get("_ncd_monitor_patched", False):
                logger.info("[ncd-monitor] token 计数器已被包装过，跳过")
                return

            original = EstimateTokenCounter.count_tokens

            def patched(  # noqa: ANN001
                self_, messages, trusted_token_usage=0, *args, **kwargs
            ):
                """按「估算值」返回，但在有可信用量时取两者较大者。

                ★ 签名留 *args/**kwargs 是**升级保险**，不是为了现在：
                  这里是【类级替换】方法本身，AstrBot 哪天给 count_tokens 加个参数，
                  Python 会在【进入函数体之前】就抛 TypeError —— 那时还没进 try，
                  异常会直接冒到压缩链路里。加两个星号，多出来的参数原样透传即可。
                  （现状是安全的：AstrBot 全部调用点最多传 2 个位置参数，
                    manager.py:69 是 count_tokens(result, trusted_token_usage)。）

                ★ 失败兜底不能返回 0：0 的含义是「上下文是空的」，会让压缩判断偏低、
                  永远不触发压缩，最后以 Context size exceeded 收场。这里退回一个
                  粗糙但量级正确的估计（把消息对象 repr 出来按字符数折算）。
                """
                try:
                    estimated = original(self_, messages, 0, *args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        f"[ncd-monitor] 估算 token 失败，退回粗略估计"
                        f"（压缩判断会偏保守）: {exc}"
                    )
                    try:
                        rough = len(str(messages)) // 3
                    except Exception:  # noqa: BLE001
                        rough = 0
                    return trusted_token_usage if trusted_token_usage > 0 else rough
                if trusted_token_usage > 0:
                    return max(trusted_token_usage, estimated)
                return estimated

            EstimateTokenCounter.count_tokens = patched
            EstimateTokenCounter._ncd_monitor_patched = True  # type: ignore[attr-defined]
            # 登记起来，卸载时还原（见模块顶部 _WRAPS 的说明）
            _WRAPS.append(
                (EstimateTokenCounter, "count_tokens", original, "_ncd_monitor_patched")
            )
            logger.info(
                "[ncd-monitor] 已包装 EstimateTokenCounter.count_tokens"
                "（图片的 765 token/张 现在会计入压缩判断）"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] 包装 token 计数器失败: {exc}")


    # =================================================================
    #  一之二、抓取【真正发给模型】的完整上下文
    # =================================================================

    def _patch_provider(self) -> None:
        """准备包装各个 Provider 类的 text_chat，抓取真正发给模型的完整上下文。

        三个关键点（每一个都踩过坑）：

        ① 为什么不用 on_llm_request 拿上下文：
           那个事件在 internal.py 里触发时，真正的消息列表还在 agent runner
           内部（astr_main_agent.py 的 agent_runner.reset），还没组装好。
           runner 最后调的是：
               self.provider.text_chat(contexts=self.run_context.messages, ...)
           所以包 text_chat 才能拿到【真正发出去的东西】。

        ② 为什么不能只包基类 Provider.text_chat：
           真正被调用的是【子类覆写】的版本，例如
               openai_source.py:1179  ProviderOpenAIOfficial.text_chat
           只包基类的话包装根本不会被走到 —— 实测就是这个原因。

        ③ 为什么不能只在插件加载时包一次：
           插件是在 AstrBot 启动过程中加载的，那时候 provider source 模块
           可能【还没导入】，Provider.__subclasses__() 是空的。
           实测日志：已包装 1 个 Provider 类的 text_chat: ['Provider']
                     ↑ 只有基类，抓不到任何东西

           所以改成【惰性补包】：插件加载时试一次，之后每次收到 LLM 请求
           前再补一次（那时 provider 一定加载完了）。遍历很便宜，幂等。

        额外好处：上下文压缩也是调 provider.text_chat 完成的，
        所以压缩请求（输入=被压缩的旧对话，输出=摘要）也会被抓到。
        """
        self._patched_provider_names: list[str] = []
        self._preimport_provider_sources()
        self._ensure_providers_patched()

    def _preimport_provider_sources(self) -> None:
        """尝试预导入所有 provider source 模块。

        启动阶段可能失败（循环依赖），失败了也没关系 ——
        _ensure_providers_patched() 会在第一次请求时补上。
        但这里【会把失败原因记进日志】，不再静默吞掉。
        """
        import importlib
        import pkgutil

        try:
            from astrbot.core.provider import sources as src_pkg
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 拿不到 provider.sources 包: {exc}")
            return

        failed: list[str] = []
        count = 0
        for mod in pkgutil.iter_modules(src_pkg.__path__):
            if mod.name in ("request_retry", "mimo_api_common"):
                continue  # 工具模块，没有 Provider 类
            try:
                importlib.import_module(f"{src_pkg.__name__}.{mod.name}")
                count += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{mod.name}({type(exc).__name__})")

        logger.info(
            f"[ncd-monitor] 预导入 provider source: 成功 {count} 个"
            + (f"，失败 {len(failed)} 个: {failed}" if failed else "，全部成功")
        )

    def _ensure_providers_patched(self) -> int:
        """遍历 Provider 的所有子类，把自己定义了 text_chat 的包上。

        幂等，可以反复调用（每次 LLM 请求前都会调一次，很便宜）。
        Returns: 本次累计已包装的类数量。
        """
        try:
            from astrbot.core.provider.provider import Provider

            def wrap(cls) -> None:
                # ★ 必须用 cls.__dict__ 判断，不能用 getattr ——
                #   属性会【继承】：基类 Provider 被包上后设了
                #   _ncd_monitor_patched=True，子类 getattr 也会读到 True，
                #   结果所有子类都被跳过（实测踩到的就是这个）。
                if cls.__dict__.get("_ncd_monitor_patched", False):
                    # 只有【本模块实例】登记过的包装才算数。重载时旧模块可能只还原了一部分，
                    # 类上留下标记却不在这份 _WRAPS 里；此时必须清标记重包，否则新实例
                    # 永远跳过 Provider，缓存诊断/实时输出会整块空白。
                    if any(c is cls and a == "text_chat" for c, a, _, _ in _WRAPS):
                        return
                    try:
                        delattr(cls, "_ncd_monitor_patched")
                    except AttributeError:
                        pass
                if "text_chat" not in cls.__dict__:
                    return
                original = cls.__dict__["text_chat"]

                async def patched(self_, *args, **kwargs):  # noqa: ANN001, ANN202
                    # ★ 实例【不】捕获进闭包，运行时再取当前活着的那个。
                    #   捕获的话，重载后新实例的数据会写进旧实例，
                    #   页面上就是"什么都没抓到"却零报错。
                    plugin = _ACTIVE
                    if plugin is None:
                        return await original(self_, *args, **kwargs)
                    sid = None
                    try:
                        sid = plugin._capture_text_chat(kwargs, args)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"[ncd-monitor] 抓取上下文失败: {exc}")

                    try:
                        if _COMPRESSING.get():
                            # ★ 压缩器那次调用改走流式，让摘要在右栏逐字显示出来。
                            #   压缩器只取 completion_text（compressor.py:279），
                            #   而 text_chat_stream 最后一次 yield 就是完整结果
                            #   （provider.py:154），所以这样替换是等价的。
                            #   失败必须退回非流式 —— 监控代码不能把压缩搞挂。
                            try:
                                resp = await plugin._run_compress_stream(
                                    self_, args, kwargs
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.error(
                                    "[ncd-monitor] 压缩流式失败，退回非流式"
                                    f"（压缩本身不受影响）: {exc}"
                                )
                                resp = await original(self_, *args, **kwargs)
                        else:
                            resp = await original(self_, *args, **kwargs)
                    except Exception:
                        plugin._mark_snapshot_error(sid)
                        raise
                    try:
                        plugin._attach_snapshot_response(sid, resp)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"[ncd-monitor] 关联响应失败: {exc}")
                    return resp

                patched.__name__ = getattr(original, "__name__", "text_chat")
                cls.text_chat = patched
                cls._ncd_monitor_patched = True  # type: ignore[attr-defined]
                _WRAPS.append((cls, "text_chat", original, "_ncd_monitor_patched"))
                self._patched_provider_names.append(cls.__name__)
                logger.info(
                    f"[ncd-monitor] 新包装 Provider 子类: {cls.__name__}"
                    f"（{cls.__module__}）"
                )

            def wrap_stream(cls) -> None:
                """包装 text_chat_stream —— 开流式之后【主对话走的就是它】。

                ★ 不包这一条会怎样：tool_loop_agent_runner.py:515-516 按开关分流，
                  streaming=True 时调的是 text_chat_stream，于是 text_chat 的包装
                  整条被绕过去 —— 面板右栏「发给模型的完整上下文」直接变空、
                  而且零报错。表现和"还没抓到请求"一模一样，很容易误判。

                ★ 包装体必须是 async generator（原函数就是），不能是普通协程：
                  要一边把 chunk 原样转发给 AstrBot，一边在旁边攒文本。
                  【绝不能吞异常、也不能改变 yield 的节奏】—— 一旦改了，
                  AstrBot 的流式解析、工具调用累积都会跟着变。
                """
                if cls.__dict__.get("_ncd_stream_patched", False):
                    if any(
                        c is cls and a == "text_chat_stream"
                        for c, a, _, _ in _WRAPS
                    ):
                        return
                    try:
                        delattr(cls, "_ncd_stream_patched")
                    except AttributeError:
                        pass
                if "text_chat_stream" not in cls.__dict__:
                    return
                original = cls.__dict__["text_chat_stream"]

                async def patched_stream(self_, *args, **kwargs):  # noqa: ANN001, ANN202
                    plugin = _ACTIVE
                    if plugin is None:
                        async for chunk in original(self_, *args, **kwargs):
                            yield chunk
                        return

                    sid = None
                    # ★ 压缩那次调用【不要在这里再抓一次】：
                    #   它是从 text_chat 的包装进来的（_run_compress_stream），
                    #   那边已经 _capture_text_chat 过了。这里再抓一次会在面板上
                    #   留下两条一模一样的 compress 快照（实测同一毫秒两条）。
                    if not _COMPRESSING.get():
                        try:
                            sid = plugin._capture_text_chat(kwargs, args)
                        except Exception as exc:  # noqa: BLE001
                            logger.error(f"[ncd-monitor] 抓取流式上下文失败: {exc}")

                    try:
                        # ★ 压缩器发起的流式调用要标成 compress：
                        #   否则它会把 _run_compress_stream 刚开的那块
                        #   以 reply 模式覆盖掉 —— 黄框永远不亮。
                        plugin._live_start(
                            sid, mode="compress" if _COMPRESSING.get() else "reply"
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"[ncd-monitor] 开实时缓冲失败: {exc}")

                    started = time.time()
                    st: dict[str, Any] = {
                        "chunks": 0,
                        "ok": True,
                        "closed": False,
                        "final": None,
                        "usage": None,
                        "tools": [],
                    }

                    def _finish() -> None:
                        """收尾：结束实时缓冲 + 把响应挂到快照上 + 打一行诊断。

                        ★★ 必须放在 finally 里，不能只写在 async for 之后。
                        消费者可以【提前 return】或调 aclose()，那时会在 yield 处
                        抛 GeneratorExit —— 循环后面的代码【一行都不会执行】，
                        表现就是"模型明明回了、QQ 也收到了，面板上这条却没有回复"。
                        （tool_loop_agent_runner.py:523 就有 `if resp is None: return`，
                          它的 finally 还会 `_close_executor(stream)`。）
                        """
                        try:
                            plugin._live_end(st["ok"])
                        except Exception as exc:  # noqa: BLE001
                            logger.error(f"[ncd-monitor] 结束实时缓冲失败: {exc}")
                        try:
                            final = st["final"]
                            if final is None:
                                final = _StreamSummary(
                                    plugin._live_text(), st["usage"], st["tools"]
                                )
                            # ★ 把【我们逐块攒的】思考过程一起交上去。
                            #   为什么不直接改 final.reasoning_content：那是 AstrBot
                            #   真正在用的响应对象，而且 openai_source.py:479-480 明说
                            #   "有 reasoning_content 但没有 content 和 tool_calls 不能丢"，
                            #   它会被写进会话历史 —— 监控往那儿写一个字都是污染。
                            plugin._attach_snapshot_response(
                                sid, final, reasoning=plugin._live_reasoning() or None
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.error(f"[ncd-monitor] 关联流式响应失败: {exc}")
                        # 诊断：一行看清"这次流式收了多少、有没有拿到完整结果"。
                        # （加这行是因为：先前我用"日志里没有 Applying streaming output"
                        #   判断流式没生效 —— 而那条日志在 turn_off 下【永远不会出现】，
                        #   是个坏判据。这里给一个真正可靠的信号。）
                        logger.info(
                            "[ncd-monitor] 流式结束：%d 个 chunk / %d 字 / %.1f 秒"
                            " · 完整结果=%s%s",
                            st["chunks"],
                            len(plugin._live_text()),
                            time.time() - started,
                            "有" if st["final"] is not None else "无（用累积文本兜底）",
                            (
                                "· 被提前关闭（已拿到完整结果，属正常收尾）"
                                if st["closed"] and st["final"] is not None
                                else (
                                    "· 被提前关闭且没拿到完整结果"
                                    if st["closed"]
                                    else ""
                                )
                            ),
                        )

                    try:
                        async for chunk in original(self_, *args, **kwargs):
                            st["chunks"] += 1
                            try:
                                # ★ 思考过程要【每一块都收】。
                                #   openai_source.py:663 每轮把 reasoning_content 清空
                                #   再填这一轮的增量，所以事后从最后那个对象上读，
                                #   只能拿到最后一轮的残片（跟 completion_text 同一个坑）。
                                #   只累加、不碰原对象 —— 监控绝不能改被监控的响应。
                                reasoning = getattr(chunk, "reasoning_content", None)
                                if isinstance(reasoning, str) and reasoning:
                                    plugin._live_append_reasoning(reasoning)
                                if _is_delta_chunk(chunk):
                                    plugin._live_append(_chunk_text(chunk))
                                elif _has_final_text(chunk):
                                    # 最后那个【完整结果】——不参与累积，
                                    # 否则会把已经攒过的文本再加一遍。
                                    # ★ 只认【真的带着文本】的非 delta chunk：
                                    #   中途冒出来的非 delta（别的 provider 的
                                    #   reasoning / 工具调用段）若覆盖了 final，
                                    #   因为兜底只在 final is None 时触发，面板会
                                    #   【安静地显示错内容】—— 最难发现的那种。
                                    st["final"] = chunk
                                usage = getattr(chunk, "usage", None)
                                if usage is not None:
                                    st["usage"] = usage
                                tools = getattr(chunk, "tools_call_name", None)
                                if tools:
                                    st["tools"] = list(tools)
                            except Exception as exc:  # noqa: BLE001
                                logger.error(f"[ncd-monitor] 处理流式 chunk 失败: {exc}")
                            yield chunk  # ★ 原样透传
                    except GeneratorExit:
                        # 消费者提前关闭了流（提前 return / aclose）。
                        # ★ 绝不能吞掉它，也绝不能再 yield。
                        st["closed"] = True
                        # ★★ 关键：已经拿到【完整结果】之后再被关闭，属于【正常收尾】。
                        #   实测每次都这样：wrapper 把最后那个完整 chunk yield 出去、
                        #   消费者拿走就收工关流，于是 GeneratorExit 落在那个 yield 上。
                        #   如果一律算失败，面板会每次都显示"提前结束" —— 狼来了，
                        #   真出问题时反而没人看。所以只有"没拿到完整结果就被关"才算异常。
                        st["ok"] = st["final"] is not None
                        raise
                    except Exception:
                        plugin._mark_snapshot_error(sid)
                        st["ok"] = False
                        raise
                    finally:
                        _finish()

                patched_stream.__name__ = getattr(
                    original, "__name__", "text_chat_stream"
                )
                cls.text_chat_stream = patched_stream
                cls._ncd_stream_patched = True  # type: ignore[attr-defined]
                _WRAPS.append(
                    (cls, "text_chat_stream", original, "_ncd_stream_patched")
                )
                self._patched_stream_names.append(cls.__name__)
                logger.info(
                    f"[ncd-monitor] 新包装 Provider 子类 stream: {cls.__name__}"
                )

            def walk(cls) -> None:
                wrap(cls)
                wrap_stream(cls)
                for sub in cls.__subclasses__():
                    walk(sub)

            walk(Provider)
            return len(self._patched_provider_names)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] 遍历 Provider 子类失败: {exc}")
            return len(getattr(self, "_patched_provider_names", []))

    @filter.after_message_sent()
    async def on_message_sent(self, event: AstrMessageEvent) -> None:
        """回复真正发出去了 —— 在实时输出块上记一笔「已发出」。

        ★ 这里【不清空】。用户要的是「常驻」：生成完、发出去之后那一块仍然留着，
          方便回头看模型刚才输出了什么；下次开始生成才覆盖。

        ★ 要带上 umo：实时输出按会话分路，不带 umo 会标到"当前那一路"上，
          而"当前"在多会话并发时是不可靠的。
        """
        self._live_mark_sent(getattr(event, "unified_msg_origin", "") or "")
        # ★ 释放点之二：回复真的发出去了。
        #   加这一路是因为【模型报错时 on_llm_response 不触发】—— 槽位会一直占着，
        #   上限是 1 时等于把机器人哑掉，实测哑了 5 分钟（等超时兜底回收）。
        #   模型失败时 AstrBot 会给用户发一条错误消息，这个钩子照样触发。
        await self._release_gate(getattr(event, "unified_msg_origin", "") or "")

    # 压缩请求的识别特征（compressor.py 里写死的那句）
    COMPRESS_MARKER = "Generate a summary of our previous conversation history."

    # 宿主机资源采样节奏：1 秒一次，保留 180 个 = 3 分钟趋势
    SYS_SAMPLE_INTERVAL = 1.0
    SYS_SAMPLE_KEEP = 180

    # AstrBot 往上下文里塞的图片占位文本，形如：
    #   [图片]
    #   [Image Attachment: path F:\...\media_image_20261011103041287_38a1.jpg]
    # 第二个含完整路径 —— 那既是噪音，也是【定位原图的线索】，所以留捕获组
    _IMG_ATTACH_RE = re.compile(r"\[Image Attachment:\s*path\s*([^\]]*)\]")
    # ★ 引用消息里的图【是另一套写法】，上面那个正则匹配不到：
    #     [Image Attachment in quoted message: path F:\...\media_image_xxx.jpg]
    #   （astr_main_agent.py:746）
    #   注意 "Image Attachment" 后面是 " in quoted message:" 而不是 ":"，
    #   所以两个正则不会互相误伤。
    #
    #   ★ 这段标记文本【只在图片真的进了 req.image_urls 之后】才会被追加
    #     （astr_main_agent.py:1446-1458 直连路径 / :1509-1514 兜底路径），
    #     失败时写的是 "[Image unavailable]"（:1443）。
    #     而 openai_source.assemble_context:1404-1408 会把 req.image_urls
    #     逐个变成 {"type":"image_url"} 内容块。
    #     => 看到这段标记 == 模型确实收到了这张图。
    #     所以这里把它当图片处理（给缩略图），而不是留在正文里当噪音。
    _QUOTED_IMG_RE = re.compile(
        r"\[Image Attachment in quoted message:\s*path\s*([^\]]*)\]"
    )
    # 系统提醒块，形如 <system_reminder>User ID: ..., Nickname: ...</system_reminder>
    _SYS_REMINDER_RE = re.compile(r"<system_reminder>(.*?)</system_reminder>", re.S)

    @classmethod
    def _extract_images(cls, raw: str) -> tuple[str, list[dict[str, str]]]:
        """从一段文本里挖出图片标记、把它们摘掉，返回 (清洗后文本, 图片列表)。

        两种标记（互不误伤）：
            [Image Attachment: path <p>]                     ← 直接发的图
            [Image Attachment in quoted message: path <p>]   ← 引用的图

        ★★ 为什么必须做成共用 helper（踩出来的）：
          图片标记【两个地方】都有：
            · 消息 content 里（历史轮次带着的）
            · extra_user_content_parts 里（本轮的）
          而 astr_main_agent.py 里产生标记的两处，写的都是 extra parts：
            :1402-1404  直接发的图 → extra_user_content_parts
            :744-747    引用的图   → extra_user_content_parts
          （:1388 / :1446 / :1512 才是把路径塞进 req.image_urls 的地方。）

          先前只在 content 那条路上找标记，于是【本轮新发的图、以及引用的图】
          在面板上既没有缩略图、也不计数 —— 用户看到的就是"引用图没送到模型"。
          其实模型收到了（图在 req.image_urls 里），是【面板没显示】。
          两条路都过一遍这个 helper，就不再依赖"标记到底躺在哪儿"这个版本细节。
        """
        if not raw:
            return "", []
        images: list[dict[str, str]] = []
        for path, quoted in [
            (p, False) for p in cls._IMG_ATTACH_RE.findall(raw)
        ] + [(p, True) for p in cls._QUOTED_IMG_RE.findall(raw)]:
            p = path.strip()
            if not p:
                continue
            k = hashlib.sha1(p.encode("utf-8", "ignore")).hexdigest()[:16]
            if any(im["k"] == k for im in images):
                continue
            images.append(
                {
                    "k": k,
                    "n": os.path.basename(p),
                    "p": p,  # 后端内部用来找原图；发给前端时会被去掉
                    "q": quoted,
                }
            )
        text = cls._IMG_ATTACH_RE.sub("", raw)
        text = cls._QUOTED_IMG_RE.sub("", text)
        return text, images

    @classmethod
    def _split_content(cls, raw: str) -> tuple[str, str, list[dict[str, str]]]:
        """把一条消息拆成三个独立部分：正文 / 系统提醒 / 图片列表。

        为什么要拆：实测一条带图消息在上下文里长这样 ——
            [0] text        "[图片]"
            [1] text        "[Image Attachment: path F:\\...\\media_image_xxx.jpg]"
            [2] text        "<system_reminder>User ID: 100000001, ...</system_reminder>"
            [3] image_url   "data:image/jpeg;base64,..."

        全塞进正文的话，气泡里就是 185 字符的一坨路径 + 提醒文字，
        又难看又占地方。拆开之后：
            正文   -> 正常气泡文字
            提醒   -> 小字一行，灰色
            图片   -> 缩略图（靠路径去找原图）

        Returns:
            (正文, 系统提醒,
             [{"k": 缩略图缓存键, "n": 文件名, "q": 是否来自引用消息}, ...])
        """
        if not raw:
            return "", "", []

        # 图片标记先摘掉（直接发的 + 引用消息里的，都算模型收到的图）。
        # quoted 标记留给前端区分显示（引用图加个边框），同一路径只登记一次。
        text, images = cls._extract_images(raw)

        reminders = [r.strip() for r in cls._SYS_REMINDER_RE.findall(raw) if r.strip()]

        text = cls._SYS_REMINDER_RE.sub("", text)
        text = text.replace("[图片]", "").replace("🖼", "")
        # 压掉连续空行、行尾空格、首尾空白
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        text = text.strip()
        # 正文全是空白行的话就当没有
        if not text.strip("\n \t"):
            text = ""

        return text, "\n".join(reminders), images

    @classmethod
    def _tidy_text(cls, text: str) -> str:
        """就地清洗纯文本（保留图片占位、去掉提醒块）。

        用在 system_prompt 和 prompt 上 —— 它们没有图片/提醒结构，
        只是要压掉开头那几行空白（实测 system_prompt 以 '\\n' 开头，
        配上 pre-wrap 会让截断区域一片空白）。
        """
        if not text:
            return ""
        text = cls._SYS_REMINDER_RE.sub(
            lambda m: "\n📋 " + m.group(1).strip() + "\n", text
        )
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        return text.strip()

    @classmethod
    def _split_extra_parts(
        cls, parts: Any
    ) -> tuple[str, list[dict[str, str]], int]:
        """把 extra_user_content_parts 拆成 (文本, 图片数)。

        ★ 这是 AstrBot 的【自动注入】通道，不经过 contexts/system_prompt/prompt，
          所以本插件以前完全看不见它 —— 面板上看着"AI 只收到这么点东西"，
          实际上模型还多收了一段 AstrBot 自己塞进去的内容。证据链：
            · builtin_stars/astrbot/group_chat_context.py:194  on_req_llm()
              -> req.extra_user_content_parts.append(TextPart(群聊上下文块))
              开关：provider_ltm_settings.group_icl_enable（内存 deque，不落库）
            · builtin_stars/astrbot/main.py:347-356  @filter.on_llm_request()
              挂在 LLM 请求前，所以它进得了 ProviderRequest
            · tool_loop_agent_runner.py:508  payload 里单独一个键
            · provider/sources/openai_source.py:1386-1401  assemble_context()
              -> 这些块被【并进最后一条 user 消息】的 content_blocks
        """
        if not parts:
            return "", [], 0
        texts: list[str] = []
        images: list[dict[str, str]] = []
        blob_count = 0
        for p in parts:
            text = getattr(p, "text", None)
            if not isinstance(text, str) and isinstance(p, dict):
                text = p.get("text")
            if isinstance(text, str) and text.strip():
                # ★★ 本轮新发的图、以及引用的图，标记就在这一段里
                #   （astr_main_agent.py:1402-1404 直接图 / :744-747 引用图，
                #    两处写的都是 extra_user_content_parts）。
                #   不在这里挖，面板上就既没缩略图也不计数。
                cleaned, found = cls._extract_images(text)
                for im in found:
                    if not any(x["k"] == im["k"] for x in images):
                        images.append(im)
                cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
                cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
                if cleaned.strip():
                    texts.append(cleaned.strip())
                continue
            # 真正的图片块（走 base64/URL 而不是本地路径）：pydantic 对象靠类名认，
            # dict 形式靠 "type" 认（entities.py:215-217 两种形态都接受）。
            # 它没有本地路径，前端拿不到缩略图，所以只计数、不进 images。
            kind = type(p).__name__
            ptype = getattr(p, "type", None)
            if isinstance(p, dict):
                ptype = p.get("type") or ptype
            if kind == "ImageURLPart" or ptype == "image_url":
                blob_count += 1
        return "\n".join(texts), images, blob_count

    def _pending_for_current_session(self) -> dict[str, Any]:
        """取【当前会话】那条待配对记录（Provider 包装里用）。

        ★ 为什么不能用"全局唯一那条"：两个会话会交错 ——
          群里一条、私聊一条几乎同时发，单槽会被后一条覆盖，
          先发那条的 group_name / is_active_reply 就丢了。实测表现是
          群里的主动回复在面板上【没有「主动」标记、来源只剩群号】。

        拿不到 umo 时（例如包装恰好跑在另一个任务里）退回"只有一条就用它"。
        """
        umo = _CURRENT_UMO.get("")
        if umo and umo in self._pending:
            return self._pending[umo]
        if len(self._pending) == 1:
            return next(iter(self._pending.values()))
        return {}

    @staticmethod
    def _cache_cause(
        flags: dict[str, Any],
        first_diff: int | None,
        prev_len: int,
        hit_pct: float | None,
    ) -> str:
        """把前缀 flag 翻成面板上的一句人话；只做归因，不参与请求。"""
        changed = []
        if flags.get("system"):
            changed.append("系统字段")
        if flags.get("head"):
            changed.append("首条")
        if flags.get("tools"):
            changed.append("工具集")
        if changed:
            cause = "+".join(changed) + "改写"
        elif first_diff == 0:
            cause = "完全一致"
        elif first_diff == prev_len + 1:
            cause = "尾部追加"
        elif first_diff:
            cause = f"第 {first_diff} 条历史改写"
        else:
            cause = "—"
        # 前缀没变却低命中，多半是服务端缓存被挤掉，不是本地改坏了。
        if hit_pct is not None and hit_pct < 80 and cause in ("完全一致", "尾部追加"):
            cause += " · 服务端未命中"
        return cause

    def _prefix_diag(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """对【真正发出去】的前缀做指纹（只读，不改请求）。

        四段：
            system  —— 系统提示词
            tools   —— 工具 schema（openai_schema 序列化）
            head    —— 历史第一条消息
            history —— 整段历史

        ★ 缓存是前缀匹配：history 变不一定断，head 变一定从那里断。
          所以面板上「变」的段，就是嫌疑最大的那一段。
        """
        system_prompt = str(kwargs.get("system_prompt") or "")
        contexts = kwargs.get("contexts") or []
        func_tool = kwargs.get("func_tool")
        schema: list[dict[str, Any]] = []
        try:
            if func_tool is not None and getattr(func_tool, "tools", None):
                schema = func_tool.openai_schema()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ncd-monitor] 工具 schema 指纹失败: {exc}")
        try:
            tools_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        except Exception:  # noqa: BLE001
            tools_text = ""
        try:
            history_text = json.dumps(contexts, ensure_ascii=False, sort_keys=True)
        except Exception:  # noqa: BLE001
            history_text = str(contexts)

        head_text = ""
        head_preview = ""
        head_role = ""
        head_chars = 0
        if contexts:
            try:
                head_text = json.dumps(contexts[0], ensure_ascii=False, sort_keys=True)
            except Exception:  # noqa: BLE001
                head_text = str(contexts[0])
            try:
                head_preview = self._content_text(
                    contexts[0].get("content")
                    if isinstance(contexts[0], dict)
                    else getattr(contexts[0], "content", "")
                )
            except Exception:  # noqa: BLE001
                head_preview = ""
            try:
                head_role = str(
                    contexts[0].get("role")
                    if isinstance(contexts[0], dict)
                    else getattr(contexts[0], "role", "")
                )
            except Exception:  # noqa: BLE001
                head_role = ""
            head_chars = len(head_preview)

        def _h(text: str) -> str:
            return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]

        # 逐条消息指纹：用来算「第一处不同」在哪。
        # 90 条 × 10 字符，一条请求不到 1KB；面板只留最近 60 条，够用。
        msg_hashes: list[str] = []
        for m in contexts[:200]:
            try:
                mt = json.dumps(m, ensure_ascii=False, sort_keys=True)
            except Exception:  # noqa: BLE001
                mt = str(m)
            msg_hashes.append(_h(mt))

        return {
            "system": _h(system_prompt),
            "tools": _h(tools_text),
            "head": _h(head_text),
            "history": _h(history_text),
            "msg_hashes": msg_hashes,
            "system_chars": len(system_prompt),
            "tools_count": len(schema),
            "ctx_count": len(contexts),
            "head_role": head_role,
            "head_chars": head_chars,
            "head_preview": " ".join(head_preview.split())[:60],
            "time": time.time(),
        }

    def _capture_text_chat(
        self, kwargs: dict[str, Any], args: tuple[Any, ...] = ()
    ) -> int | None:
        contexts = kwargs.get("contexts")
        system_prompt = kwargs.get("system_prompt") or ""
        prompt = kwargs.get("prompt") or ""

        # ★ 先认「这是不是本插件之外的内部调用」。
        #   toolbox 抓完正文会自己调 provider.text_chat 做总结 —— 那是另一条管道上的
        #   请求，不属于任何会话。不排除掉的话：那 4000 字网页正文会被记成一次正常
        #   请求，面板右栏凭空多一坨，而且「快压缩了吗」的判断会跟着虚高。
        #   它靠一个约定标记自报身份（两侧都写了 _INTERNAL_REQUEST_MARKERS）。
        if _is_internal_request(contexts, prompt):
            return None

        # 前缀指纹：必须在【真正调用 provider 之前】量，才是实际发出去的版本。
        try:
            umo_now = _CURRENT_UMO.get() or getattr(self, "_last_umo", "")
            if umo_now:
                self._prefix_by_umo[umo_now] = self._prefix_diag(kwargs)
                if len(self._prefix_by_umo) > 100:
                    self._prefix_by_umo.clear()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ncd-monitor] 前缀指纹失败: {exc}")

        inject_text, inject_images, inject_blobs = self._split_extra_parts(
            kwargs.get("extra_user_content_parts")
        )

        if contexts is None and len(args) >= 6:
            # 极端情况：按位置传参（text_chat(self, prompt, session_id, image_urls,
            # audio_urls, func_tool, contexts, ...)）
            contexts = args[5]
        # ★ 只有注入块、没有正文的情况也必须记下来
        #   （纯 @ + 空文本时 AstrBot 会把 prompt 兜底成 "<attachment>"，
        #    但那一刻若 contexts 也为空，早期版本会在这里 return None 丢掉整条请求）
        if (
            not contexts
            and not system_prompt
            and not prompt
            and not inject_text
            and not inject_images
        ):
            return None

        messages: list[dict[str, Any]] = []
        image_count = 0
        n_direct_all = 0
        n_quoted_all = 0
        if system_prompt:
            messages.append(
                {
                    "role": "system",
                    "content": self._tidy_text(str(system_prompt)),
                    "reminder": "",
                    "images": [],
                }
            )
        for m in contexts or []:
            role = getattr(m, "role", None)
            content = getattr(m, "content", None)
            if role is None and isinstance(m, dict):
                role = m.get("role")
                content = m.get("content")
            raw = self._content_text(content)
            text, reminder, images = self._split_content(raw)
            imgs = self._count_images(content)
            # ★ 图片有两种存在形式，别重复算也别漏算：
            #   · 直接发的图：[Image Attachment: path ...] 标记 + 一个 ImageURLPart
            #     -> 标记和 part 指的是同一张，取 max
            #   · 引用图：只有标记，图片本身在 req.image_urls 里（不在 content 里）
            #     -> _count_images 数不到，得靠标记补上
            n_direct = sum(1 for it in images if not it.get("q"))
            n_quoted = sum(1 for it in images if it.get("q"))
            n_direct_all += n_direct
            n_quoted_all += n_quoted
            image_count += max(imgs, n_direct) + n_quoted
            # 图片本身没有文字，上面的占位清洗后可能啥都不剩，补个标记
            if not text and not images and content is not None:
                text = self._placeholder_for(content)
            # 登记缩略图：hash -> 原图路径，供 /thumb 使用
            for it in images:
                self._thumb_paths[it["k"]] = it.pop("p", "")
            messages.append(
                {
                    "role": str(role or "?"),
                    "content": text,
                    "reminder": reminder,
                    "images": images,
                }
            )

        if prompt:
            messages.append(
                {
                    "role": "user",
                    "content": str(prompt),
                    "reminder": "",
                    "images": [],
                }
            )

        # ★ 自动注入块：物理上被并进【最后一条 user 消息】，所以挂到最后一条上显示。
        #   注意它和 m["reminder"] 是两回事：reminder 是 content 里内嵌的
        #   Identifier 提醒（astr_main_agent.py 写入正文），这里是独立的额外内容块。
        #   ★ 本轮新发的图 / 引用的图，标记也在这一段里 —— 挖出来的缩略图
        #     直接并进最后一条消息，前端就能显示（引用图带橙框）。
        if inject_text or inject_images or inject_blobs:
            if not messages:
                messages.append(
                    {
                        "role": "user",
                        "content": "",
                        "reminder": "",
                        "images": [],
                    }
                )
            last = messages[-1]
            last["inject"] = inject_text
            if inject_images:
                existing = last.setdefault("images", [])
                have = {im.get("k") for im in existing}
                for im in inject_images:
                    if im["k"] not in have:
                        existing.append(im)
                        have.add(im["k"])
            for im in inject_images:
                self._thumb_paths[im["k"]] = im.pop("p", "")
            image_count += inject_blobs + len(inject_images)
            # ★ 诊断：让"图到底从哪来"以后一眼可见（我自己在这上面绕过弯路）
            logger.info(
                "[ncd-monitor] 上下文 #%d 图片拆分：content 直发 %d / 引用 %d ·"
                " 注入块 直发 %d / 引用 %d / 无路径图块 %d · 合计 %d",
                self._snapshot_seq,
                n_direct_all,
                n_quoted_all,
                sum(1 for im in inject_images if not im.get("q")),
                sum(1 for im in inject_images if im.get("q")),
                inject_blobs,
                image_count,
            )

        joined = "\n".join(
            m["content"] + (m.get("inject") or "") for m in messages
        )
        kind = "compress" if self.COMPRESS_MARKER in joined else "chat"

        total_chars = sum(
            len(m["content"]) + len(m.get("inject") or "") for m in messages
        )
        active_ctx = self._pending_for_current_session()
        snapshot = {
            "id": self._snapshot_seq,
            "time": time.time(),
            "kind": kind,
            "messages": messages,
            "message_count": len(messages),
            "image_count": image_count,
            "total_chars": total_chars,
            "estimated_tokens": self._estimate_messages_tokens(
                [
                    {
                        "role": m["role"],
                        "content": m["content"] + (m.get("inject") or ""),
                    }
                    for m in messages
                ]
            ),
            "inject_chars": sum(len(m.get("inject") or "") for m in messages),
            "tools": self._tool_names(kwargs.get("func_tool")),
            "umo": active_ctx.get("umo", ""),
            "group_id": active_ctx.get("group_id", ""),
            "group_name": active_ctx.get("group_name", ""),
            "sender_name": active_ctx.get("sender_name", ""),
            "sender_id": active_ctx.get("sender_id", ""),
            "has_summary": any(
                m["content"].startswith(SUMMARY_PREFIX) for m in messages
            ),
            "response": None,
            "error": None,
        }
        self._snapshot_seq += 1
        self.context_snapshots.append(snapshot)
        self._save_snapshots()

        # 诊断：图片抓没抓到、缩略图索引登记了几个。
        # （这里之所以要打日志，是因为 TextPart 是 pydantic 对象而不是 dict，
        #   isinstance(p, dict) 曾经把整条多模态消息的文本都丢掉，
        #   表现就是页面上完全没有缩略图、却看不出哪里报错。）
        logger.info(
            f"[ncd-monitor] 抓到上下文 #{snapshot['id']} kind={kind} "
            f"消息 {len(messages)} 条 · 图片 {image_count} 张 · "
            f"缩略图索引 {len(self._thumb_paths)} 个"
        )
        return snapshot["id"]

    @staticmethod
    def _count_images(content: Any) -> int:
        """数一条消息里有多少张图。

        图片会【持久留在对话历史里】—— 实测每次请求都会重新带上历史上
        所有发过的图（最多观察到 26 条带图消息）。所以这个数字很值得看：
        它不体现在 prompt_tokens 上（视觉编码单独算），但会拖慢每次推理。

        ★ 同样要 dict 和对象两种都认（ImageURLPart 是 pydantic 对象）。
        """
        if not isinstance(content, list):
            return 0
        n = 0
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "image_url":
                    n += 1
            elif type(p).__name__ == "ImageURLPart" or hasattr(p, "image_url"):
                n += 1
        return n

    @staticmethod
    def _placeholder_for(content: Any) -> str:
        try:
            if isinstance(content, list):
                kinds = []
                for p in content:
                    if isinstance(p, dict):
                        kinds.append(str(p.get("type") or "?"))
                return f"[{'/'.join(kinds) or '非文本内容'}]"
        except Exception:  # noqa: BLE001
            pass
        return "[非文本内容]"

    @staticmethod
    def _tool_names(func_tool: Any) -> list[str]:
        try:
            if func_tool is None:
                return []
            names = []
            for t in func_tool:
                n = getattr(t, "name", None)
                if n:
                    names.append(str(n))
            return names
        except Exception:  # noqa: BLE001
            return []

    def _find_snapshot(self, snapshot_id: int | None) -> dict[str, Any] | None:
        if snapshot_id is None:
            return None
        for s in reversed(self.context_snapshots):
            if s["id"] == snapshot_id:
                return s
        return None

    def _mark_snapshot_error(self, snapshot_id: int | None) -> None:
        s = self._find_snapshot(snapshot_id)
        if s is not None:
            s["error"] = "请求抛异常（见 AstrBot 日志）"

    # =================================================================
    #  实时输出缓冲（右栏底部「模型正在输出」那块）
    # =================================================================

    # 最多在内存里保留几路实时输出。防的是"会话很多时无限堆积"。
    LIVE_KEEP = 4

    def _live_key(self) -> str:
        """当前这一路实时输出属于哪个会话。

        ★ 这里以前是"全局唯一一块"，于是两个会话同时生成时：
             · B 一开始生成就把 A 那块【重置】掉；
             · 之后 A、B 的增量都往同一块里加 —— 文字【逐字交替混在一起】。
          现在按 umo 分路，各攒各的，前端点「并发」按钮切换着看。

        ★ 已知限制（有意不改，理由记在这里免得后人重复踩）：
          压缩流（mode="compress"）此时拿到的 _CURRENT_UMO 是【当轮】的值，
          但压缩天然发生在会话信息之外，若两个会话【同时】压缩，它们会共用
          同一块实时缓冲、面板上文字混着显示。
          为什么不在 key 里给压缩流单独编一个身份：试过，会连带弄坏另一条路径 ——
          `_live_clear()` 是按"当前 key"删的，一旦压缩块的 key 与普通路径不同，
          非压缩时的那次清理就删不掉它（`测试-压缩流式.py` 用例 3 会红）。
          而这点混流的影响面【只在面板显示】：压缩的**结果**来自
          `_run_compress_stream` 自己捞的 final，不经过实时缓冲。
          代价与收益不成比例，所以保持现状 —— 若把 max_concurrent_replies
          设成 1，压缩就不会重叠；默认 0 时接受两个压缩可能同屏。
        """
        try:
            return _CURRENT_UMO.get("") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _live_prune(self) -> None:
        """把最老的【已结束】那几路丢掉，在跑的绝不丢。"""
        try:
            if len(self._lives) <= self.LIVE_KEEP:
                return
            done = [
                (k, v)
                for k, v in self._lives.items()
                if v.get("finished") is not None
            ]
            done.sort(key=lambda kv: kv[1].get("started", 0))
            for key, _ in done[: max(0, len(self._lives) - self.LIVE_KEEP)]:
                self._lives.pop(key, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 清理实时输出失败（忽略）: {exc}")

    def _live_start(self, snapshot_id: int | None, mode: str = "reply") -> None:
        """一次流式生成开始 —— 覆盖【本会话】上一次的内容，重新开一块。

        mode:
            "reply"    — 正常回复的生成（默认）
            "compress" — 上下文压缩的生成。前端据此给这块套黄框、标「正在压缩中」。

        ★ mode="compress" 时【不挂会话信息】：压缩发生在主回复之前，
          self._pending 里还是【上一轮】的会话，挂上去会张冠李戴。
        """
        compress = mode == "compress"
        ctx = {} if compress else self._pending_for_current_session()
        key = self._live_key()
        # ★ 用自增序号排序，不要用 time.time()：
        #   两次 _live_start 可能落在同一个时间戳上，那时 max() 平局取第一个，
        #   默认显示就会挑到【旧】的那一路（实测就是这么翻车的）。
        self._live_seq += 1
        self._lives[key] = {
            "text": "",
            # ★ 思考过程单独攒一路。它是【逐块增量】来的，不能等最后那个完整结果 ——
            #   openai_source.py:663 每轮把 llm_response.reasoning_content 清空重填，
            #   所以最后拿到的只是【最后一轮的残片】。跟 completion_text 是同一个坑。
            "reasoning": "",
            "started": time.time(),
            "seq": self._live_seq,
            "finished": None,
            "sent": None,
            "ok": True,
            "mode": mode,
            "umo": ctx.get("umo", "") or ("" if compress else key),
            "group_name": ctx.get("group_name", ""),
            "sender_name": ctx.get("sender_name", ""),
            "snapshot_id": snapshot_id,
        }
        self._live_prune()

    def _live_current(self) -> dict[str, Any]:
        """当前会话那一路的原始缓冲（没有就给个空 dict，调用方不用判空）。"""
        return self._lives.get(self._live_key()) or {}

    def _live_append(self, delta: str) -> None:
        live = self._live_current()
        if not delta or not live or live.get("finished"):
            return
        live["text"] += delta

    def _live_append_reasoning(self, delta: str) -> None:
        """攒思考过程的增量（和正文分开存，面板上分开展示）。"""
        live = self._live_current()
        if not delta or not live or live.get("finished"):
            return
        live["reasoning"] = (live.get("reasoning") or "") + delta

    def _live_reasoning(self) -> str:
        """读本会话当前攒下的思考过程。"""
        return self._live_current().get("reasoning", "") or ""

    def _live_end(self, ok: bool) -> None:
        """一次流式生成结束（正常结束、抛异常、或被消费者提前关闭）。"""
        live = self._live_current()
        if not live:
            return
        live["finished"] = time.time()
        if not ok:
            live["ok"] = False

    def _live_text(self) -> str:
        """读【本会话】当前缓冲的文本（不触发任何清理）。"""
        return self._live_current().get("text", "") or ""

    def _live_mark_sent(self, umo: str = "") -> None:
        """回复真正发出去了 —— 记一笔时间，面板上就能显示「已发出 HH:MM:SS」。

        ★ 注意：这里【不再清空】。用户明确要「常驻」——生成完、发出去之后
          这一路仍然留着，方便回头看模型刚才输出了什么；下次生成才覆盖。
        """
        live = self._lives.get(umo or self._live_key())
        if live is not None:
            live["sent"] = time.time()

    def _live_clear(self) -> None:
        self._lives.pop(self._live_key(), None)

    def _live_view(self, live: dict[str, Any], key: str = "") -> dict[str, Any]:
        """把一路实时输出转成给前端的形状。"""
        text = live.get("text", "") or ""
        finished = live.get("finished")
        sent = live.get("sent")
        return {
            "key": key,
            "text": text,
            "chars": len(text),
            # ★ 思考过程：与正文分开给前端，面板上单独一块显示。
            #   注意它【不进聊天】—— AstrBot 的 display_reasoning_text 管的是聊天，
            #   这里是监控侧的观察，两回事。
            "reasoning": live.get("reasoning", "") or "",
            "reasoning_chars": len(live.get("reasoning", "") or ""),
            "done": finished is not None,
            "ok": live.get("ok", True),
            # "reply" | "compress" —— 前端靠它决定要不要套黄框标「正在压缩中」
            "mode": live.get("mode", "reply"),
            "elapsed": round(
                (finished or time.time()) - live.get("started", 0), 1
            ),
            "sent": (
                time.strftime("%H:%M:%S", time.localtime(sent))
                if sent
                else None
            ),
            "umo": live.get("umo", ""),
            "group_name": live.get("group_name", ""),
            "sender_name": live.get("sender_name", ""),
            "label": self._gate_label(live.get("umo", "") or key),
        }

    def _live_payload(self) -> dict[str, Any] | None:
        """默认要看的那一路（最后开始的那一路）；一路都没有就返回 None。

        ★ 常驻语义不变：生成结束后【不自动消失】，留到下一次 _live_start 覆盖。
          要收起由前端那个「收起」按钮负责（纯前端状态）。
        """
        if not self._lives:
            return None
        key = max(self._lives, key=lambda k: self._lives[k].get("seq", 0))
        return self._live_view(self._lives[key], key)

    def _lives_payload(self) -> list[dict[str, Any]]:
        """全部实时输出，新的在前。前端靠它做「并发」切换。"""
        items = sorted(
            self._lives.items(), key=lambda kv: kv[1].get("seq", 0), reverse=True
        )
        return [self._live_view(live, key) for key, live in items]

    def _attach_snapshot_response(
        self, snapshot_id: int | None, resp: Any, reasoning: str | None = None
    ) -> None:
        """把这次响应挂到快照上。

        reasoning：流式那条路【逐块攒下来】的思考过程。传了就用它，
        因为流式下 resp.reasoning_content 只剩最后一轮的残片（见 _live_start 的说明）；
        非流式那条路不传，直接读 resp 上的完整内容。
        """
        s = self._find_snapshot(snapshot_id)
        if s is None or resp is None:
            return
        usage = getattr(resp, "usage", None)
        s["response"] = {
            "text": getattr(resp, "completion_text", "") or "",
            "reasoning": reasoning
            if reasoning is not None
            else getattr(resp, "reasoning_content", None),
            "tool_calls": list(getattr(resp, "tools_call_name", None) or []),
            "input_tokens": getattr(usage, "input", None),
            "output_tokens": getattr(usage, "output", None),
            "cached_tokens": getattr(usage, "input_cached", None),
        }
        # 压缩请求：把输出当作摘要记下来（和压缩器那一路互为补充）
        if s["kind"] == "compress":
            text = (s["response"]["text"] or "").strip()
            if text:
                s["summary"] = text


    # =================================================================
    #  一、捕获上下文压缩摘要
    # =================================================================

    async def _run_compress_stream(
        self, provider: Any, args: tuple, kwargs: dict
    ) -> Any:
        """把压缩器那次 text_chat 换成流式，返回最后一次 yield 的完整结果。

        ★ 这里【不】自己开实时缓冲 —— text_chat_stream 的包装会开，
          并且因为此刻 _COMPRESSING=True，它会开成 mode="compress"。
          本方法只负责把流消费完、把完整结果捞出来交回给压缩器。

        压缩器只用 completion_text（compressor.py:279），所以等价的完整
        LLMResponse 就够了，不需要工具调用之类的字段。
        """
        final = None
        chunks = 0
        started = time.time()
        async for chunk in provider.text_chat_stream(*args, **kwargs):
            chunks += 1
            # ★ 与面板同一套判据：只认【真的带着文本】的非 delta chunk。
            #   这里比面板更要紧 —— 压缩器只取 completion_text（compressor.py:279），
            #   收尾的空壳一旦顶掉真正的摘要，压缩器拿到的就是空文本，
            #   等于把上下文换成了空白摘要。
            if not _is_delta_chunk(chunk) and _has_final_text(chunk):
                final = chunk
        if final is None:
            # 没有带文本的完整结果 → 退回逐字累积的文本；两条都空才算真失败。
            text = self._live_text()
            if not text:
                raise RuntimeError("压缩流式没有返回完整结果")
            final = _StreamSummary(text, None, [])
        logger.info(
            "[ncd-monitor] 压缩流式收完：%d 个 chunk / %d 字 / %.1f 秒",
            chunks,
            len(self._live_text()),
            time.time() - started,
        )
        return final

    def _strip_history_reasoning(self, messages: list[Any]) -> int:
        """按开关剥掉历史里的思考过程。返回摘掉的段数。

        由 _patch_runner() 挂上的包装在【每圈对话开始】时调用 —— 那一刻
        ContextManager.process()（压缩决策）还没跑，run_context.messages
        也还没落盘，所以这一刀同时省下三件事：压缩判断 / 发给模型 / 存进数据库。
        """
        if self._conf_bool("keep_reasoning_in_context", True):
            return 0
        chars = _count_think_chars(messages)
        removed = _strip_think_parts(messages)
        if removed:
            self.reasoning_stripped_segments += removed
            self.reasoning_stripped_chars += chars
            logger.info(
                f"[ncd-monitor] 已从历史剥离 {removed} 段思考（{chars} 字）"
                f" —— 累计 {self.reasoning_stripped_segments} 段 / "
                f"{self.reasoning_stripped_chars} 字"
            )
        return removed

    def _patch_runner(self) -> None:
        """包装 ToolLoopAgentRunner.step()：在压缩决策之前剥掉历史思考。

        ★ 为什么挂 step()（这里改过一次，第一版挂错了地方）：
            生产路径是 internal.py:53 导入的 astr_agent_run_util.run_agent() /
            run_live_agent()，它们在内部【自己 while 循环调 agent_runner.step()】
            （astr_agent_run_util.py:156）。而 `step_until_done()` 只有 SDK /
            第三方那条路才走（core/star/context.py、manager.py、third_party.py）。
            第一版挂在 step_until_done 上 → **生产里一次都没触发**（日志 0 次剥离），
            而单元测试直接调 step_until_done 所以是"假绿"。
            挂 step() 则所有驱动都覆盖到。

        ★ 为什么要"每圈只剥一次"：
            step() 是【一个 LLM 轮次】。每轮都剥的话，模型在一圈内的多轮工具调用
            之间就看不到自己刚才的推理了 —— 而那正是开了思考之后工具链变顺的原因。
            runner 是 build_main_agent() 里每圈新建的（astr_main_agent.py:1645），
            所以在实例上记一个标记就等价于"这一圈已经剥过了"。
        """
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import (
                ToolLoopAgentRunner,
            )

            cls = ToolLoopAgentRunner
            # 用 __dict__ 而不是 getattr：属性会继承，见 _patch_compressor 的说明。
            if cls.__dict__.get("_ncd_monitor_patched", False):
                logger.info("[ncd-monitor] Agent runner 已被包装过，跳过")
                return

            original = cls.__dict__["step"]

            async def patched_step(self_, *args, **kwargs):  # noqa: ANN001, ANN202
                # ★ 同 Provider 包装：实例运行时取，不捕获进闭包（见 _ACTIVE 说明）
                plugin = _ACTIVE
                if plugin is not None and not getattr(
                    self_, "_ncd_reasoning_stripped", False
                ):
                    # ★★ 整段包 try：这是"看/改别人的数据"，
                    #    绝不能让监控的异常挡住 agent 的 step（那会真的挂对话）。
                    try:
                        plugin._strip_history_reasoning(self_.run_context.messages)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"[ncd-monitor] 剥离历史思考失败（不影响本轮）: {exc}")
                    finally:
                        # 剥失败也记上：同一圈不要反复重试同一个动作
                        self_._ncd_reasoning_stripped = True
                async for item in original(self_, *args, **kwargs):
                    yield item

            cls.step = patched_step  # type: ignore[method-assign]
            cls._ncd_monitor_patched = True  # type: ignore[attr-defined]
            _WRAPS.append((cls, "step", original, "_ncd_monitor_patched"))
            logger.info("[ncd-monitor] 已包装 Agent runner，可按开关剥离历史思考")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] 包装 Agent runner 失败: {exc}")

    def _pick_persona_by_session(
        self,
        mgr: Any,
        result: tuple,
        umo: Any,
        conversation_persona_id: str | None,
    ) -> tuple:
        """按会话类型挑人格。挑不了就原样返回 result。

        ★ 只在 AstrBot 自己【没选到具体人格】时才介入：
          · `conversation_persona_id` 非 None（会话被手动指定过，含 "[%None]"
            这种"明确不设人格"）→ 尊重；
          · `forced_id` 非 None（session rule 强制指定）→ 尊重。
          这两条就是需求里的「手动尊重」。
        """
        persona_id, persona, forced_id, webchat_flag = result

        if forced_id or conversation_persona_id is not None:
            return result

        want = (
            self._conf_str("persona_private")
            if _is_private_session(umo)
            else self._conf_str("persona_public")
        )
        if not want:
            return result

        picked = mgr.get_persona_v3_by_id(want)
        if picked is None:
            # 人格名写错/被删了 —— 回退，但要说清楚，别让用户对着"没生效"猜
            logger.warning(
                f"[ncd-monitor] 配置的人格 {want!r} 在 AstrBot 人格库里找不到，"
                "这一轮保持原人格（检查插件配置里的人格名拼写）"
            )
            return result
        return (want, picked, forced_id, webchat_flag)

    def _record_lm_unavailable(self, runner: Any, chunks: int) -> None:
        """记一条「无模型可用」的现场，并打一条 WARNING。

        这是 B3 的落点：以前只看到一句 `All available chat models are unavailable.`，
        完全不知道当时有几个候选、上下文多大、有没有收到任何输出。
        """
        try:
            provider = getattr(runner, "provider", None)
            ids: list[str] = []
            for cand in [provider, *(getattr(runner, "fallback_providers", None) or [])]:
                if cand is None:
                    continue
                cfg = getattr(cand, "provider_config", None) or {}
                pid = str(cfg.get("id") or "").strip()
                if pid:
                    ids.append(pid)
            ctx = getattr(getattr(runner, "run_context", None), "messages", None) or []
            rec = {
                "ts": time.time(),
                "candidates": ids,
                "messages": len(ctx),
                "chunks": chunks,
                "prompt_tokens": int(getattr(self, "last_prompt_tokens", 0) or 0),
            }
            self._lm_unavailable.appendleft(rec)
            logger.warning(
                "[ncd-monitor] 出现「无模型可用」：候选=%s · 上下文 %s 条 · "
                "prompt≈%s token · 收到的 chunk=%s 个。"
                "★ 这个分支意味着每个候选都回了空流（不是没配模型）",
                ids or "（空）",
                rec["messages"],
                rec["prompt_tokens"],
                chunks,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] 记录「无模型可用」失败（不影响对话）: {exc}")

    def _patch_lm_diag(self) -> None:
        """包装 `_iter_llm_responses_with_fallback`：抓住「无模型可用」的现场。

        背景（2026-10-17 查源码确认）：那句
            All available chat models are unavailable.
        来自 tool_loop_agent_runner.py:647-650，而且只是**两个分支里的第二个**：

            candidates = [self.provider, *self.fallback_providers]   # 至少 1 个
            if last_exception: → "All chat models failed: <类型>: <消息>"   # 有真异常
            else:              → "All available chat models are unavailable."

        候选至少有一个，所以走到第二句只能是：**每个候选都产出了空流**
        （一个 chunk 都没有，既没异常也没错误响应）。也就是说它不是
        "没有可用模型"，而是"模型回了空"。原来这句话没有任何上下文，
        只能靠猜——包装一下就有现场了。

        ★ 只在这一句出现时记录，正常对话零开销。
        """
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import (
                ToolLoopAgentRunner,
            )

            cls = ToolLoopAgentRunner
            # 用 __dict__ 而不是 getattr：属性会继承，见 _patch_compressor 的说明。
            if cls.__dict__.get("_ncd_monitor_patched_lmdiag", False):
                logger.info("[ncd-monitor] LLM 回退链已包装过，跳过")
                return

            original = cls.__dict__["_iter_llm_responses_with_fallback"]

            async def patched(self_, *args, **kwargs):  # noqa: ANN001, ANN202
                chunks = 0
                err_text = ""
                async for item in original(self_, *args, **kwargs):
                    # ★ 包 try：这是旁观别人的数据，绝不能让监控的异常
                    #    挡住对话（那会真的挂住这一轮）。
                    try:
                        if getattr(item, "is_chunk", False):
                            chunks += 1
                        elif getattr(item, "role", None) == "err":
                            err_text = str(getattr(item, "completion_text", "") or "")
                    except Exception:  # noqa: BLE001
                        pass
                    yield item
                if _is_lm_unavailable(err_text):
                    plugin = _ACTIVE
                    if plugin is not None:
                        plugin._record_lm_unavailable(self_, chunks)

            cls._iter_llm_responses_with_fallback = patched
            cls._ncd_monitor_patched_lmdiag = True  # type: ignore[attr-defined]
            _WRAPS.append(
                (
                    cls,
                    "_iter_llm_responses_with_fallback",
                    original,
                    "_ncd_monitor_patched_lmdiag",
                )
            )
            logger.info("[ncd-monitor] 已包装 LLM 回退链，可记录「无模型可用」现场")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] 包装 LLM 回退链失败: {exc}")

    def _patch_persona(self) -> None:
        """包装 PersonaManager.resolve_selected_persona()：按会话类型分流人格。

        需求：私聊用「私密人格」，**其它一切**（群聊 / OtherMessage）用「公开人格」。
        人格内容仍然由 AstrBot 的人格库管理，这里只决定「选哪一个」。

        ★ 为什么包装这里，而不是在 on_llm_request 里改提示词：
          那个钩子跑在人格【已经拼进 system_prompt】之后，那时只能做字符串替换，
          脆且容易把别人的提示词切坏。resolve_selected_persona 是 AstrBot 解析
          人格的唯一入口，而且签名里就带 umo（`平台:MessageType:会话`）。
        """
        try:
            from astrbot.core.persona_mgr import PersonaManager

            cls = PersonaManager
            # 用 __dict__ 而不是 getattr：属性会继承，见 _patch_compressor 的说明。
            if cls.__dict__.get("_ncd_monitor_patched", False):
                logger.info("[ncd-monitor] PersonaManager 已被包装过，跳过")
                return
            original = cls.__dict__["resolve_selected_persona"]

            async def patched(  # noqa: ANN001, ANN202
                self_,
                *,
                umo,
                conversation_persona_id,
                platform_name,
                provider_settings=None,
            ):
                result = await original(
                    self_,
                    umo=umo,
                    conversation_persona_id=conversation_persona_id,
                    platform_name=platform_name,
                    provider_settings=provider_settings,
                )
                # ★ 同其它包装：实例运行时取，不捕获进闭包（见 _ACTIVE 说明）
                plugin = _ACTIVE
                if plugin is None:
                    return result
                # 先拿到 AstrBot 自己的结果再决定要不要换 —— 这样"尊重手动"
                # 不需要我们复刻它那套优先级逻辑，直接沿用。
                try:
                    return plugin._pick_persona_by_session(
                        self_, result, umo, conversation_persona_id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[ncd-monitor] 人格分流失败（保持原样）: {exc}")
                    return result

            cls.resolve_selected_persona = patched  # type: ignore[method-assign]
            cls._ncd_monitor_patched = True  # type: ignore[attr-defined]
            _WRAPS.append(
                (cls, "resolve_selected_persona", original, "_ncd_monitor_patched")
            )
            logger.info("[ncd-monitor] 已包装 PersonaManager，可按会话类型分流人格")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] 包装 PersonaManager 失败: {exc}")

    # -----------------------------------------------------------------
    #  知识库按会话类型分流
    # -----------------------------------------------------------------

    def _pick_kb_names_by_session(self, umo: Any) -> list[str]:
        """按会话类型取要用的知识库名。没配就返回空表（= 本插件不干预）。

        ★ 配置值与 AstrBot 自己的 `kb_names` 同语义（列表），所以允许用逗号
          分隔写多个名字 —— 不是预留灵活性，是要跟底层字段的类型对齐。
        """
        raw = (
            self._conf_str("kb_private")
            if _is_private_session(umo)
            else self._conf_str("kb_public")
        )
        return [part.strip() for part in raw.split(",") if part.strip()]

    async def _kb_by_session(
        self, query: str, umo: Any, context: Any
    ) -> tuple[bool, str | None]:
        """按会话类型挑知识库并直接检索。

        返回 (handled, text)：
          handled=True  → 本插件已接管，text 是检索结果（None = 没检到）
          handled=False → 不归本插件管，调用方继续走 AstrBot 原逻辑

        ★ 只在会话【没有显式 kb_config】时才介入：会话级配置是手动意图，
          与人格分流里"手动尊重"同一条规则。
        """
        names = self._pick_kb_names_by_session(umo)
        if not names:
            return False, None

        try:
            cfg = await sp.session_get(str(umo), "kb_config", default={})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[ncd-monitor] 读会话知识库配置失败，交给 AstrBot 原逻辑: {exc}"
            )
            return False, None
        if isinstance(cfg, dict) and "kb_ids" in cfg:
            return False, None

        kb_mgr = getattr(context, "kb_manager", None)
        if kb_mgr is None:
            return False, None

        # 名字一个都对不上就喊一声再退回原逻辑 —— 别让用户对着"没生效"猜
        resolved: list[str] = []
        for name in names:
            try:
                if await kb_mgr.get_kb_by_name(name):
                    resolved.append(name)
            except Exception:  # noqa: BLE001
                continue
        if not resolved:
            logger.warning(
                f"[ncd-monitor] 配置的知识库 {names!r} 在 AstrBot 里找不到，"
                "这一轮不注入（检查插件配置里的知识库名拼写）"
            )
            return False, None

        try:
            config = context.get_config(umo=str(umo))
            top_k = int(config.get("kb_final_top_k", 5) or 5)
            top_k_fusion = int(config.get("kb_fusion_top_k", 20) or 20)
        except Exception:  # noqa: BLE001
            top_k, top_k_fusion = 5, 20

        result = await kb_mgr.retrieve(
            query=query,
            kb_names=resolved,
            top_k_fusion=top_k_fusion,
            top_m_final=top_k,
        )
        hits = (result or {}).get("results") or []
        if not hits:
            return True, None

        # ★ 相关度阈值是【相对分】，不是绝对分：融合分 =
        #   0.9 × min-max归一化稠密分 + 0.1 × min-max归一化稀疏分，
        #   而归一化保证每次检索的第一名都被顶到满分 ——
        #   所以「你好啊」和「支配方羞辱对方的台词」的第一名都是 0.9。
        #   它只能砍掉第 2、3 名的弱命中，砍不掉"第一名本身就是垃圾"。
        #   想要后者，唯一的办法是把 top_k 调到 1。
        threshold = self._conf_float("kb_score_threshold", 0.0)
        if threshold > 0:
            try:
                kept = [h for h in hits if float(h.get("score") or 0.0) >= threshold]
                if not kept:
                    return True, None
                if len(kept) < len(hits):
                    # 复用 AstrBot 自己的排版函数，免得注入格式跟别处分叉。
                    # ★ 失败就退回不筛：宁可有噪声，也不能因为一个私有方法
                    #   改名就把知识库整个搞没（外层 try 会把它变成"不注入"）。
                    text = kb_mgr._format_context(  # noqa: SLF001
                        [
                            SimpleNamespace(
                                kb_name=h.get("kb_name", ""),
                                doc_name=h.get("doc_name", ""),
                                content=h.get("content", ""),
                                score=float(h.get("score") or 0.0),
                            )
                            for h in kept
                        ]
                    )
                    return True, (text or None)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[ncd-monitor] 按相关度过滤知识库失败，本次不过滤: {exc}"
                )

        text = (result or {}).get("context_text", "")
        return True, (text or None)

    def _patch_quote_guard(self) -> None:
        """发送前校验引用源：源消息被撤回/取不到时，去掉引用只发正文。

        AstrBot 的 ResultDecorateStage 在插件 on_decorating_result 之后才插
        Reply，插件钩子拦不住；这里包住 aiocqhttp 的最终 send，在真正发出前
        做一次检查。只检查“延迟超过 quote_guard_after 秒”的回复，避免每条
        消息都多打一次 get_msg；0 关闭。
        """
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ncd-monitor] 引用守卫导入失败（跳过）: {exc}")
            return
        cls = AiocqhttpMessageEvent
        if cls.__dict__.get("_ncd_monitor_patched_quote", False):
            return
        original = cls.__dict__.get("send")
        if original is None:
            logger.warning("[ncd-monitor] 找不到 AiocqhttpMessageEvent.send，引用守卫未启用")
            return

        async def patched(self_, message):  # noqa: ANN001, ANN202
            plugin = _ACTIVE
            if plugin is not None:
                try:
                    await plugin._strip_broken_quote(self_, message)
                except Exception as exc:  # noqa: BLE001
                    # 守卫失败绝不能把正常发送搞挂。
                    logger.debug(f"[ncd-monitor] 引用守卫失败（按原样发送）: {exc}")
            return await original(self_, message)

        cls.send = patched  # type: ignore[method-assign]
        cls._ncd_monitor_patched_quote = True  # type: ignore[attr-defined]
        _WRAPS.append(
            (cls, "send", original, "_ncd_monitor_patched_quote")
        )
        logger.info("[ncd-monitor] 已包装 AiocqhttpMessageEvent.send，可剔除失效引用")

    async def _strip_broken_quote(self, event: Any, message: Any) -> None:
        """延迟回复的触发消息若已撤回，去掉 Reply，正文继续发。"""
        after = self._conf_float("quote_guard_after", 3.0)
        if after <= 0:
            return
        try:
            from astrbot.core.message.components import Reply
        except Exception:  # noqa: BLE001
            return
        chain = getattr(message, "chain", None)
        if not chain:
            return
        msg = getattr(event, "message_obj", None)
        mid = str(getattr(msg, "message_id", "") or "")
        if not mid:
            return
        try:
            ts = float(getattr(msg, "timestamp", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if not ts or time.time() - ts < after:
            return
        targets = [
            c
            for c in chain
            if isinstance(c, Reply) and str(getattr(c, "id", "")) == mid
        ]
        if not targets:
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        routing: dict[str, Any] = {}
        raw = getattr(msg, "raw_message", None)
        if isinstance(raw, dict) and raw.get("self_id"):
            routing["self_id"] = raw["self_id"]
        ok = False
        try:
            data = await asyncio.wait_for(
                bot.call_action("get_msg", message_id=int(mid), **routing),
                timeout=1.2,
            )
            # 被撤回的消息通常取不到 sender/time；只有两者都在才保留引用。
            ok = bool(
                isinstance(data, dict) and data.get("sender") and data.get("time")
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ncd-monitor] 引用源校验失败（按失效处理）: {exc}")
            ok = False
        if not ok:
            chain[:] = [c for c in chain if c not in targets]
            logger.info("[ncd-monitor] 引用源消息已失效，发送时降级为纯文本")

    def _patch_kb(self) -> None:
        """包装 astr_main_agent.retrieve_knowledge_base()：按会话类型分流知识库。

        需求同人格分流：私聊一套知识库，其它一切（群聊等）另一套。

        ★ 为什么不用 AstrBot 原生的两条路：
          · 会话级 `kb_config`：按 umo【精确匹配】，没有"所有私聊"这种粒度，
            新加的私聊好友不会自动生效；
          · 配置方案(umop 路由)：能通配，但它指向的是一整份配置快照
            （AstrBotConfig 独立文件），意味着以后每次改主配置都得同步一份，
            是个维护陷阱。
          所以在这里按 umo 里的 MessageType 分流 —— 和人格用的是同一套判定。

        ★ 为什么包装这个模块级函数而不是 kb_mgr.retrieve：
          kb_mgr.retrieve 的签名里没有 umo（只有 kb_names），判不了会话类型。
          而 retrieve_knowledge_base 是 astr_main_agent._apply_kb 取知识库的
          唯一入口，且 _apply_kb 在【调用点】才解析模块全局名，
          所以换掉模块属性就能生效（已核对 astr_main_agent.py:299）。
        """
        try:
            from astrbot.core import astr_main_agent

            # 用 __dict__ 而不是 getattr：模块对象没有继承，但保持与其它
            # _patch_* 一致的写法，免得以后误改成会走继承的判断。
            if astr_main_agent.__dict__.get("_ncd_monitor_patched", False):
                logger.info("[ncd-monitor] 知识库入口已被包装过，跳过")
                return
            original = astr_main_agent.retrieve_knowledge_base

            async def patched(  # noqa: ANN001, ANN202
                query: str, umo: str, context: Any
            ):
                # ★ 同其它包装：实例运行时取，不捕获进闭包（见 _ACTIVE 说明）
                plugin = _ACTIVE
                if plugin is not None:
                    try:
                        handled, text = await plugin._kb_by_session(
                            query, umo, context
                        )
                        if handled:
                            return text
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            f"[ncd-monitor] 知识库分流失败（退回原逻辑）: {exc}"
                        )
                return await original(query=query, umo=umo, context=context)

            astr_main_agent.retrieve_knowledge_base = patched  # type: ignore[assignment]
            astr_main_agent._ncd_monitor_patched = True  # type: ignore[attr-defined]
            _WRAPS.append(
                (
                    astr_main_agent,
                    "retrieve_knowledge_base",
                    original,
                    "_ncd_monitor_patched",
                )
            )
            logger.info("[ncd-monitor] 已包装知识库入口，可按会话类型分流知识库")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] 包装知识库入口失败: {exc}")

    def _patch_compressor(self) -> None:
        """给 LLMSummaryCompressor.__call__ 包一层，捕获它产出的摘要。

        压缩器返回的消息列表里含：
            user:      "Our previous history conversation summary: <摘要>"
            assistant: "Acknowledged the summary of our previous conversation history."
        从这里把 <摘要> 抠出来。
        """
        try:
            from astrbot.core.agent.context import compressor as compressor_mod

            cls = compressor_mod.LLMSummaryCompressor
            # 用 __dict__ 而不是 getattr：属性会继承，如果以后 AstrBot 给
            # 这个类加了子类，getattr 会把父类的标记读成子类的，导致误跳过。
            if cls.__dict__.get("_ncd_monitor_patched", False):
                logger.info("[ncd-monitor] 压缩器已被包装过，跳过")
                return

            original = cls.__call__

            async def patched(self_, messages):  # noqa: ANN001, ANN202
                # ★ 同 Provider 包装：实例运行时取，不捕获进闭包（见 _ACTIVE 说明）
                plugin = _ACTIVE
                if plugin is None:
                    return await original(self_, messages)
                # ★★ 前置估算必须包在 try 里。
                #    以前这两行裸在 try 之外，于是 _estimate_messages_tokens
                #    一旦抛异常（它要遍历 messages，属于"看别人的数据"），
                #    真正的压缩 original() 根本不会被调用，异常直接冒进
                #    AstrBot 的压缩流程 —— 也就是【监控代码把被监控对象搞挂了】。
                #    这和 Provider 包装的保护级别不一致，这里补齐。
                before_tokens = 0
                before_count = 0
                try:
                    before_tokens = plugin._estimate_messages_tokens(messages)
                    before_count = len(messages)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[ncd-monitor] 压缩前估算失败（不影响压缩本身）: {exc}")

                # ★ 压缩期间打标记：Provider.text_chat 的包装靠它认出
                #   "这次调用是压缩器发的"，从而改走流式、把摘要逐字显示出来。
                #   try/finally 保证一定复位 —— 不复位的话，
                #   后面所有普通回复都会被当成压缩（黄框一直亮着，且流会被记错）。
                mark = _COMPRESSING.set(True)
                try:
                    result = await original(self_, messages)
                finally:
                    _COMPRESSING.reset(mark)

                try:
                    plugin._on_compressed(before_count, before_tokens, result)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[ncd-monitor] 记录压缩失败: {exc}")
                return result

            cls.__call__ = patched
            cls._ncd_monitor_patched = True  # type: ignore[attr-defined]
            _WRAPS.append((cls, "__call__", original, "_ncd_monitor_patched"))
            logger.info("[ncd-monitor] 已包装 LLMSummaryCompressor，可捕获压缩摘要")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] 包装压缩器失败: {exc}")

    def _on_compressed(
        self,
        before_count: int,
        before_tokens: int,
        result: list[Any],
    ) -> None:
        summary_text = ""
        for msg in result or []:
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.startswith(SUMMARY_PREFIX):
                summary_text = content[len(SUMMARY_PREFIX) :].strip()
                break

        self.compression_count += 1
        after_tokens = self._estimate_messages_tokens(result)

        # ★ 判定这一次压缩到底有没有用。
        #   实测踩过：总结那次 LLM 调用失败时，压缩器会【原样 return messages】
        #   （compressor.py:282 的 except 分支），于是 AstrBot 打出
        #   "Compress completed. 13597 -> 13597"，紧接着退化成减半截断。
        #   整个过程【零报错】，面板上只看得出"压缩了一次"—— 只有翻日志才知道
        #   压缩白跑了一趟（白烧一次完整的 LLM 调用），而且下一次还会再来。
        if not summary_text:
            verdict, note = (
                "no_summary",
                "压缩器没有产出摘要。总结那次 LLM 调用失败或返回空时，"
                "AstrBot 会原样保留上下文（compressor.py:282），紧接着退化成"
                "「减半截断」。去 AstrBot 日志搜 `Failed to generate summary`。",
            )
        elif after_tokens >= before_tokens:
            verdict, note = (
                "no_saving",
                f"摘要生成了，但 token 没降（{before_tokens} → {after_tokens}）。"
                "通常是被保留的最近几轮本身就很大 —— 比如一轮里塞了十几张图"
                "（图片落在最新一轮里会被无条件保留，压缩动不了它）。",
            )
        else:
            verdict, note = "ok", ""
        ineffective = verdict != "ok"

        record = {
            "index": self.compression_count,
            "time": time.time(),
            "before_messages": before_count,
            "after_messages": len(result or []),
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "saved_tokens": max(0, before_tokens - after_tokens),
            "summary": summary_text or "(未从结果中解析到摘要文字)",
            "verdict": verdict,
            "ineffective": ineffective,
            "note": note,
        }
        self.summaries.append(record)
        if ineffective:
            # ★ 措辞按 verdict 分开：no_summary 时上下文是【被原样保留】的，
            #   这时候写 "X → Y token" 会自相矛盾（真实失败时两边本来就相等），
            #   只会把以后排查的人带偏。
            detail = (
                f"上下文被原样保留（{before_tokens} token，{before_count} 条消息）"
                if verdict == "no_summary"
                else f"{before_tokens} → {after_tokens} token，消息 {before_count} → {len(result or [])}"
            )
            logger.warning(
                f"[ncd-monitor] ★第 {self.compression_count} 次压缩【没有生效】："
                f"{detail}。{note}"
            )
        else:
            logger.info(
                f"[ncd-monitor] 捕获到第 {self.compression_count} 次上下文压缩，"
                f"摘要 {len(summary_text)} 字，"
                f"消息 {before_count} → {len(result or [])}，"
                f"省下约 {before_tokens - after_tokens} token"
            )

    @staticmethod
    def _content_text(content: Any) -> str:
        """把一条消息的 content 统一取出成纯文本。

        运行时的 content 可能是：
            · 字符串                              "你好"
            · 多模态列表 + dict                    [{"type":"text","text":"你好"}, ...]
            · 多模态列表 + pydantic 对象           [TextPart(text="你好"), ...]   ← ★ 这个漏过
            · Message 对象 / dict

        ★ 踩过的坑：一开始只判断 isinstance(p, dict)，但 AstrBot 运行时
          传进来的其实是 TextPart / ImageURLPart 这些 pydantic 对象，
          isinstance(p, dict) 是 False → 整条多模态消息的文本被丢掉
          → 图片的 [Image Attachment: path ...] 也拿不到 → 缩略图全空。

        所以下面 dict 和对象两种都认。
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content

        def part_text(p: Any) -> str:
            # ① dict 形态
            if isinstance(p, dict):
                t = p.get("text") or p.get("content")
                return t if isinstance(t, str) else ""
            # ② 对象形态（TextPart.text / 类似）
            t = getattr(p, "text", None)
            if isinstance(t, str):
                return t
            c = getattr(p, "content", None)
            if isinstance(c, str):
                return c
            # ③ 纯字符串元素
            if isinstance(p, str):
                return p
            return ""

        if isinstance(content, list):
            return "".join(part_text(p) for p in content)

        if isinstance(content, dict):
            t = content.get("text") or content.get("content")
            return t if isinstance(t, str) else ""

        return part_text(content) or str(content)

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        """粗估一段文本的 token：中文按 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token。"""
        if not text:
            return 0
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        other = len(text) - cjk
        return cjk + other // 4

    @classmethod
    def _estimate_messages_tokens(cls, messages: list[Any]) -> int:
        """粗估 token 数：中文按 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token。

        只作为【拿不到真实 usage 时的兜底】。正常情况下用 API 返回的
        usage.input —— 那才是真实值。
        """
        total = 0
        for msg in messages or []:
            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            text = cls._content_text(content)
            if not text:
                continue
            total += cls._estimate_text_tokens(text)
        return total

    @classmethod
    def _estimate_tool_tokens(cls, req: Any) -> int:
        """估算本轮工具定义占多少 token（把 OpenAI schema 序列化后折算）。

        ★ 只是估算：真正发出去的是 Provider 适配器转换后的 schema，
          字段可能略有差异；这里只用来回答"工具定义吃了多少上下文"。
        """
        func_tool = getattr(req, "func_tool", None)
        if not func_tool or not getattr(func_tool, "tools", None):
            return 0
        try:
            text = json.dumps(func_tool.openai_schema(), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return 0
        return cls._estimate_text_tokens(text)

    def _context_breakdown(
        self,
        system_prompt: str,
        req: Any,
        contexts: list[dict],
        prompt: str,
    ) -> dict[str, int]:
        """本轮请求的上下文构成（本地估算）：系统提示词 / 工具定义 / 对话消息。

        与真实 usage.input 不是同一口径 —— 真实值拿得到时，百分比用真实值，
        这三段只用来回答"大致是哪一块占的"。
        """
        return {
            "system": self._estimate_text_tokens(system_prompt),
            "tools": self._estimate_tool_tokens(req),
            "messages": self._estimate_messages_tokens(
                [*contexts]
                + ([{"role": "user", "content": prompt}] if prompt else [])
            ),
        }

    @classmethod
    def _count_nonempty(cls, messages: list[Any]) -> int:
        """统计有多少条消息是【有内容】的（别把空壳也算进去）。"""
        n = 0
        for msg in messages or []:
            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            if cls._content_text(content).strip():
                n += 1
        return n

    # =================================================================
    #  环境解析：配置优先，其次自动探测，最后缺省值
    # =================================================================

    def _conf_str(self, key: str, default: str = "") -> str:
        """取一个字符串配置项，去掉首尾空白和成对的引号。

        ★ 用户从资源管理器复制路径时经常把引号一起复制进来，
          或者路径里带中文空格；这里统一收拾一下，
          否则 open() 会以一个看不懂的报错失败。
        """
        try:
            raw = self._conf.get(key)
        except Exception:  # noqa: BLE001
            return default
        if raw is None:
            return default
        text = str(raw).strip().strip('"').strip("'").strip()
        return text or default

    def _conf_bool(self, key: str, default: bool) -> bool:
        """取一个布尔配置项。

        ★ 面板存进来的是真 bool，但从 JSON 手改过的可能是字符串
          （"true"/"false"/"是"/"否"）或数字（0/1），这里都认。
          认不出来的值退回 default —— 不能因为配置写歪了就把行为改掉。
        """
        try:
            raw = self._conf.get(key)
        except Exception:  # noqa: BLE001
            return default
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes", "on", "是", "开"):
            return True
        if text in ("false", "0", "no", "off", "否", "关"):
            return False
        return default

    def _conf_float(self, key: str, default: float) -> float:
        """取一个浮点配置项。

        ★ 与 _conf_bool 同样的顾虑：从 JSON 手改过的值可能是字符串，
          认不出来的值一律退回 default —— 不能因为配置写歪了就把行为改掉。
        """
        try:
            raw = self._conf.get(key)
        except Exception:  # noqa: BLE001
            return default
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _env(self) -> dict[str, str]:
        """解析出所有环境相关的值，结果缓存（改配置需重载插件）。

        每一项的来源优先级：
            插件配置  >  自动探测  >  内置缺省

        返回：
            lm_base           LM Studio 地址
            sl_base           SnowLuma WebUI 地址
            sl_dir            SnowLuma 根目录（探测不到就是空串）
            sl_config         OneBot 配置文件路径（读不到就是空串）
            sl_identity_db    身份库路径（读不到就是空串）
        """
        if self._env_cache is not None:
            return self._env_cache

        # ★ 这里必须加锁，否则日志会出现【两行一模一样的"环境解析"】，
        #   看着像加载了两个插件实例。
        #   原因：api_overview 把 _lm_studio_info 和 _snowluma_info 丢进
        #   run_in_executor 的【两个线程】并发跑，两边都会调 _env()；
        #   它们同时看到缓存是空的，于是各算一遍、各打一行日志。
        #   计算本身是幂等的所以没出错，但这个重复日志很误导排查。
        #   （持有锁期间只有 os.path.isdir / glob 这类本地元数据操作，很快。）
        with _ENV_LOCK:
            if self._env_cache is not None:  # 双检：可能已经被另一个线程填好了
                return self._env_cache

            # SnowLuma 根目录：配置里填了就用，没填就就近探测
            sl_dir = self._conf_str("snowluma_dir") or _detect_snowluma_root()

            # OneBot 配置：优先用显式配置的路径，否则挑【最新改动的那个】
            # ★ 同样不能用 _first_match（它按名字排序，换号后会一直读旧号的配置）
            sl_config = self._conf_str("snowluma_onebot_config")
            if not sl_config and sl_dir:
                sl_config = _find_onebot_config(sl_dir)

            # 身份库：优先用显式配置的路径，否则在 data\<QQ号>\ 下挑【最新】那个
            # ★ 不能再用 _first_match —— 它按名字排序，换号后会一直读旧账号的库
            sl_identity_db = self._conf_str("snowluma_identity_db")
            if not sl_identity_db and sl_dir:
                sl_identity_db = _find_identity_db(sl_dir)

            self._env_cache = {
                "lm_base": self._conf_str("lm_studio_base", DEFAULT_LM_STUDIO_BASE),
                "sl_base": self._conf_str("snowluma_base", DEFAULT_SNOWLUMA_BASE),
                "sl_dir": sl_dir,
                "sl_config": sl_config,
                "sl_identity_db": sl_identity_db,
            }
            logger.info(
                "[ncd-monitor] 环境解析: LM Studio=%s · SnowLuma=%s · 根目录=%s",
                self._env_cache["lm_base"],
                self._env_cache["sl_base"],
                sl_dir or "未探测到（在插件配置里填 snowluma_dir）",
            )
            return self._env_cache

    # =================================================================
    #  群号 -> 群名
    # =================================================================

    def _load_group_names(self, force: bool = False) -> None:
        """从 SnowLuma 的身份库读群号->群名。

        ★ 按库文件的 mtime 判断要不要重读：群改名 / 换库都能自动跟上，
          不必重载插件（原来只读一次，改名后要重载才看得见）。
        ★ 读失败时【保留旧表】不清空 —— 库被临时锁住不该让面板突然全掉名字。
        """
        db_path = self._env()["sl_identity_db"]
        if not db_path:
            if not self._group_names_loaded:
                self._group_names_loaded = True
                logger.info(
                    "[ncd-monitor] 未找到 SnowLuma 身份库，群名退化成「群 <群号>」"
                    "（要显示群名请在插件配置里填 snowluma_dir）"
                )
            return

        try:
            mtime = os.path.getmtime(db_path)
        except OSError:
            mtime = 0.0
        if self._group_names_loaded and not force and mtime == self._group_names_mtime:
            return
        self._group_names_loaded = True

        names: dict[str, str] = {}
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                rows = conn.execute("SELECT group_id, group_name FROM groups").fetchall()
                for gid, gname in rows:
                    if gid and gname:
                        names[str(gid)] = str(gname)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 读群名失败（保留上一次的结果）: {exc}")
            return

        self._group_names = names
        self._group_names_mtime = mtime
        logger.info(
            f"[ncd-monitor] 已从 SnowLuma 身份库读到 {len(names)} 个群名（{db_path}）"
        )

    def _refresh_group_names(self) -> None:
        """按 mtime 检查身份库变没变，变了就重读群名。

        ★ 只在【面板轮询】那条路上调（api_overview，而且丢进线程池）。
          绝不能放进 _gate_label / on_llm_request ——
          那条是回复链路，读盘会阻塞事件循环（_gate_label 的注释里写着这条纪律）。
        """
        try:
            self._load_group_names()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 刷新群名失败: {exc}")

    def _group_name(self, group_id: str | None) -> str:
        if not group_id:
            return ""
        if not self._group_names_loaded:
            self._load_group_names()
        return self._group_names.get(str(group_id), "")

    @staticmethod
    def _group_id_from_umo(umo: str) -> str:
        """从 unified_msg_origin 里取群号 / 好友号。

        形如：demo-bot:GroupMessage:200000001
        """
        try:
            if ":" in umo:
                return umo.rsplit(":", 1)[-1]
        except Exception:  # noqa: BLE001
            pass
        return ""

    # =================================================================
    #  各会话上下文占用（按 unified_msg_origin 聚合）
    # =================================================================

    def _session_usage(self, max_ctx: int) -> list[dict[str, Any]]:
        """按会话汇总【最近一次】请求的上下文占用，供左栏进度条卡片使用。

        为什么要按会话分开看：
            这台机器上同时跑着 QQ 群、私聊、以及 WebUI 的 demo-bot 通道，
            它们共用同一个模型上下文上限，但各自的历史长度完全独立 ——
            只看全局那条用量条，会看不出"是哪个会话在逼近压缩线"。

        取的是每个会话【最近一次】快照的估算 token，而不是累加：
            上下文是滚动窗口，累加没有意义；要的是"此刻这个会话占了多少"。

        ⚠️ token 是【估算值】（_estimate_messages_tokens），不是 LM Studio
           回传的真实用量 —— 真实值只有发起过请求的那个会话才有
           （last_prompt_tokens），所以这里统一用估算口径，避免两种口径混着比。
        """
        latest: dict[str, dict[str, Any]] = {}
        for s in self.context_snapshots:
            key = s.get("umo") or ""
            cur = latest.get(key)
            if cur is None or s.get("time", 0) >= cur.get("time", 0):
                latest[key] = s

        # 每个会话的缓存命中：用最近保留的 LLM 调用累加，而不是估算。
        # DeepSeek 的 usage.input 已包含 cached；input_cached 是其中命中部分。
        cache_stats: dict[str, dict[str, int]] = {}

        def add_cache(key: str, input_tokens: Any, cached_tokens: Any) -> None:
            try:
                inp = int(input_tokens)
                cached = int(cached_tokens or 0)
            except (TypeError, ValueError):
                return
            if inp <= 0:
                return
            st = cache_stats.setdefault(
                key, {"input": 0, "cached": 0, "requests": 0}
            )
            st["input"] += inp
            st["cached"] += max(0, min(cached, inp))
            st["requests"] += 1

        for call in self.llm_calls:
            add_cache(
                str(call.get("umo") or ""),
                call.get("input_tokens"),
                call.get("cached_tokens"),
            )

        # 插件刚重载时 llm_calls 是空的；用最近快照里的 response 兜底，
        # 这样面板不会立刻变成“—”，能保留上一轮的真实数字。
        for key, s in latest.items():
            if key in cache_stats:
                continue
            resp = s.get("response") or {}
            if isinstance(resp, dict):
                add_cache(key, resp.get("input_tokens"), resp.get("cached_tokens"))

        rows: list[dict[str, Any]] = []
        for key, s in latest.items():
            tokens = int(s.get("estimated_tokens") or 0)
            tokens += int(s.get("inject_chars") or 0) // 4  # 注入块也占上下文
            gid = s.get("group_id") or ""
            is_group = bool(gid)
            cs = cache_stats.get(key) or {}
            cache_input = int(cs.get("input") or 0)
            cache_hit = int(cs.get("cached") or 0)
            # 认不出 umo 的快照（例如非 agent 路径发起的 provider 调用）
            # 单独成一档，别和真正的私聊混在一起
            if not key:
                label = "未识别会话"
            elif is_group:
                label = self._group_name(gid) or f"群 {gid}"
            else:
                label = s.get("sender_name") or "私聊"
            rows.append(
                {
                    "umo": key,
                    "is_group": is_group,
                    "group_id": gid,
                    "group_name": s.get("group_name") or self._group_name(gid),
                    "sender_name": s.get("sender_name") or "",
                    "label": label or ("未识别会话" if not key else key),
                    "messages": int(s.get("message_count") or 0),
                    "tokens": tokens,
                    "chars": int(s.get("total_chars") or 0),
                    "percent": round(tokens / max_ctx * 100, 1) if max_ctx else 0.0,
                    "time": s.get("time", 0),
                    "requests": sum(
                        1 for x in self.context_snapshots if (x.get("umo") or "") == key
                    ),
                    "cache_requests": int(cs.get("requests") or 0),
                    "cache_input_tokens": cache_input,
                    "cached_tokens": cache_hit,
                    "cache_hit_rate": (
                        round(cache_hit / cache_input * 100, 1)
                        if cache_input
                        else None
                    ),
                }
            )
        # 最占的排最前 —— 这张卡的用处就是一眼看出"谁快爆了"
        rows.sort(key=lambda r: (-r["percent"], -r["time"]))
        return rows

    @staticmethod
    def _preview(text: str, limit: int = 40) -> str:
        t = " ".join((text or "").split())
        return t[:limit] + ("…" if len(t) > limit else "")

    # =================================================================
    #  主动回复判定
    # =================================================================

    @staticmethod
    def _raw_has_wake(event: AstrMessageEvent) -> tuple[bool, str]:
        """从 OneBot 原始事件里判断【用户是不是真的 @ 了机器人】。

        为什么要看 raw_message 而不是 event.is_at_or_wake_command：

          AstrBot 内置插件的 handle_empty_mention 有一个 60 秒"等待器"：
          当有人只发了一个 @机器人（没有内容）时，它会盯着这个会话，
          接下来 60 秒内**任何一条消息**都会被【插入一个 @机器人】后重新入队。

          结果就是：用户明明没 @ 它，is_at_or_wake_command 却是 True。
          实测 2026-10-11 10:38:52 那次：
              SnowLuma 侧只有一条【没有 @】的消息
              AstrBot 侧却出现两条，第二条带 [At:100000000]

          而等待器只修改 message_obj.message，**不动 raw_message**，
          所以 raw_message["message"] 里保留着 QQ 真正发来的消息数组。

        Returns:
            (是否真的被唤醒, 依据说明)
        """
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if not isinstance(raw, dict):
                return False, "no_raw"
            msg = raw.get("message")
            if not isinstance(msg, list):
                return False, "not_array_format"
            self_id = str(event.get_self_id())
            for seg in msg:
                if not isinstance(seg, dict):
                    continue
                stype = seg.get("type")
                data = seg.get("data") or {}
                if stype == "at":
                    qq = str(data.get("qq"))
                    if qq == self_id or qq == "all":
                        return True, f"at:{qq}"
                # OneBot v11 的 reply 段只有 id，没有发送者；
                # 回复机器人这种情况由 _has_reply_to_bot 从解析后的组件判断
            return False, "none"
        except Exception as exc:  # noqa: BLE001
            return False, f"raw_error:{type(exc).__name__}"

    @staticmethod
    def _has_reply_to_bot(event: AstrMessageEvent) -> bool:
        """解析后的组件里有没有"回复机器人的消息"。

        等待器只会补 At，不会造 Reply，所以这里看到的就是用户真的回复了机器人。
        """
        try:
            import astrbot.api.message_components as Comp

            self_id = str(event.get_self_id())
            for comp in event.get_messages() or []:
                if isinstance(comp, Comp.Reply):
                    sid = getattr(comp, "sender_id", None)
                    if sid is not None and str(sid) == self_id:
                        return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # =================================================================
    #  二、采集 LLM 请求 / 响应
    # =================================================================

    def _gate_label(self, umo: str) -> str:
        """给闸门/面板用的人话标签。

        ★ 只用【已经加载好】的群名，绝不在钩子里去读身份库 ——
          这个钩子跑在回复链路上，读盘会阻塞事件循环。
          群名没加载好时退化成群号，不影响功能。
        """
        try:
            tail = umo.rsplit(":", 1)[-1] if ":" in umo else umo
            if "GroupMessage" in umo:
                name = (
                    self._group_names.get(str(tail), "")
                    if self._group_names_loaded
                    else ""
                )
                return f"🏠 {name or tail}"
            return f"👤 {tail}"
        except Exception:  # noqa: BLE001
            return umo

    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(self, event: AstrMessageEvent) -> None:
        # ★ 只有本地模型才需要等 ComfyUI（抢显存）；云端 API 不该被拦
        if (await self._llm_runtime()).get("local", True):
            await self._wait_comfy_idle()
        """在 AstrBot 拿会话锁【之前】插一道全局并发闸。

        插入点：internal.py:217（`OnWaitingLLMRequestEvent`，注释写着
        「等待调用 LLM（在获取锁之前，仅通知）」）。

        ★ 这里 await 就是在排队 —— 排队期间这条消息的回复会被推迟，
          这是【有意】的行为（不是卡住）。闸门本身 fail-open，见 _Gate。
        """
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if not umo:
                return
            _GATE.limit = self._conf_gate_limit()
            _GATE.hold_timeout = float(self._conf_gate_hold_timeout())
            if _GATE.limit <= 0:
                return  # 闸门关闭：完全恢复原来的全并行
            # 先回收僵死槽位 —— 这样"某个会话响应丢了"最多卡到下一条消息就自愈。
            # ★ 被回收的会话，它在插件侧记的 token 也要一起丢掉：否则下一次
            #   _release_gate 会先交回那个陈旧 token（空操作），把新 token 卡住。
            for stale_umo in await _GATE.reap_stale():
                self._gate_tokens.pop(stale_umo, None)
            token = await _GATE.acquire(umo, self._gate_label(umo))
            # 记下 token，等这一轮结束（两个释放点，谁先到谁放）再交回去
            self._gate_tokens.setdefault(umo, []).append(token)
        except Exception as exc:  # noqa: BLE001
            # ★ 绝不把异常抛给回复链路
            logger.warning(f"[ncd-monitor] 并发闸门入口异常，直接放行: {exc}")

    async def _release_gate(self, umo: str) -> None:
        """交回本会话的一个并发 token。

        ★ 有两个释放点，都是"这一轮真的结束了"的信号，谁先到谁放：
            ① on_llm_response  —— 拿到模型回复（正常路径）
            ② after_message_sent —— 回复真的发出去了
          以前只有 ①，于是【模型报错时 ① 永远不会触发】，槽位一直占着 ——
          实测 18:17 一次 LM Studio 超限，把机器人哑了整整 5 分钟
          （上限是 1 时，一个泄漏的槽挡住所有人）。
          ② 覆盖了这条：模型失败时 AstrBot 会发一条错误消息给用户，
          after_message_sent 照样触发（实测 `Prepare to send - … Token坏掉了`）。
        """
        if not umo:
            return
        try:
            tokens = self._gate_tokens.get(umo)
            if not tokens:
                return
            await _GATE.release(tokens.pop(0))
            if not tokens:
                self._gate_tokens.pop(umo, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 释放并发 token 失败（忽略）: {exc}")

    def _conf_gate_limit(self) -> int:
        """并发上限。0 或负数 = 不限制。

        ★ 默认 0（不排队）：对话各走各的，谁也不用等人。
          2026-10-21 用户要求「对话不排队，生图才排队」——生图的串行由
          astrbot_plugin_ncd_comfy 自己的渲染锁负责（同一块 GPU 一次一张图）。
          ★ 想恢复串行就把它设成 1：LM Studio 以 kv_unified=true 加载时 KV 池
          是【共享】的，两个请求各带几千 token 同时进去会顶穿、报
          "Context size has been exceeded"（README「并发与排队」有实测记录）。
        """
        raw = (self._conf or {}).get("max_concurrent_replies")
        try:
            return int(raw) if raw not in (None, "") else 0
        except (TypeError, ValueError):
            return 0

    def _conf_gate_hold_timeout(self) -> float:
        raw = (self._conf or {}).get("gate_hold_timeout")
        try:
            return float(raw) if raw not in (None, "") else 300.0
        except (TypeError, ValueError):
            return 300.0

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        # 只有本地模型才记「刚活动过」，云端 API 不需要出图前让路
        if (await self._llm_runtime()).get("local", True):
            self._gpu_write(llm_ts=time.time())
        try:
            # ★ 惰性补包：插件加载时 provider source 可能还没导入，
            #   这里每次请求前补一遍（幂等、很便宜）。
            #   这一步必须在真正调用 provider 之前完成 —— on_llm_request
            #   正好在 internal.py 里 provider 调用之前触发。
            self._ensure_providers_patched()

            contexts = getattr(req, "contexts", None) or []
            system_prompt = getattr(req, "system_prompt", "") or ""
            prompt = getattr(req, "prompt", "") or ""
            umo = getattr(event, "unified_msg_origin", "") or ""

            # 整个请求的估算（含 system + 历史 + 本轮）
            all_msgs = [{"role": "system", "content": system_prompt}, *contexts]
            if prompt:
                all_msgs.append({"role": "user", "content": prompt})
            estimated = self._estimate_messages_tokens(all_msgs)

            # 上下文构成（本地估算，给面板画三段条 / 明细用）：
            #   system  —— 人设 + 系统提示词
            #   tools   —— 本轮可用的函数工具 schema
            #   messages—— 历史上下文 + 本轮用户消息
            self._ctx_breakdown = {
                **self._context_breakdown(system_prompt, req, contexts, prompt),
                "time": time.time(),
                "umo": umo,
            }

            # 顺带检查：本轮上下文里是否已经带上了压缩摘要
            has_summary = any(
                isinstance(m.get("content"), str)
                and m["content"].startswith(SUMMARY_PREFIX)
                for m in contexts
                if isinstance(m, dict)
            )

            # 判断是不是"主动回复"。
            # 不能只看 event.is_at_or_wake_command —— 内置插件的"空@等待器"
            # 会给消息补一个 At，把没 @ 的消息也变成"已唤醒"（详见 _raw_has_wake）。
            is_group = "GroupMessage" in umo
            try:
                astrbot_says_wake = bool(
                    getattr(event, "is_at_or_wake_command", False)
                )
            except Exception:  # noqa: BLE001
                astrbot_says_wake = True
            raw_wake, raw_reason = self._raw_has_wake(event)
            reply_to_bot = self._has_reply_to_bot(event)
            real_wake = raw_wake or reply_to_bot
            is_active_reply = is_group and not real_wake
            if is_active_reply:
                self.active_reply_count += 1

            # 诊断用：把判定依据记下来，方便核对"某条到底算不算主动回复"
            try:
                comps = [
                    type(c).__name__ for c in (event.get_messages() or [])
                ]
            except Exception:  # noqa: BLE001
                comps = []
            logger.info(
                f"[ncd-monitor] 请求诊断 群聊={is_group} "
                f"AstrBot判定唤醒={astrbot_says_wake} 原始消息真@={raw_wake}({raw_reason}) "
                f"回复机器人={reply_to_bot} → 判定主动回复={is_active_reply} "
                f"组件={comps} umo={umo}"
            )

            gid = self._group_id_from_umo(umo) if is_group else ""
            # ★ 记下"当前会话"，让 Provider 的包装知道这次调用属于谁
            _CURRENT_UMO.set(umo)
            self._last_umo = umo
            # 清掉过期记录：请求发了但响应始终没回来时不能无限堆积
            cutoff = time.time() - _PENDING_TTL
            for stale in [k for k, v in self._pending.items() if v.get("time", 0) < cutoff]:
                self._pending.pop(stale, None)

            self._timeline.append(
                {
                    "ts": time.time(),
                    "kind": "llm.request",
                    "umo": umo,
                    "text": f"请求 · 约 {estimated} tok · {self._count_nonempty(all_msgs)} 条",
                }
            )
            # 按 umo 分开存，等 on_llm_response 拿到真实 usage 后再落盘
            self._pending[umo] = {
                "time": time.time(),
                "umo": umo,
                "is_group": is_group,
                "is_active_reply": is_active_reply,
                "group_id": gid,
                "group_name": self._group_name(gid),
                "sender_name": self._safe_name(event),
                "sender_id": self._safe_id(event),
                "preview": self._preview(prompt),
                "context_messages": self._count_nonempty(all_msgs),
                "estimated_tokens": estimated,
                "has_summary": has_summary,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] on_llm_request 失败: {exc}")

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        try:
            think = getattr(response, "reasoning_content", None) or ""
            if str(think).strip():
                self._thinkings.append(
                    {
                        "ts": time.time(),
                        "umo": str(getattr(event, "unified_msg_origin", "") or ""),
                        "chars": len(str(think)),
                        "text": str(think)[:4000],
                    }
                )
            usage = getattr(response, "usage", None)
            # ★ TokenUsage 的字段是 input_other / input_cached / output，
            #   以及两个 property：input(=other+cached) 和 total。
            #   不要写成 input_tokens / output_tokens —— 那样 getattr 全是 None。
            # ★ 按 umo 取自己那条 —— 不能取"全局唯一那条"。
            #   两个会话交错时（群里一条 + 私聊一条），单槽会被别人覆盖，
            #   先发那条的 group_name / is_active_reply / 起始时间就都配错了。
            umo_ev = getattr(event, "unified_msg_origin", "") or ""
            p = self._pending.pop(umo_ev, None)
            if p is None:
                if len(self._pending) == 1:
                    # 事件 umo 与请求 umo 不一致（正常不该发生）：只有一条就凑合用
                    p = next(iter(self._pending.values()))
                    self._pending.clear()
                elif self._pending:
                    logger.warning(
                        "[ncd-monitor] 响应配不上请求：umo=%s 待配对=%s"
                        "（这一条会缺群名/主动标记/耗时）",
                        umo_ev,
                        list(self._pending),
                    )
            p = p or {}
            now = time.time()
            # ★ 上下文用量用【API 返回的真实 usage.input】，不要用自己估算的。
            #   估算只作为拿不到 usage 时的兜底。
            real_in = getattr(usage, "input", None)
            real_out = getattr(usage, "output", None)
            real_cached = getattr(usage, "input_cached", None)
            ctx_tokens = real_in if real_in is not None else p.get("estimated_tokens", 0)
            self.last_prompt_tokens = ctx_tokens

            # 全局 Token 用量累计（自插件加载）。只统计带真实 usage 的请求：
            # 估算值掺进来会把缓存命中率算歪。
            if real_in is not None:
                try:
                    inp = int(real_in)
                    cached = max(0, min(int(real_cached or 0), inp))
                    out = int(real_out or 0)
                except (TypeError, ValueError):
                    inp = cached = out = None
                if inp is not None:
                    totals = getattr(self, "_usage_totals", None)
                    if totals is None:
                        totals = {"input": 0, "cached": 0, "output": 0, "calls": 0}
                        self._usage_totals = totals
                    totals["input"] += inp
                    totals["cached"] += cached
                    totals["output"] += out
                    totals["calls"] += 1
                    try:
                        await asyncio.to_thread(
                            self._token_daily_add, inp, cached, out
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[ncd-monitor] 每日 Token 记账失败: {exc}")
            self.context_series.append(
                {
                    "time": now,
                    "tokens": ctx_tokens,
                    "estimated": p.get("estimated_tokens", 0),
                    "real": real_in,
                    "messages": p.get("context_messages", 0),
                    "has_summary": p.get("has_summary", False),
                    "umo": p.get("umo", ""),
                }
            )

            record = {
                "time": now,
                "umo": getattr(event, "unified_msg_origin", "") or p.get("umo", ""),
                "sender": self._safe_sender(event),
                "sender_name": self._safe_name(event) or p.get("sender_name", ""),
                "sender_id": self._safe_id(event) or p.get("sender_id", ""),
                "group_id": p.get("group_id", ""),
                "group_name": p.get("group_name", ""),
                "is_group": p.get("is_group", False),
                "is_active_reply": p.get("is_active_reply", False),
                "preview": p.get("preview", ""),
                "context_messages": p.get("context_messages", 0),
                "estimated_tokens": p.get("estimated_tokens", 0),
                # 从请求发出到响应回来的耗时
                "latency_ms": (
                    round((now - p["time"]) * 1000) if p.get("time") else None
                ),
                "input_tokens": real_in,
                "output_tokens": real_out,
                "cached_tokens": real_cached,
                "has_reasoning": bool(getattr(response, "reasoning_content", None)),
                "tool_calls": list(getattr(response, "tools_call_name", None) or []),
                "text_len": len(getattr(response, "completion_text", "") or ""),
                "reply_preview": self._preview(
                    getattr(response, "completion_text", "") or "", 50
                ),
            }
            self._timeline.append(
                {
                    "ts": now,
                    "kind": "llm.response",
                    "umo": umo_ev,
                    "text": f"回复 · {record.get('latency_ms') or 0}ms · "
                    f"{record.get('input_tokens') or 0}→{record.get('output_tokens') or 0} tok"
                    + (
                        f" · 工具 {len(record.get('tool_calls') or [])}"
                        if record.get("tool_calls")
                        else ""
                    ),
                }
            )
            # 前缀诊断（只读）：和同会话上一条请求比，哪一段变了。
            prefix = self._prefix_by_umo.pop(umo_ev, None)
            if prefix is None and p.get("umo"):
                prefix = self._prefix_by_umo.pop(p["umo"], None)
            record["prefix"] = prefix or {}
            prev = None
            for old in reversed(self.llm_calls):
                if (old.get("umo") or "") == record["umo"]:
                    prev = old
                    break
            flags: dict[str, Any] = {}
            if prefix and prev and prev.get("prefix"):
                pp = prev["prefix"]
                flags = {
                    "system": prefix.get("system") != pp.get("system"),
                    "tools": prefix.get("tools") != pp.get("tools"),
                    "head": prefix.get("head") != pp.get("head"),
                    "history": prefix.get("history") != pp.get("history"),
                    "msg_delta": int(record.get("context_messages") or 0)
                    - int(prev.get("context_messages") or 0),
                }
                # 第一处不同的消息位置（1 基）：
                #   0            = 完全一样
                #   1..prev_len  = 头/中间变了（缓存从这里断）
                #   prev_len + 1 = 只有尾部追加（缓存不受影响）
                cur_h = prefix.get("msg_hashes") or []
                prev_h = pp.get("msg_hashes") or []
                first_diff = None
                for i, (a, b) in enumerate(zip(cur_h, prev_h)):
                    if a != b:
                        first_diff = i + 1
                        break
                if first_diff is None:
                    first_diff = (
                        0
                        if len(cur_h) == len(prev_h)
                        else min(len(cur_h), len(prev_h)) + 1
                    )
                flags["first_diff"] = first_diff
                flags["prev_ctx"] = len(prev_h)
                flags["cur_ctx"] = len(cur_h)
                flags["head_chars"] = int(prefix.get("head_chars") or 0)
                flags["prev_head_chars"] = int(pp.get("head_chars") or 0)
                if prev.get("input_tokens"):
                    flags["prev_hit"] = round(
                        (prev.get("cached_tokens") or 0) / prev["input_tokens"] * 100, 1
                    )
                hit_pct = None
                if real_in:
                    try:
                        hit_pct = round((real_cached or 0) / real_in * 100, 1)
                    except (TypeError, ValueError, ZeroDivisionError):
                        hit_pct = None
                flags["hit_pct"] = hit_pct
                # 把「哪一段变了」翻成人话；只做归因，不改变任何请求。
                flags["cause"] = self._cache_cause(
                    flags, first_diff, len(prev_h), hit_pct
                )
            record["prefix_flags"] = flags
            self.llm_calls.append(record)
            # ★ 释放点之一：拿到模型回复（正常路径）。
            #   注意【模型报错时这里不会执行】，所以还有 after_message_sent 那一路。
            await self._release_gate(umo_ev)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ncd-monitor] on_llm_response 失败: {exc}")

    @staticmethod
    def _sender_obj(event: AstrMessageEvent):
        try:
            return getattr(getattr(event, "message_obj", None), "sender", None)
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def _safe_name(cls, event: AstrMessageEvent) -> str:
        s = cls._sender_obj(event)
        if s is None:
            return ""
        return str(getattr(s, "nickname", "") or "")

    @classmethod
    def _safe_id(cls, event: AstrMessageEvent) -> str:
        s = cls._sender_obj(event)
        if s is None:
            return ""
        return str(getattr(s, "user_id", "") or "")

    @classmethod
    def _safe_sender(cls, event: AstrMessageEvent) -> str:
        name = cls._safe_name(event)
        uid = cls._safe_id(event)
        if name and uid:
            return f"{name}({uid})"
        return name or uid

    # =================================================================
    #  三、外部探针（宿主机 / LM Studio / SnowLuma）
    # =================================================================

    @staticmethod
    def _http_ping(url: str, timeout: float = 1.5) -> dict[str, Any]:
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ncd-monitor"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {
                    "ok": True,
                    "status": resp.status,
                    "ms": round((time.time() - t0) * 1000, 1),
                }
        except urllib.error.HTTPError as exc:
            # 有响应但状态码不是 2xx —— 对"存活探测"来说也算活着
            return {
                "ok": True,
                "status": exc.code,
                "ms": round((time.time() - t0) * 1000, 1),
                "note": "有响应",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": type(exc).__name__,
                "ms": round((time.time() - t0) * 1000, 1),
            }

    def _cached_probe(
        self, key: str, fn: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """按 key 缓存一次探活结果，TTL 见 _PROBE_CACHE_SECONDS。

        锁【包住】fn()：两个探针跑在 run_in_executor 的两个线程里，面板刷新
        叠在一起时会有多个线程同时看到空缓存 → 各探一遍 → 请求量根本没降。
        代价是两次探活串行（最长等一个 timeout=2），面板 5 秒的刷新间隔吃得下。
        返回副本，免得调用方往响应体里补字段时把缓存改脏。
        """
        cache = self.__dict__.setdefault("_probe_cache", {})
        lock = self.__dict__.get("_probe_cache_lock")
        if lock is None:
            # setdefault 在 GIL 下是原子的：并发首次进来也只会有一个 Lock 生效。
            lock = self.__dict__.setdefault("_probe_cache_lock", threading.Lock())
        with lock:
            hit = cache.get(key)
            if hit is not None and (time.time() - hit[0]) < _PROBE_CACHE_SECONDS:
                return copy.deepcopy(hit[1])
            value = fn()
            cache[key] = (time.time(), value)
            return copy.deepcopy(value)

    def _lm_studio_info(self) -> dict[str, Any]:
        # 面板每 5 秒刷一次，探活结果没必要跟着刷 —— 见 _cached_probe。
        return self._cached_probe("lm_studio", self._lm_studio_probe)

    def _lm_native_models(self, base: str) -> list[dict[str, Any]] | None:
        """LM Studio 原生 `/api/v0/models`；拿不到返回 None（老版本没这个端点）。

        ★ 实测字段（2026-10-17 对着正在跑的 LM Studio 抓的真实响应）：
            id / object / type / publisher / arch / compatibility_type /
            quantization / state / max_context_length / loaded_context_length /
            capabilities（可选，例 ['tool_use']）

        ★ 这个 API 里【没有】`n_slots` / `n_ctx_slot` —— 那两个只出现在
          LM Studio 自己的 server 日志里。能反映"加载配置漂移"的是
          `loaded_context_length`，以及 `capabilities` 里 `tool_use` 在不在。
        """
        try:
            with urllib.request.urlopen(f"{base}/api/v0/models", timeout=2) as r:
                data = json.loads(r.read().decode())
            items = data.get("data")
            return items if isinstance(items, list) else None
        except Exception:  # noqa: BLE001
            return None

    def _note_lm_drift(self, loaded: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """比对「已加载模型的关键配置」，把变化追加进时间线；返回**当前时间线**（新的在前）。

        返回值语义是【时间线】，不是"本次新增的那几条"：
        面板要能回看"刚才发生过什么漂移"，所以旧条目会一直留着
        （内部封顶 20 条，对外最多给 8 条）。没有新变化时原样返回。

        为什么需要这个：换模型 / 重载模型时 LM Studio 会重新分配上下文长度。
        2026-10-16 实测踩过——同一个 4B 模型一会儿 4 槽 × 8192、一会儿
        1 槽 × 24576，表现成 AstrBot 突然 `Context size has been exceeded`，
        或者干脆不调工具（可能就是 `capabilities` 里 `tool_use` 掉了）。
        当时**没有任何地方记录"什么时候变的"**，只能靠翻日志猜，查了一整轮。

        `capabilities` 里的 `tool_use` 尤其值得盯：它一旦消失，插件注册的
        工具模型全都收不到，日志看起来却一切正常。

        ★ 调用方保证串行（_cached_probe 的锁包住了 fn()），所以这里直接改
          self 上的状态是安全的，不必再加锁。
        """
        sig = {
            m["id"]: (m.get("context"), m.get("tool_use"))
            for m in loaded
            if m.get("id")
        }
        prev = self.__dict__.get("_lm_signature")
        self.__dict__["_lm_signature"] = sig
        if prev is None:
            return []  # 第一次只建基线，不把"启动"当成"变化"

        records = self.__dict__.setdefault("_lm_drift", [])
        now = time.time()
        for mid, (ctx, tool) in sig.items():
            old = prev.get(mid)
            if old is None:
                records.append(
                    {
                        "ts": now,
                        "model": mid,
                        "field": "加载",
                        "old": None,
                        "new": f"ctx={ctx}",
                    }
                )
                continue
            if old[0] != ctx:
                records.append(
                    {
                        "ts": now,
                        "model": mid,
                        "field": "loaded_context_length",
                        "old": old[0],
                        "new": ctx,
                    }
                )
            if old[1] != tool:
                records.append(
                    {
                        "ts": now,
                        "model": mid,
                        "field": "tool_use",
                        "old": old[1],
                        "new": tool,
                    }
                )
        for mid, old in prev.items():
            if mid not in sig:
                records.append(
                    {
                        "ts": now,
                        "model": mid,
                        "field": "卸载",
                        "old": f"ctx={old[0]}",
                        "new": None,
                    }
                )

        if len(records) > 20:
            del records[:-20]
        return list(reversed(records[-8:]))

    def _lm_studio_probe(self) -> dict[str, Any]:
        # ★ 不能是 @staticmethod：下面要读 self._env()（配置 + 自动探测）。
        #   在 @staticmethod 里写 self. 是 NameError，而且异常会被
        #   asyncio.gather 吞在 executor 里，表现成整个 /overview 500
        #   —— 这个坑真踩过，见 README 第二节 ①。
        base = self._env()["lm_base"]
        info: dict[str, Any] = {"reachable": False, "base": base}

        # 优先走原生端点：多给 state / loaded_context_length / capabilities。
        # 老版本 LM Studio 没有它，再退回 OpenAI 兼容的 /v1/models（只有 id）。
        native = self._lm_native_models(base)
        if native is not None:
            info["reachable"] = True
            info["models"] = [m.get("id") for m in native]
            loaded = [
                {
                    "id": m.get("id"),
                    "type": m.get("type"),
                    "context": m.get("loaded_context_length"),
                    "max_context": m.get("max_context_length"),
                    "tool_use": "tool_use" in (m.get("capabilities") or []),
                    "quantization": m.get("quantization"),
                }
                for m in native
                if m.get("state") == "loaded" and m.get("id")
            ]
            info["loaded"] = loaded
            drift = self._note_lm_drift(loaded)
            if drift:
                info["drift"] = drift
            return info

        try:
            with urllib.request.urlopen(f"{base}/v1/models", timeout=2) as r:
                data = json.loads(r.read().decode())
            info["reachable"] = True
            info["models"] = [m.get("id") for m in data.get("data", [])]
            info["detail"] = "老版本 LM Studio（没有 /api/v0/models），看不到加载配置"
        except Exception as exc:  # noqa: BLE001
            info["error"] = type(exc).__name__
        return info

    def _snowluma_info(self) -> dict[str, Any]:
        # 同 _lm_studio_info：缓存 20 秒，别被面板的 5 秒刷新带着一起刷。
        return self._cached_probe("snowluma", self._snowluma_probe)

    def _snowluma_probe(self) -> dict[str, Any]:
        env = self._env()
        base = env["sl_base"]
        info: dict[str, Any] = {"reachable": False, "base": base}
        try:
            with urllib.request.urlopen(f"{base}/", timeout=2) as r:
                info["reachable"] = r.status == 200
        except urllib.error.HTTPError as exc:
            info["reachable"] = True
            info["status"] = exc.code
        except Exception as exc:  # noqa: BLE001
            info["error"] = type(exc).__name__

        # 顺便读 OneBot 配置，看端口和 Token 状态（安全巡检）。
        # ★ 读不到不是错误 —— 很多人根本不用 SnowLuma，或者装在别的地方。
        #   前端据此显示「未配置」而不是一片红。
        cfg_path = env["sl_config"]
        if not cfg_path:
            info["config_error"] = (
                "没找到 OneBot 配置文件。请在插件配置里填 snowluma_dir"
                "（SnowLuma 根目录），或直接填 snowluma_onebot_config。"
            )
            return info
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            nets = cfg.get("networks", {})
            info["config_path"] = cfg_path
            info["endpoints"] = []
            for key in ("httpServers", "wsServers"):
                for idx, item in enumerate(nets.get(key, [])):
                    info["endpoints"].append(
                        {
                            "kind": key,
                            "index": idx,
                            "host": item.get("host"),
                            "port": item.get("port"),
                            # ★ 只暴露"有没有 Token"，绝不下发 Token 本身
                            "has_token": bool(item.get("accessToken")),
                        }
                    )
            info["ws_clients"] = len(nets.get("wsClients", []))
        except Exception as exc:  # noqa: BLE001
            info["config_error"] = f"读取 {cfg_path} 失败: {str(exc)[:100]}"
        return info

    # ★ 不能是 @staticmethod：下面要读 self._gpu_latest（1 秒采样器拿到的 GPU 值）。
    #   改成实例方法后，调用处 self._system_info 依旧是普通 bound method，不受影响。
    def _system_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        try:
            import psutil

            vm = psutil.virtual_memory()
            info["memory"] = {
                "used_gb": round(vm.used / 1024**3, 1),
                "total_gb": round(vm.total / 1024**3, 1),
                "percent": vm.percent,
            }
            info["cpu"] = {
                "percent": psutil.cpu_percent(interval=None),
                "cores": psutil.cpu_count(),
            }
        except Exception as exc:  # noqa: BLE001
            info["psutil_error"] = type(exc).__name__

        # ★ 如果 1 秒采样器已经在跑，GPU 数据直接用它刚拿到的 ——
        #   否则这里会**再 spawn 一个 nvidia-smi**（单次约 100ms），纯浪费。
        if self._gpu_latest is not None:
            info["gpus"] = [dict(self._gpu_latest)]
            return info

        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.used,memory.total,utilization.gpu,"
                    "temperature.gpu,power.draw",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=6,
            )
            gpus = []
            for line in out.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 6:
                    gpus.append(
                        {
                            "name": parts[0],
                            "mem_used": int(parts[1]),
                            "mem_total": int(parts[2]),
                            "util": int(parts[3]),
                            "temp": int(parts[4]),
                            "power": parts[5],
                        }
                    )
            info["gpus"] = gpus
        except Exception as exc:  # noqa: BLE001
            info["gpu_error"] = type(exc).__name__

        return info

    # =================================================================
    #  四、AstrBot 自身状态
    # =================================================================

    def _astrbot_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        try:
            cfg = self.context.get_config()
            settings = cfg.get("provider_settings", {}) or {}
            provider_cfg = None
            for p in cfg.get("provider", []) or []:
                if p.get("enable"):
                    provider_cfg = p
                    break
            if provider_cfg:
                info["model"] = provider_cfg.get("id")
                info["max_context_tokens"] = provider_cfg.get("max_context_tokens")
                info["modalities"] = provider_cfg.get("modalities")
                info["custom_extra_body"] = provider_cfg.get("custom_extra_body")

            compression = (
                cfg.get("agent_runner", {}).get("config", {}).get("compression", {})
            )
            info["compression_config"] = {
                "max_turns": compression.get("max_turns"),
                "keep_recent_ratio": compression.get("keep_recent_ratio"),
                "strategy": compression.get("overflow_strategy"),
            }
            info["compression_threshold"] = 0.82  # compressor.py 的默认值

            ltm = cfg.get("provider_ltm_settings", {}) or {}
            info["active_reply"] = ltm.get("active_reply")
            info["group_message_max_cnt"] = ltm.get("group_message_max_cnt")
        except Exception as exc:  # noqa: BLE001
            info["config_error"] = str(exc)[:150]

        info["uptime_sec"] = round(time.time() - self.started_at)
        info["compression_count"] = self.compression_count
        info["last_prompt_tokens"] = self.last_prompt_tokens
        info["active_reply_count"] = self.active_reply_count
        info["llm_call_count"] = len(self.llm_calls)
        # 思考剥离的账：面板上用来量化"思考到底吃了多少上下文"
        info["keep_reasoning_in_context"] = self._conf_bool(
            "keep_reasoning_in_context", True
        )
        info["reasoning_stripped_segments"] = self.reasoning_stripped_segments
        info["reasoning_stripped_chars"] = self.reasoning_stripped_chars
        return info

    def _token_daily_dir(self) -> Path:
        override = getattr(self, "_token_daily_override", "")
        if override:
            return Path(override)
        return Path(ASTRBOT_ROOT) / "data" / "plugin_data" / "ncd_monitor" / "token_daily"

    @staticmethod
    def _token_day_key(ts: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))

    def _token_day_path(self, day: str) -> Path:
        return self._token_daily_dir() / f"{day}.json"

    def _token_day_load(self, day: str) -> dict[str, int]:
        cache = getattr(self, "_token_day_cache", None)
        if cache is None:
            cache = {}
            self._token_day_cache = cache
        if day in cache:
            return cache[day]
        data = {"input": 0, "cached": 0, "output": 0, "calls": 0}
        try:
            raw = json.loads(self._token_day_path(day).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in data:
                    data[key] = max(0, int(raw.get(key) or 0))
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 读每日 Token 失败 {day}: {exc}")
        cache[day] = data
        return data

    def _token_daily_add(self, inp: int, cached: int, out: int) -> None:
        """把一次真实 usage 计入当天；累计由每日文件求和得到，不另存文件。"""
        lock = getattr(self, "_token_day_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._token_day_lock = lock
        with lock:
            day = self._token_day_key()
            data = self._token_day_load(day)
            data["input"] += max(0, int(inp))
            data["cached"] += max(0, min(int(cached), int(inp)))
            data["output"] += max(0, int(out))
            data["calls"] += 1
            self._token_day_write(day, data)
            self._token_daily_cache_ts = 0.0

    def _token_day_write(self, day: str, data: dict) -> None:
        path = self._token_day_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            tmp.write_text(
                json.dumps({"date": day, **data}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning(f"[ncd-monitor] 写每日 Token 失败 {day}: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _token_day_stats(self, day: str) -> dict[str, Any]:
        data = self._token_day_load(day)
        inp = int(data.get("input") or 0)
        cached = max(0, min(int(data.get("cached") or 0), inp))
        out = int(data.get("output") or 0)
        return {
            "date": day,
            "input": inp,
            "cached": cached,
            "uncached": max(0, inp - cached),
            "output": out,
            "total": inp + out,
            "calls": int(data.get("calls") or 0),
            "cache_hit_rate": round(cached / inp * 100, 1) if inp else None,
        }

    def _token_total_stats(self) -> dict[str, Any]:
        """真正的累计：所有每日账本求和（永久账本为准，重载不清零）。

        ★ 不另存一个累计文件：单独存会和每日文件不一致（补记/改文件后会漂），
          按天文件求和才是真值。文件数 = 天数，很小，跟着 payload 一起缓存。
        """
        try:
            days = sorted(
                p.stem
                for p in self._token_daily_dir().glob("*.json")
                if p.stem[:4].isdigit()
            )
        except OSError:
            days = []
        inp = cached = out = calls = 0
        for day in days:
            raw = self._token_day_load(day)
            v_in = int(raw.get("input") or 0)
            cached += max(0, min(int(raw.get("cached") or 0), v_in))
            inp += v_in
            out += int(raw.get("output") or 0)
            calls += int(raw.get("calls") or 0)
        return {
            "since": days[0] if days else "",
            "days": len(days),
            "input": inp,
            "cached": cached,
            "uncached": max(0, inp - cached),
            "output": out,
            "total": inp + out,
            "calls": calls,
            "cache_hit_rate": round(cached / inp * 100, 1) if inp else None,
        }

    def _token_daily_payload(self) -> dict[str, Any]:
        """今日 + 最近 30 天；文件按天永久保留，不自动清理。"""
        now = time.time()
        cached = getattr(self, "_token_daily_cache", None)
        if cached is not None and now - getattr(
            self, "_token_daily_cache_ts", 0.0
        ) < 10:
            return cached
        try:
            days = sorted(
                (
                    p.stem
                    for p in self._token_daily_dir().glob("*.json")
                    if p.stem[:4].isdigit()
                ),
                reverse=True,
            )
        except OSError:
            days = []
        recent = [self._token_day_stats(day) for day in days[:30]]
        today_key = self._token_day_key()
        today = next(
            (row for row in recent if row["date"] == today_key),
            {
                "date": today_key,
                "input": 0,
                "cached": 0,
                "uncached": 0,
                "output": 0,
                "total": 0,
                "calls": 0,
                "cache_hit_rate": None,
            },
        )
        payload = {
            "today": today,
            "recent": recent,
            "days": len(days),
            "total": self._token_total_stats(),
        }
        self._token_daily_cache = payload
        self._token_daily_cache_ts = now
        return payload

    def _usage_stats(self) -> dict[str, Any]:
        """面板「Token 用量」卡的数据（自插件加载累计）。

        口径与 DSH 面板一致：
            总用量    = 未缓存输入 + 缓存读取 + 输出
            缓存命中  = 缓存读取 / (未缓存输入 + 缓存读取)
        ★ 这里用独立累计器，不对 llm_calls 求和 —— 那个 deque 只有 300 条，
          滚掉旧记录后总数会跟着缩水。
        """
        totals = getattr(self, "_usage_totals", None) or {}
        try:
            inp = int(totals.get("input") or 0)
            cached = max(0, min(int(totals.get("cached") or 0), inp))
            out = int(totals.get("output") or 0)
            calls = int(totals.get("calls") or 0)
        except (TypeError, ValueError):
            inp = cached = out = calls = 0
        return {
            "input": inp,
            "cached": cached,
            "uncached": max(0, inp - cached),
            "output": out,
            "total": inp + out,
            "cache_hit_rate": round(cached / inp * 100, 1) if inp else None,
            "calls": calls,
        }

    def _stats(self) -> dict[str, Any]:
        """汇总统计，给页面顶部的数字卡用。"""
        calls = list(self.llm_calls)
        lat = [c["latency_ms"] for c in calls if c.get("latency_ms") is not None]
        inp = [c["input_tokens"] for c in calls if c.get("input_tokens") is not None]
        out = [c["output_tokens"] for c in calls if c.get("output_tokens") is not None]
        grp = sum(1 for c in calls if c.get("is_group"))
        return {
            "total_calls": len(calls),
            "group_calls": grp,
            "private_calls": len(calls) - grp,
            "active_reply_count": self.active_reply_count,
            "avg_latency_ms": round(sum(lat) / len(lat)) if lat else None,
            "max_latency_ms": max(lat) if lat else None,
            "total_input_tokens": sum(inp) if inp else 0,
            "total_output_tokens": sum(out) if out else 0,
            "group_names_loaded": len(self._group_names),
        }

    # =================================================================
    #  五、Web API（前端用 apiGet 调用）
    # =================================================================

    async def api_overview(self):
        """一次返回页面需要的全部数据。"""
        loop = asyncio.get_running_loop()
        # 阻塞操作丢到线程池，别卡住事件循环
        system, lm, sl, _ = await asyncio.gather(
            loop.run_in_executor(None, self._system_info),
            loop.run_in_executor(None, self._lm_studio_info),
            loop.run_in_executor(None, self._snowluma_info),
            # 群名刷新也在这里做（面板每 5 秒轮询一次 /overview）：库变了才重读，
            # 群改名不用重载插件就能跟上
            loop.run_in_executor(None, self._refresh_group_names),
        )

        astrbot_info = self._astrbot_info()
        max_ctx = astrbot_info.get("max_context_tokens") or 0
        used = self.last_prompt_tokens

        # 最近一次请求的上下文概况 —— 这样在第一次压缩发生【之前】，
        # 页面也不是空的，可以看到上下文慢慢长大、逼近压缩线
        last_ctx = self.context_series[-1] if self.context_series else None
        live_context = {
            "messages": (last_ctx or {}).get("messages", 0),
            "tokens": (last_ctx or {}).get("tokens", used),
            "real": (last_ctx or {}).get("real"),
            "estimated": (last_ctx or {}).get("estimated", 0),
            "has_summary": (last_ctx or {}).get("has_summary", False),
            "time": (last_ctx or {}).get("time"),
            "request_count": len(self.context_series),
        }

        payload = {
            "server_time": time.time(),
            "astrbot": astrbot_info,
            "system": system,
            "lm_studio": lm,
            "lm_unavailable": list(self._lm_unavailable),
            "snowluma": sl,
            "live_context": live_context,
            "stats": self._stats(),
            "usage": self._usage_stats(),
            "usage_daily": self._token_daily_payload(),
            "context_breakdown": dict(self._ctx_breakdown),
            "llm_runtime": await self._llm_runtime(),
            "sessions": self._session_usage(max_ctx),
            "context": {
                "used": used,
                "max": max_ctx,
                "percent": round(used / max_ctx * 100, 1) if max_ctx else 0,
                "threshold": 0.82,
                "threshold_tokens": (
                    int(max_ctx * 0.82) if max_ctx else 0
                ),
            },
            "last_summary": self.summaries[-1] if self.summaries else None,
        }
        return json_response(payload)

    async def api_history(self):
        return json_response(
            {
                "context_series": list(self.context_series),
                "llm_calls": list(self.llm_calls)[-60:],
            }
        )

    async def api_summaries(self):
        return json_response({"items": list(reversed(self.summaries))})

    @staticmethod
    def _short_args(args: Any, limit: int = 220) -> str:
        try:
            text = json.dumps(args or {}, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            text = str(args)
        return text[:limit] + ("…" if len(text) > limit else "")

    @filter.on_using_llm_tool()
    async def on_tool_use(self, event: AstrMessageEvent, tool: Any, tool_args: Any) -> None:
        try:
            name = str(getattr(tool, "name", "") or "?")
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            self._tool_start[f"{umo}|{name}"] = time.time()
        except Exception:  # noqa: BLE001
            pass

    @filter.on_llm_tool_respond()
    async def on_tool_respond(
        self, event: AstrMessageEvent, tool: Any, tool_args: Any, tool_result: Any
    ) -> None:
        try:
            name = str(getattr(tool, "name", "") or "?")
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            started = self._tool_start.pop(f"{umo}|{name}", None)
            dur_ms = round((time.time() - started) * 1000) if started else None
            texts = []
            for item in (getattr(tool_result, "content", None) or []):
                text = getattr(item, "text", None)
                if text:
                    texts.append(str(text))
            result = "\n".join(texts)
            is_err = bool(getattr(tool_result, "isError", False))
            stat = self._tool_stats.setdefault(
                name, {"calls": 0, "errors": 0, "total_ms": 0, "last_ts": 0}
            )
            stat["calls"] += 1
            stat["errors"] += 1 if is_err else 0
            stat["total_ms"] += dur_ms or 0
            stat["last_ts"] = time.time()
            self._timeline.append(
                {
                    "ts": time.time(),
                    "kind": "tool",
                    "umo": umo,
                    "text": f"{name} · {dur_ms or 0}ms · {len(result)}B"
                    + (" · 错误" if is_err else ""),
                }
            )
            self._tool_events.append(
                {
                    "ts": time.time(),
                    "umo": umo,
                    "tool": name,
                    "args": self._short_args(tool_args),
                    "dur_ms": dur_ms,
                    "result_len": len(result),
                    "result_head": result[:400],
                    "error": is_err,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ncd-monitor] 记录工具调用失败: {exc}")

    async def api_timeline(self):
        """最近事件：对话请求/响应 + 工具调用。"""
        return json_response({"items": list(self._timeline)[-120:]})

    async def api_gen_full(self, idx: str = "0"):
        """点开大图：按 recent 下标读原图，返回 base64（不暴露任意路径）。"""
        try:
            i = int(idx)
        except (TypeError, ValueError):
            i = 0
        recent = self._comfy_state_recent()
        if i < 0 or i >= len(recent):
            return json_response({"error": "out of range"}, status_code=404)
        path = str(recent[i].get("path") or "")
        if not path or not os.path.isfile(path):
            return json_response({"error": "no file"}, status_code=404)
        low = path.lower()
        mime = "image/jpeg" if low.endswith((".jpg", ".jpeg")) else (
            "image/webp" if low.endswith(".webp") else "image/png"
        )
        try:
            if os.path.getsize(path) > 25 * 1024 * 1024:
                return json_response({"error": "too large"}, status_code=413)
        except OSError:
            return json_response({"error": "gone"}, status_code=404)

        # ★ 大图先压到 1280px 再回传：桥接层要搬 base64，原图 1~2MB 容易出问题，
        #   压缩后一般 100~300KB，查看够清晰。
        data_url = self._display_image(path)
        if not data_url:
            return json_response({"error": "image decode failed"}, status_code=500)
        return json_response({"url": data_url})

    def _display_image(self, path: str) -> str:
        """生成/读取 1280px 显示图（base64 data URL），缓存到 thumbcache。"""
        try:
            key = hashlib.sha1(
                f"{path}|{os.path.getmtime(path)}|view1280".encode()
            ).hexdigest()[:16]
            cache = os.path.join(self._thumb_dir, f"{key}_view.jpg")
            if os.path.exists(cache):
                with open(cache, "rb") as fh:
                    raw = fh.read()
            else:
                from PIL import Image

                img = Image.open(path).convert("RGB")
                img.thumbnail((1280, 1280))
                os.makedirs(self._thumb_dir, exist_ok=True)
                img.save(cache, "JPEG", quality=82)
                with open(cache, "rb") as fh:
                    raw = fh.read()
            return "data:image/jpeg;base64," + base64.b64encode(raw).decode()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 显示图生成失败: {exc}")
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                return "data:image/jpeg;base64," + base64.b64encode(raw).decode()
            except OSError:
                return ""

    async def api_audit_thumb(self, idx: str = "0"):
        """审核明细的小缩略图（只有被拦的才存了 96px 小图）。"""
        try:
            i = int(idx)
        except (TypeError, ValueError):
            i = 0
        recent = self._comfy_state_audit()
        if i < 0 or i >= len(recent):
            return json_response({"error": "out of range"}, status_code=404)
        path = str(recent[i].get("thumb") or "")
        if not path or not os.path.isfile(path):
            return json_response({"error": "no thumb"}, status_code=404)
        key = hashlib.sha1(f"{path}|{os.path.getmtime(path)}".encode()).hexdigest()[:16]
        url = self._make_thumb(key, path)
        if not url:
            return json_response({"error": "thumb failed"}, status_code=404)
        return json_response({"url": url, "k": key})

    def _comfy_state_recent(self) -> list:
        return self._comfy_state().get("recent") or []

    def _comfy_state_audit(self) -> list:
        return self._comfy_state().get("audit_recent") or []

    @staticmethod
    def _comfy_state() -> dict:
        path = os.path.join(
            ASTRBOT_ROOT, "data", "plugin_data", "ncd_bus", "state", "comfy.json"
        )
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:  # noqa: BLE001
            return {}

    async def api_gen_thumb(self, idx: str = "0"):
        """本地生成卡片的缩略图：按 recent 下标取，路径只来自状态文件。"""
        try:
            i = int(idx)
        except (TypeError, ValueError):
            i = 0
        state = os.path.join(
            ASTRBOT_ROOT, "data", "plugin_data", "ncd_bus", "state", "comfy.json"
        )
        try:
            with open(state, encoding="utf-8") as fh:
                recent = (json.load(fh) or {}).get("recent") or []
        except Exception:  # noqa: BLE001
            recent = []
        if i < 0 or i >= len(recent):
            return json_response({"error": "out of range"}, status_code=404)
        path = str(recent[i].get("path") or "")
        if not path or not os.path.isfile(path):
            return json_response({"error": "no file"}, status_code=404)
        if not path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return json_response({"error": "not image"}, status_code=404)
        try:
            key = hashlib.sha1(
                f"{path}|{os.path.getmtime(path)}".encode()
            ).hexdigest()[:16]
        except OSError:
            return json_response({"error": "gone"}, status_code=404)
        url = self._make_thumb(key, path)
        if not url:
            return json_response({"error": "thumb failed"}, status_code=404)
        return json_response({"url": url, "k": key})

    async def api_thinking(self):
        """最近几次思考（只读内存，仅面板可见）。"""
        return json_response({"items": list(self._thinkings)[-10:]})

    async def api_tools(self):
        """工具调用：最近事件 + 按工具汇总（只读内存）。"""
        stats = {}
        for name, s in self._tool_stats.items():
            calls = int(s.get("calls") or 0)
            stats[name] = {
                "calls": calls,
                "errors": int(s.get("errors") or 0),
                "avg_ms": round((s.get("total_ms") or 0) / calls) if calls else None,
                "last_ts": s.get("last_ts"),
            }
        return json_response(
            {"recent": list(self._tool_events)[-40:], "stats": stats}
        )

    async def api_state(self, name: str = "", **kwargs):
        """读某个插件发布的状态快照（只允许字母数字下划线，防路径穿越）。"""
        safe = "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "_-")
        if not safe:
            return json_response({"empty": True, "error": "缺少状态名"})
        path = os.path.join(
            ASTRBOT_ROOT, "data", "plugin_data", "ncd_bus", "state", f"{safe}.json"
        )
        if not os.path.exists(path):
            return json_response({"empty": True})
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if safe == "comfy":
                self._enrich_comfy(data)
                await self._attach_comfy_queue(data)
            return json_response(data)
        except Exception as exc:  # noqa: BLE001
            return json_response({"empty": True, "error": f"{type(exc).__name__}: {exc}"})

    def _ensure_file_server(self) -> int:
        """启动本地图片服务；用锁保证并发首次请求只起一个 server。"""
        lock = self.__dict__.setdefault("_file_srv_lock", threading.Lock())
        with lock:
            srv = getattr(self, "_file_srv", None)
            if srv is not None:
                return srv.server_address[1]
            return self._start_file_server()

    def _start_file_server(self) -> int:
        """启动本地图片服务（127.0.0.1 随机端口，签名校验，只读临时目录里的图）。"""
        srv = getattr(self, "_file_srv", None)
        if srv is not None:
            return srv.server_address[1]
        secret = getattr(self, "_file_secret", "") or os.urandom(16).hex()
        self._file_secret = secret
        roots = [
            os.path.realpath(os.path.join(ASTRBOT_ROOT, "data", "temp")),
            # 会话分目录后的生成图（comfy_generate 落盘位置）
            os.path.realpath(
                os.path.join(ASTRBOT_ROOT, "data", "plugin_data", "ncd_comfy", "out")
            ),
            # 审核小图
            os.path.realpath(
                os.path.join(
                    ASTRBOT_ROOT,
                    "data",
                    "plugins",
                    "astrbot_plugin_ncd_comfy",
                    "data",
                    "audit",
                )
            ),
        ]
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            roots.append(os.path.realpath(str(get_astrbot_temp_path())))
        except Exception:  # noqa: BLE001
            pass
        roots.append(os.path.realpath(tempfile.gettempdir()))

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401
                pass

            def do_GET(self):
                try:
                    from urllib.parse import parse_qs, unquote, urlparse

                    q = parse_qs(urlparse(self.path).query)
                    p = unquote((q.get("p") or [""])[0])
                    s = (q.get("s") or [""])[0]
                    want = hmac.new(
                        secret.encode(), p.encode(), hashlib.sha256
                    ).hexdigest()[:16]
                    if not p or not hmac.compare_digest(s, want):
                        self.send_error(403)
                        return
                    real = os.path.realpath(p)
                    if not any(
                        real.startswith(r + os.sep) for r in roots
                    ):
                        self.send_error(403)
                        return
                    if not real.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        self.send_error(403)
                        return
                    with open(real, "rb") as fh:
                        raw = fh.read()
                    ctype = "image/jpeg" if real.lower().endswith((".jpg", ".jpeg")) else (
                        "image/webp" if real.lower().endswith(".webp") else "image/png"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception:  # noqa: BLE001
                    try:
                        self.send_error(404)
                    except Exception:  # noqa: BLE001
                        pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._file_srv = srv
        logger.info(f"[ncd-monitor] 本地图片服务已启动: 127.0.0.1:{srv.server_address[1]}")
        return srv.server_address[1]

    def _view_url(self, path: str) -> str:
        """给面板用的直链（带签名）。"""
        try:
            from urllib.parse import quote

            port = self._ensure_file_server()
            sig = hmac.new(
                self._file_secret.encode(), path.encode(), hashlib.sha256
            ).hexdigest()[:16]
            return f"http://127.0.0.1:{port}/i?p={quote(path)}&s={sig}"
        except Exception:  # noqa: BLE001
            return ""

    def _probe_comfy(self, port: int) -> bool:
        """探测本机某个 ComfyUI 端口（0.8 秒超时，5 秒缓存）。"""
        now = time.time()
        cache = getattr(self, "_comfy_probe_cache", None)
        if cache is None:
            cache = self._comfy_probe_cache = {}
        hit = cache.get(port)
        if hit and now - hit[0] < 5:
            return hit[1]
        ok = False
        try:
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/system_stats", timeout=0.8
            ) as resp:
                ok = resp.status == 200
        except Exception:  # noqa: BLE001
            ok = False
        cache[port] = (now, ok)
        return ok

    async def _attach_comfy_queue(self, data: dict) -> None:
        """用 ComfyUI 的 /queue 覆盖总线里的估算队列（跑/等以实际为准）。"""
        status = data.get("status") or {}
        try:
            port = int(status.get("port") or 8199)
        except (TypeError, ValueError):
            port = 8199
        now = time.time()
        cache = getattr(self, "_comfy_queue_cache", None)
        if (
            isinstance(cache, tuple)
            and len(cache) == 3
            and cache[0] == port
            and now - cache[1] < 0.9
        ):
            actual = cache[2]
        else:
            def fetch() -> dict:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/queue", timeout=1.2
                ) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                return raw if isinstance(raw, dict) else {}

            try:
                raw = await asyncio.to_thread(fetch)
                actual = {
                    "ok": True,
                    "alive": True,
                    "port": port,
                    "running": len(raw.get("queue_running") or []),
                    "pending": len(raw.get("queue_pending") or []),
                }
            except Exception as exc:  # noqa: BLE001
                actual = {
                    "ok": False,
                    "alive": False,
                    "port": port,
                    "running": 0,
                    "pending": 0,
                    "error": type(exc).__name__,
                }
            self._comfy_queue_cache = (port, now, actual)
        data["actual"] = actual
        if actual.get("ok"):
            try:
                plugin_running = int(status.get("queue_plugin_running") or 0)
                plugin_pending = int(status.get("queue_plugin_pending") or 0)
            except (TypeError, ValueError):
                plugin_running = plugin_pending = 0
            # ComfyUI 的 /queue 只看已经提交给它的任务；
            # 插件公平队列里还没提交的那张，必须在面板上补进“等”。
            actual["plugin_running"] = plugin_running
            actual["plugin_pending"] = plugin_pending
            actual["display_running"] = max(actual["running"], plugin_running)
            actual["display_pending"] = actual["pending"] + plugin_pending
            status["queue_running"] = actual["display_running"]
            status["queue_pending"] = actual["display_pending"]
            data["status"] = status

    def _enrich_comfy(self, data: dict) -> None:
        """给 comfy 状态附上内联缩略图：面板一次请求就够，不再二次取图。"""
        try:
            # 两个端口：bot 的 8199 由 comfy 插件发布；你自己的完整版默认 8188
            data["external_comfy"] = {
                "port": 8188,
                "running": self._probe_comfy(8188),
            }
            for item in data.get("recent") or []:
                path = str(item.get("path") or "")
                if not path or not os.path.isfile(path):
                    continue
                key = hashlib.sha1(
                    f"{path}|{os.path.getmtime(path)}".encode()
                ).hexdigest()[:16]
                url = self._make_thumb(key, path)
                if url:
                    item["thumb"] = url
                view = self._view_url(path)
                if view:
                    item["view"] = view
            for item in data.get("audit_recent") or []:
                path = str(item.get("thumb") or "")
                if not path or not os.path.isfile(path):
                    continue
                key = hashlib.sha1(
                    f"{path}|{os.path.getmtime(path)}".encode()
                ).hexdigest()[:16]
                url = self._make_thumb(key, path)
                if url:
                    item["thumb_data"] = url
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ncd-monitor] 状态缩略图失败: {exc}")

    async def api_search_stats(self):
        """搜索/抓取统计：直接读工具箱落盘的 JSON，不跨插件调用。"""
        path = os.path.join(
            ASTRBOT_ROOT, "data", "plugin_data", "ncd_toolbox", "search_stats.json"
        )
        if not os.path.exists(path):
            return json_response({"ok": True, "empty": True})
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            return json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        def avg(total: float, count: float) -> float:
            return round(total / count, 1) if count else 0

        data["ok"] = True
        data["avg_search_ms"] = avg(data.get("search_ms", 0), data.get("search", 0))
        data["avg_results"] = avg(data.get("search_results", 0), data.get("search", 0))
        data["avg_fetch_ms"] = avg(data.get("fetch_ms", 0), data.get("fetch", 0))
        data["avg_fetch_chars"] = avg(data.get("fetch_chars", 0), data.get("fetch", 0))
        return json_response(data)

    # ---- 本地模型 / ComfyUI 互斥（共享状态文件） ----
    GPU_GATE = os.path.join(ASTRBOT_ROOT, "data", "plugin_data", "ncd_gpu_gate.json")

    def _gpu_read(self) -> dict:
        try:
            with open(self.GPU_GATE, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _gpu_write(self, **fields) -> None:
        tmp = ""
        try:
            data = self._gpu_read()
            data.update(fields)
            os.makedirs(os.path.dirname(self.GPU_GATE), exist_ok=True)
            tmp = f"{self.GPU_GATE}.{os.getpid()}.{time.time_ns()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.GPU_GATE)
        except Exception:  # noqa: BLE001
            if tmp:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

    def _comfy_active(self) -> bool:
        data = self._gpu_read()
        return bool(data.get("comfy")) and (time.time() - float(data.get("comfy_ts") or 0) < 900)

    @staticmethod
    def _is_local_base(url: str) -> bool:
        """api_base 是不是本机地址（本地模型才需要和出图抢显卡）。"""
        try:
            from urllib.parse import urlsplit

            host = (urlsplit(str(url or "")).hostname or "").lower()
        except Exception:  # noqa: BLE001
            return True
        if not host:
            return True
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        if host.startswith(("10.", "192.168.", "169.254.")):
            return True
        if host.startswith("172."):
            try:
                second = int(host.split(".")[1])
                if 16 <= second <= 31:
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _llm_runtime(self) -> dict:
        """当前 LLM provider 的来源（id/type/base/是否本地），带 30 秒缓存。"""
        cached = getattr(self, "_llm_rt", None)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        info = {"id": "", "type": "", "base": "", "local": True}
        try:
            provider = await self.context.get_using_provider_async()
            cfg = dict(getattr(provider, "provider_config", {}) or {})
            base = str(cfg.get("api_base") or cfg.get("base_url") or "")
            info.update(
                id=str(cfg.get("id") or ""),
                type=str(cfg.get("type") or ""),
                base=base,
                local=self._is_local_base(base) if base else True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ncd-monitor] 读取 provider 失败（按本地处理）: {exc}")
        self._llm_rt = (time.time(), info)
        return info

    async def _wait_comfy_idle(self, timeout: float = 600.0) -> None:
        """ComfyUI 在出图时，等它结束再让 LLM 发请求（本地模型抢显存会 OOM）。"""
        deadline = time.time() + timeout
        while self._comfy_active() and time.time() < deadline:
            await asyncio.sleep(1.0)

    async def api_sysinfo(self):
        """宿主机资源的最新值 + 趋势序列（前端 1 秒拉一次）。

        采样在后台按固定节奏跑，这里只是【读内存】——
        所以 1Hz 拉取很便宜，不会每次都去调 nvidia-smi。

        ★ 顺带带上 `live`（模型正在逐字生成的内容）。
          为什么挂在这条路由上而不是新开一条：
          它同样只是读内存，而这条路由前端【已经】每秒拉一次 ——
          零新增请求、零新增路由就能做到 1 秒粒度的实时输出。
          代价是"一段一段跳"而不是逐字，但那本来就是流式进度的真实反映。
        """
        self._ensure_sampler()
        # 顺手回收僵死并发槽：这条路由前端每秒拉一次，是个可靠的节拍。
        # （另一条自愈路径是"新消息进来时先回收"，见 on_waiting_llm_request）
        for stale_umo in await _GATE.reap_stale():
            self._gate_tokens.pop(stale_umo, None)
        return json_response(
            {
                "interval": self.SYS_SAMPLE_INTERVAL,
                "keep": self.SYS_SAMPLE_KEEP,
                "latest": self.sys_samples[-1] if self.sys_samples else None,
                "series": list(self.sys_samples),
                "live": self._live_payload(),
                # 多路实时输出（按会话分）：前端做「并发」切换用
                "lives": self._lives_payload(),
                # 全局并发闸门状态：前端做「并发 N/M」「排队 N」两个入口用
                "gate": _GATE.snapshot(),
            }
        )

    # ------------------------------------------------------------------
    #  上下文快照落盘（重载后别再凭空清空）
    # ------------------------------------------------------------------

    def _load_snapshots(self) -> None:
        """从 plugin_data 读回上次的快照；读不到就静默跳过。

        ★ 为什么需要（2026-10-17 实测）：面板右栏「发给模型的完整上下文」
          只读内存里的 deque。改插件要重载，一重载就清空 ——
          当时最后一条快照 18:44:49 捕获，18:44:51 重载，面板立刻变成
          「还没有抓到请求」，看起来像 bug，其实只是内存被重置了。
        ★ 存在 plugin_data 而**不是插件目录**：插件目录会被打包分享出去，
          而这里面是完整的聊天正文。
        ★ 全部包 try：缓存坏了绝不能挡住插件启动。
        """
        try:
            with open(self._snap_cache, encoding="utf-8") as f:
                data = json.load(f)
            items = data if isinstance(data, list) else (data.get("items") or [])
            restored = 0
            for snap in items[-20:]:
                if isinstance(snap, dict) and snap.get("id"):
                    self.context_snapshots.append(snap)
                    try:
                        self._snapshot_seq = max(self._snapshot_seq, int(snap["id"]))
                    except (TypeError, ValueError):
                        pass
                    restored += 1
            if restored:
                logger.info(
                    f"[ncd-monitor] 已恢复 {restored} 条上下文快照（重载不再清空）"
                )
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 读上下文快照缓存失败（忽略）: {exc}")

    def _save_snapshots(self, force: bool = False) -> None:
        """把最近 12 条快照写到 plugin_data；最多每 5 秒写一次。

        ★ 节流：一次对话会 append 好几次，每次都整份写盘没必要。
        ★ 只存 12 条：面板够看，文件也不会太大（里面是完整对话文本）。
        ★ 全部包 try：写盘失败绝不能影响对话。
        """
        now = time.time()
        if not force and (now - self._snap_saved_at) < 5.0:
            return
        self._snap_saved_at = now
        try:
            items = list(self.context_snapshots)[-12:]
            parent = os.path.dirname(self._snap_cache)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self._snap_cache + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
            os.replace(tmp, self._snap_cache)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 写上下文快照缓存失败（忽略）: {exc}")

    async def api_contexts(self):
        """每次真正发给模型的消息列表，按时间倒序（最新在前）。

        快照来自 Provider.text_chat 包装：
            kind="chat"      普通对话
            kind="compress"  上下文压缩请求（输入=被压缩的旧对话，response=摘要）

        每条消息含三个字段：
            content   正文（已清洗）
            reminder  AstrBot 塞的系统提醒（User ID / 时间等），单独给前端
            images    [{"k": 缩略图缓存键, "n": 文件名}]，图片数据靠 /thumb 另取
        """
        items = []
        for s in reversed(self.context_snapshots):
            items.append(
                {
                    "id": s["id"],
                    "time": s["time"],
                    "kind": s["kind"],
                    "message_count": s["message_count"],
                    "image_count": s.get("image_count", 0),
                    "total_chars": s["total_chars"],
                    "estimated_tokens": s["estimated_tokens"],
                    "tools": s["tools"],
                    "umo": s["umo"],
                    "group_id": s.get("group_id", ""),
                    "group_name": s["group_name"],
                    "sender_name": s["sender_name"],
                    "sender_id": s.get("sender_id", ""),
                    "has_summary": s["has_summary"],
                    "error": s["error"],
                    "summary": s.get("summary"),
                    "response": s["response"],
                    "messages": s["messages"],
                }
            )
        return json_response({"items": items, "count": len(items)})

    # =================================================================
    #  缩略图：图片原图平均 576 KB，直接塞进来会把页面拖垮
    #  （实测 524 个文件共 294.6 MB，24 张图内嵌 base64 约 18 MB）
    #  所以服务端缩到 96px 再当 base64 传，并按路径 hash 缓存到磁盘。
    #
    #  为什么不用 <img src=".../thumb">：
    #      插件页面在 iframe 里，凭证由父窗口持有、靠 postMessage 转发
    #      （plugin_page_bridge.js 里根本没有 token）。
    #      实测直接访问插件 API 是 401，所以 <img src> 走不通，
    #      只能走桥把图片当数据传。
    # =================================================================

    THUMB_SIZE = 96
    THUMB_QUALITY = 70

    def _thumb_file(self, key: str) -> str:
        return os.path.join(self._thumb_dir, f"{key}.jpg")

    def _make_thumb(self, key: str, path: str) -> str | None:
        """生成/读取缩略图，返回 base64 data URL；失败返回 None。

        ★ 顺序很关键：**先确认这个 key 是登记过的，再碰磁盘**。
          以前是先按客户端给的 key 拼路径读缓存，于是
              key = "..\\..\\..\\Users\\x\\Pictures\\shot"
          就能把任意 .jpg 读成 base64（key 直接参与拼路径）。
          登记过的 key 一定是 sha1(path)[:16]，永远不会越界。

        ★ 另加一道 realpath 前缀校验做纵深防御：
          即使 key 绕过了字符集检查，也不允许命中缓存目录之外的路径。
        """
        if not path or not os.path.exists(path):
            return None

        cache = self._thumb_file(key)
        try:
            real_dir = os.path.realpath(self._thumb_dir)
            real_cache = os.path.realpath(cache)
            if os.path.dirname(real_cache) != real_dir:
                logger.warning(f"[ncd-monitor] /thumb 键越界，已拒绝: {key!r}")
                return None
            if os.path.exists(real_cache):
                with open(real_cache, "rb") as fh:
                    return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()
        except Exception:  # noqa: BLE001
            pass

        try:
            from PIL import Image

            os.makedirs(self._thumb_dir, exist_ok=True)
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((self.THUMB_SIZE, self.THUMB_SIZE))
                im.save(cache, "JPEG", quality=self.THUMB_QUALITY)
            with open(cache, "rb") as fh:
                return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ncd-monitor] 生成缩略图失败 {path}: {exc}")
            return None

    # 合法的缩略图缓存键：sha1 十六进制取前 16 位（见 _capture_text_chat 的登记处）。
    # ★ 必须白名单化 —— 这个 key 会被拼进文件路径（os.path.join(dir, key + ".jpg")），
    #   不校验就等于把"读哪个文件"的决定权交给了调用方。
    _THUMB_KEY_RE = re.compile(r"[0-9a-f]{16}")

    async def api_thumb(self, key: str = "", **kwargs):
        """按缓存键取一张缩略图（键由 /contexts 给出）。

        路径从 _thumb_paths 里查 —— 每次 /contexts 都会登记一遍，
        所以顺序是「先拉 contexts，再拉缩略图」，前端照做即可。

        ★ 为什么 key 走【路径】而不是 query：
          AstrBot 调用 view_handler 时只把路由正则捕获的名字传进去 ——
            return await run_maybe_async(lambda: view_handler(**path_values))
          （dashboard/api/plugins.py:217），query 参数【根本不会传】。
          而路由里只有 <key> 这种尖括号写法才会生成捕获组：
            _plugin_api_route_pattern: r"<(?:(path):)?([A-Za-z_][A-Za-z0-9_]*)>"
          {key} 和 :key 都会被当成字面量，匹配不上。
        """
        key = str(key or kwargs.get("k") or "").strip()
        # ★ 必须先做字符集白名单：key 会被拼进文件路径，见 _make_thumb 的说明。
        #   合法键只可能是 sha1(path) 的前 16 位十六进制。
        if not self._THUMB_KEY_RE.fullmatch(key):
            logger.warning(f"[ncd-monitor] /thumb 键非法（应为 16 位十六进制）: {key!r}")
            return json_response({"error": "bad key"}, status_code=400)

        path = self._thumb_paths.get(key, "")
        if not path:
            logger.warning(
                f"[ncd-monitor] /thumb 键 {key} 没有登记过路径"
                f"（已登记 {len(self._thumb_paths)} 个）"
            )
        data = self._make_thumb(key, path)
        if not data:
            logger.warning(
                f"[ncd-monitor] /thumb 生成失败 key={key} path={path!r} "
                f"存在={os.path.exists(path) if path else False}"
            )
            return json_response({"error": "not found", "k": key}, status_code=404)
        return json_response({"k": key, "data": data})
