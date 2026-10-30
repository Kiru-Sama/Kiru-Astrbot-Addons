"""链路工具箱：自建网页检索 + QQ 收藏表情发送 + 本机 SDXL 画图。

这几件事都刻意不走 AstrBot 的内置工具：
  · 内置的 web_search_* 全部依赖第三方付费 API key，且只回 URL + 摘要，抓不到正文；
  · 内置没有「以收藏表情样式发出动画表情」的能力（send_message_to_user 只能发普通图片/文件）；
  · 内置没有画图能力。

本插件改为：
  · 搜索/抓正文走你本机的 Edge —— 真浏览器，能渲染 JS、复用登录态、不被无头风控误伤；
  · 发表情走 QQ 账号自己的收藏表情夹；
  · 画图已拆成独立插件 astrbot_plugin_ncd_draw（2026-10-19 整理）。

暴露给模型的工具：
  web_search_bing   搜网页，返回标题/网址/摘要
  fetch_page        打开某个网址，取渲染后的正文
  list_stickers     列出收藏表情（分页，只给文字，不给图）
  preview_sticker   按序号取一张的缩略图给模型看（真·看图确认）
  send_sticker      按序号把收藏表情发到当前会话

配置项见同目录 `_conf_schema.json`；AstrBot 会把值存到
`data/config/astrbot_plugin_ncd_toolbox_config.json`。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import mcp.types

import astrbot.api.message_components as Comp
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

# 发图要用：AstrBot 的 MessageChain 在这里（api.message_components 没有导出它）
from astrbot.core.message.message_event_result import MessageChain

# 插件页面（生图界面）用的 Web API 辅助
from astrbot.api.web import file_response, json_response
from astrbot.api.web import request as web_request

try:  # 组装给 LLM 的消息对象，和压缩器用的是同一个类
    from astrbot.core.agent.message import Message, TextPart
except Exception:  # noqa: BLE001
    Message = None  # type: ignore[assignment]
    TextPart = None  # type: ignore[assignment]
from astrbot.api.provider import ProviderRequest

try:  # AstrBot 以包的形式加载插件（data.plugins.<目录>.main）
    from .avatar import AvatarError, fetch_avatar, is_qq, parse_target
    from .history import ALLOWED_COUNTS, DEFAULT_COUNT, format_transcript
    from .browser import DEFAULT_SEARCH_URL, Browser, find_edge, public_url_error
    from . import deepseek_web
    from .stickers import (
        StickerError,
        collection_key,
        fetch_all,
        find_by_index,
        load_preview,
        render_list,
        send_sticker,
        shuffled,
    )
except ImportError:  # 直接以顶层模块导入时（离线单测）
    from avatar import AvatarError, fetch_avatar, is_qq, parse_target
    from history import ALLOWED_COUNTS, DEFAULT_COUNT, format_transcript
    from browser import DEFAULT_SEARCH_URL, Browser, find_edge, public_url_error
    import deepseek_web  # type: ignore
    from stickers import (
        StickerError,
        collection_key,
        fetch_all,
        find_by_index,
        load_preview,
        render_list,
        send_sticker,
        shuffled,
    )

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR_NAME = os.path.basename(PLUGIN_DIR)

# AstrBot 安装根目录（本文件往上四层：<AstrBot>\data\plugins\<插件>\main.py）
ASTRBOT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(PLUGIN_DIR)))


def default_profile_dir() -> str:
    """Edge 专用配置目录的默认位置。

    ★ 绝不能放在插件目录里。这份 Edge 配置含有登录态与账号信息 ——
      实测里面就有微软账号邮箱、姓名、手机号。而插件目录是整个插件里
      最容易被复制、打包、备份、分享出去的地方，等于把个人账号一起送出去。
      AstrBot 的 data/plugin_data 才是插件运行数据该待的地方。
    """
    return os.path.join(
        ASTRBOT_ROOT, "data", "plugin_data", "ncd_toolbox", "browser_profile"
    )

# 可选的搜索引擎模板。{query} 会被替换成已 URL 编码的关键词。
SEARCH_PRESETS = {
    "bing": "https://cn.bing.com/search?q={query}",
    "baidu": "https://www.baidu.com/s?wd={query}",
}

MAX_SEARCH_COUNT = 20

# 搜索查询里的客套话/指令词。模型常把「帮我搜一下今天显卡多少钱」原样当 query，
# 这些词对搜索引擎毫无帮助、还会稀释关键词。
# ★ 只收【多字】短语，且按长度从长到短处理：
#   单字（请/看/的）太容易误伤 —— "请求" 会被剥成 "求"，
#   这类误伤比省下两个字符严重得多。
_QUERY_NOISE = tuple(
    sorted(
        (
            "帮我搜一下", "帮我查一下", "帮我搜索", "帮我查找", "帮我搜", "帮我查",
            "搜索一下", "查询一下", "请问一下", "麻烦你", "麻烦",
            "能不能", "可不可以", "可以帮我", "我想知道", "我想要", "我想",
            "搜一下", "搜搜", "查一下", "查查", "找一下", "找找", "看看",
            "给我", "请问", "谢谢", "帮忙", "帮我",
        ),
        key=len,
        reverse=True,
    )
)

# 结果里同一站点最多留几条（防止 10 条里 6 条来自同一个站）
MAX_PER_DOMAIN = 2

# 结果网址里要剥掉的跟踪参数
_TRACKING_PARAMS = ("utm_", "spm", "from", "ref", "src", "fbclid", "gclid", "msclkid")


def clean_query(query: str) -> str:
    """把「帮我搜一下 xxx 谢谢」清洗成「xxx」。

    ★ 只剥开头/结尾的客套词，中间一概不动 —— 中间才是关键词。
    ★ 剥完如果剩下不到 2 个字符，就退回原文（宁可脏一点，也别搜个空串）。
    """
    raw = str(query or "").strip()
    q = raw.rstrip("？?。！!～~，, 　")
    changed = True
    while changed:
        changed = False
        for word in _QUERY_NOISE:
            if q.startswith(word) and len(q) - len(word) >= 2:
                q = q[len(word):].lstrip("：:，, 　的")
                changed = True
            if q.endswith(word) and len(q) - len(word) >= 2:
                q = q[: -len(word)].rstrip("：:，, 　")
                changed = True
    q = " ".join(q.split())
    return q if len(q) >= 2 else raw


def _norm_result_url(url: str) -> str:
    """结果网址判重用：去掉跟踪参数和末尾斜杠。"""
    text = str(url or "").strip()
    if not text:
        return ""
    if "?" in text:
        base, _, query = text.partition("?")
        kept = []
        for pair in query.split("&"):
            name = pair.split("=", 1)[0].lower()
            if not name or any(name.startswith(t) or name == t for t in _TRACKING_PARAMS):
                continue
            kept.append(pair)
        text = base + ("?" + "&".join(kept) if kept else "")
    return text.rstrip("/")


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        return (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


_STATS_PATH = Path(ASTRBOT_ROOT) / "data" / "plugin_data" / "ncd_toolbox" / "search_stats.json"
_STATS: dict[str, float] = {
    "search": 0, "search_ms": 0, "search_results": 0,
    "fetch": 0, "fetch_ms": 0, "fetch_chars": 0,
    "summarize": 0, "cache_hit": 0, "read_many": 0,
}
_STOPWORDS = {"什么", "怎么", "为什么", "如何", "哪些", "可以", "一下", "帮我", "搜", "查", "的", "了", "是", "和", "吗"}


def keywords_of(text: str, limit: int = 8) -> list[str]:
    """从一句话里挑关键词（去掉疑问词/虚词），给正文取窗口用。"""
    src = str(text or "")
    # ★ 中文没有空格：先把疑问词/虚词当子串删掉，否则「帮我搜一下」会整句变成一个"关键词"
    for w in sorted(_STOPWORDS, key=len, reverse=True):
        if len(w) >= 2:
            src = src.replace(w, " ")
    words = re.findall(r"[\w\u4e00-\u9fff]{2,}", src)
    out: list[str] = []
    for w in words:
        if w in _STOPWORDS or w.lower() in _STOPWORDS:
            continue
        if w not in out:
            out.append(w)
        if len(out) >= limit:
            break
    return out


def stats_add(**fields: float) -> None:
    """累加统计并落盘（失败不影响主流程）。"""
    tmp = _STATS_PATH.with_name(
        f"{_STATS_PATH.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        for k, v in fields.items():
            _STATS[k] = _STATS.get(k, 0) + v
        _STATS["updated"] = time.time()
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(_STATS, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _STATS_PATH)
    except Exception:  # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def dedupe_results(items: list[dict], per_domain: int = MAX_PER_DOMAIN) -> list[dict]:
    """按网址去重 + 限制单站点条数，保持原顺序。

    ★ 为什么限站点：Bing 第一页经常被同一个大站刷满（知乎/CSDN/百家号），
      10 条里 6 条同源，模型看着像"搜到很多"，实际只有一个来源。
    """
    seen: set[str] = set()
    per: dict[str, int] = {}
    out: list[dict] = []
    for item in items:
        norm = _norm_result_url(item.get("url") or "")
        if not norm or norm in seen:
            continue
        domain = _domain_of(norm)
        if domain and per.get(domain, 0) >= per_domain:
            continue
        seen.add(norm)
        if domain:
            per[domain] = per.get(domain, 0) + 1
        out.append(item)
    return out


# ----------------------------------------------------------------------
#  出站文本清洗：把 Markdown 洗成 QQ 能看的纯文本
# ----------------------------------------------------------------------
# ★ 为什么必须做兜底：QQ 不渲染 Markdown，模型（尤其 4B）很爱输出
#   `##` 标题、`|---|` 表格、`**加粗**` —— 在聊天里就是一堆乱符号。
#   光在提示词里说"别用 Markdown"不够（实测照样吐表格），所以出口再洗一遍。
# ★ 只洗"明显是标记"的东西，不碰正文：链接转成「标题（网址）」、表格转成
#   用全角空格分隔的几行、列表符号换成「·」。
_MD_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]*\|[\s:|-]*$")
_MD_HR = re.compile(r"^[ \t]*([-*_])[ \t]*(\1[ \t]*){2,}$", re.M)
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_MD_STRIKE = re.compile(r"~~(.+?)~~")
_MD_CODE = re.compile(r"`{1,3}([^`\n]+?)`{1,3}")
_MD_ITALIC = re.compile(r"(?<![\w*])([*_])(?!\s)([^*_\n]+?)(?<!\s)\1(?![\w*])")
_MD_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.M)
_MD_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.M)


def strip_markdown(text: str) -> str:
    """把 Markdown 洗成纯文本。空值原样返回，没标记时也原样返回。"""
    if not text:
        return text
    s = str(text).replace("\r\n", "\n")
    # 代码块围栏去掉，保留里面的内容
    s = re.sub(r"```[A-Za-z0-9_+-]*\n?", "", s)

    # 表格：分隔行整行丢弃；数据行去掉首尾竖线、单元格用全角空格隔开
    lines: list[str] = []
    for line in s.split("\n"):
        stripped = line.strip()
        if "|" in stripped and _MD_TABLE_SEP.match(stripped):
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            cells = [c for c in cells if c]
            if cells:
                lines.append("　".join(cells))
                continue
        lines.append(line)
    s = "\n".join(lines)

    s = _MD_HR.sub("", s)
    s = _MD_HEADING.sub("", s)
    s = _MD_LINK.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", s)
    s = _MD_BOLD.sub(r"\2", s)
    s = _MD_STRIKE.sub(r"\1", s)
    s = _MD_CODE.sub(r"\1", s)
    s = _MD_ITALIC.sub(r"\2", s)
    s = _MD_BULLET.sub("· ", s)
    s = _MD_QUOTE.sub("", s)

    s = "\n".join(ln.rstrip() for ln in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# 跨插件约定：抓正文后的「总结」调用会在 prompt 开头带上这个标记自报身份。
# 监控插件（astrbot_plugin_ncd_monitor）据此把它排除在会话统计之外 ——
# 否则那 4000 字网页正文会被记成一次正常请求，面板右栏凭空多一坨，
# 而且「快压缩了吗」的判断会跟着虚高。改这里要同步改对面那一份。
_INTERNAL_REQUEST_MARKER = "[内部·网页总结]"

# 打乱顺序缓存最多留几个会话。正常就一两个会话，纯粹防呆。
MAX_STICKER_ORDERS = 64

# 工具菜单的入口工具名。开了 tool_menu 之后它是【唯一】常驻的工具，
# 模型调它一次就点开本会话的其余工具（见 _apply_tool_menu）。
MENU_TOOL = "ncd_tools"

# 本插件注册的工具名，用于自检日志
# ★ 沉浸模式那条「只保留本插件工具」的过滤不能一刀切 —— 它会把姊妹插件的工具
#   也删掉，实测表现为：工具少了一半，模型直接说“我不会画图/不会读文件”。
#   这里显式放行别的插件提供的工具；以后谁再加工具，记得同步加到这一行。
#   （2026-10-22：清掉已下线的独立绘图插件 ncd_draw 的四个旧工具名，避免误导）
FOREIGN_KEEP = ("comfy_tools", "comfy_generate", "comfy_models", "comfy_status",
                "comfy_say", "comfy_output", "comfy_gallery", "comfy_prompt_help",
                "comfy_tag_lookup",
                "memory_write", "memory_search", "memory_forget",
                "extend_turns", "read_attachment",
                  "feedback_submit")

TOOL_NAMES = (
    MENU_TOOL,
    "web_search_bing",
    "fetch_page",
    "web_read_many",
    "list_stickers",
    "preview_sticker",
    "send_sticker",
    "view_avatar",
    "recall_history",
)
"""本插件提供的全部工具名。

★ 沉浸 / 半沉浸模式会把【不在这个元组里】的工具从 req.func_tool 里移除。
  所以新增任何一个 `@filter.llm_tool` 都必须同步加到这里 ——
  否则它在沉浸模式下会被静默过滤掉，表现是"机器人死活不调这个工具"。
  `测试-工具注册.py` 会用源码里的装饰器名和这个元组做双向比对，漏一个就红。
"""


# 每个工具给一行"说人话"的说明，给 ncd_tools 的清单用。
# ★ 必须覆盖 TOOL_NAMES 里的每一个（MENU_TOOL 除外）；测试会核对。
#   漏了也不会消失 —— 清单里会显示"（新增工具，参数见工具定义）"，
#   但那样模型容易填错参数，所以还是补上更好。
TOOL_SUMMARY: dict[str, str] = {
    "web_search_bing": "web_search_bing(query, count) —— 用本机浏览器搜网页，只给标题和网址",
    "fetch_page": "fetch_page(url, query) —— 打开一个网页，按关键词取相关段落并总结",
    "web_read_many": "web_read_many(urls, query) —— 一次并发读最多 4 个网页，各取相关段落"
    "（比一条条 fetch_page 快，适合比较多个来源）",
    "list_stickers": "list_stickers(page) —— 列出收藏表情包（只给序号和备注）",
    "preview_sticker": "preview_sticker(index) —— 看第 index 张表情的实际图片",
    "send_sticker": "send_sticker(index) —— 把第 index 张表情发到当前会话",
    "view_avatar": "view_avatar(who) —— 看头像，也能看【你自己】的形象（who 填 me）；"
    "不给 who 就是当前说话的人；图只给你自己看，不会发给用户",
    "recall_history": "recall_history(count) —— 回溯最近 30/50/70/90 条历史重看"
    "（平时只看得到最近几条）",
}


class ModelError(Exception):
    """底模名字解析失败（可预期的用户错误，要给人话而不是栈）。"""

# 默认注入的能力提示（可在插件设置里改；留空 = 不注入）。
# ★ 为什么要注入：开了工具菜单之后，模型第一轮只看得到 MENU_TOOL 一个工具，
#   它必须"知道自己会什么"才会想到去点菜单。这段就是那本人肉说明书。
DEFAULT_MENU_HINT = (
    "你手头有一套外部工具可以用来：搜网页、读网页正文、翻自己的收藏表情包、"
    "以及看某个人的头像（图只给你自己看，不会发给用户）。\n"
    "要查东西 / 想发表情包 / 想画图之前，先调用工具： ncd_tools "
    "拿到当轮可用的工具清单，再按清单里的名字去调用具体工具。不要凭印象编工具名。\n"
    "画图 / 发语音是另一个插件（链路本地生成）的事：直接调 comfy_tools 看参数，再调 comfy_generate / comfy_say。\n"
    "云端 API 下出图是异步的：先回复，画好自动发图；翻旧图用 comfy_gallery + comfy_output。\n"
    "调用方式：这是「工具调用」，不是聊天 —— 要真的把工具调起来、"
    "参数按工具定义里的字段填，不要只用文字说「我去搜一下」。\n"
    "一次只调一个，等结果回来再决定下一步；ncd_tools 返回的清单里写了每个工具要哪些参数，照着填。\n"
    "另外：回复用【纯文本】，不要 Markdown —— 不要 ## 标题、不要 |表格|、不要 **加粗**、"
    "不要 ```代码块```，QQ 里全都会显示成乱符号。"
)

# SD1.5 的 77 token 上限来自 CLIP 的 max_position_embeddings = 77，
# diffusers 直接按 77 截断、且【不报错】；而且是按 token 算，不是词数
# （英文大约 50~60 词就顶到）。这条约束散落在工具描述 / 菜单清单 /
# 中文拦截三处文案里，没有抽常量 —— 三处措辞本来就不一样（一处是给模型的
# 硬规则、一处是清单摘要、一处是拦截时的补救提示），抽出来反而更难读。

# ----------------------------------------------------------------------
#  搜索 / 抓取 / 总结 的进程内缓存
# ----------------------------------------------------------------------
# ★ 分三层缓存，键各不相同 —— 因为依赖的东西不一样：
#     s|<query>|<count>|<是否带摘要>        搜索结果
#     p|<url>|<fetch_max_chars>             抓到的整页（正文/标题/状态）
#     m|<正文hash>|<问题hash>|<max_chars>   总结
#   ★ 总结的键里【必须】带"用户这一问"：_summarize_page 本来就是按当前问题
#     提炼要点的，只按 url 缓存会让"换个问题问同一页"拿到上一问的要点 —— 那是错的。
#   ★ 只放内存，不落盘：重启即清空。缓存的是网页内容，没理由写进硬盘。
_CACHE: dict[str, tuple[float, str]] = {}
CACHE_MAX_ENTRIES = 100


def _norm_query(value: str) -> str:
    """规范化搜索词：折叠连续空白、去首尾、转小写（大小写不影响搜索结果）。"""
    return " ".join(str(value or "").split()).lower()


def _norm_url(value: str) -> str:
    """规范化网址：去首尾空白、去掉末尾多余的斜杠。"""
    url = str(value or "").strip()
    if len(url) > 8 and url.endswith("/"):
        url = url[:-1]
    return url




# 画幅三档（默认值按 12 GB 显卡定；可在插件设置里改 sd_max_side / sd_max_mp）：
# 警告线 = 上限 × 这个比例（默认 1440）。★ 不做成第三个配置项：



# 画幅预设（SDXL 原生约 1MP）。★ 用固定档位而不是让模型自由填 933×1217：






class NcdToolboxPlugin(star.Star):
    def __init__(
        self, context: star.Context, config: dict[str, Any] | None = None
    ) -> None:
        super().__init__(context, config)
        self.context = context
        # Star.__init__ 自己不保存 config，得我们存
        self._conf: dict[str, Any] = config or {}
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()
        # 会话 -> (打乱种子, 收藏集指纹, 已排好序的表情列表)
        self._sticker_orders: dict[str, tuple[str, str, list[dict]]] = {}
        # 待给模型看的图（umo -> [本地路径]）。模型调 view_avatar 后填，
        # 下一轮请求由 _tune_request 塞进 req.image_urls，然后立刻清空。
        self._pending_images: dict[str, list[str]] = {}
        # 工具菜单：哪些会话已经点开过菜单（umo -> 时间戳）。
        # 只有点开过的会话才看得到 ncd_tools 以外的工具，见 _apply_tool_menu。
        self._revealed: dict[str, float] = {}
        # 消息合并（同一个人 3 秒内连发几条 → 合成一条再交给模型）：
        #   key=(umo, 发送者ID) → {"texts": [...], "first": 时间戳, "notified": bool}
        self._merge_buf: dict[tuple[str, str], dict] = {}
        self._merge_lock = asyncio.Lock()
        # 沉浸 / 半沉浸互斥（详情见 _normalize_rp_mode）
        self._normalize_rp_mode()
        # 工具描述的"代码原文"快照。用户覆盖后还能一键还原，靠的就是它。
        self._orig_descriptions: dict[str, str] = {}
        # 最近几轮【真正发出去】的系统提示词（含本插件注入的部分）。
        # 只为让面板能摊开看，不参与任何逻辑。
        self._prompt_log: deque[dict[str, Any]] = deque(maxlen=5)


    # ------------------------------------------------------------------
    #  配置读取
    # ------------------------------------------------------------------

    def _conf_str(self, key: str, default: str = "") -> str:
        """取字符串配置，顺手去掉用户从资源管理器复制路径时带上的引号。"""
        raw = self._conf.get(key)
        if raw is None:
            return default
        text = str(raw).strip().strip('"').strip("'").strip()
        return text or default

    def _conf_int(self, key: str, default: int) -> int:
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(f"[toolbox] 配置 {key}={raw!r} 不是整数，改用默认值 {default}")
            return default

    def _conf_bool(self, key: str, default: bool) -> bool:
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")

    async def _web_notice(self, event: AstrMessageEvent, text: str) -> None:
        """搜索/抓正文前先往会话发一句进度，像出图那样。"""
        if not self._conf_bool("web_notify", True):
            return
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain(str(text))]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[toolbox] 进度提示发送失败（忽略）: {exc}")

    def _conf_float(self, key: str, default: float) -> float:
        """取浮点配置。

        ★ 和 _conf_int 一样：从 JSON 手改过的值可能是字符串，
          认不出来一律退回默认 —— 不能因为配置写歪了就把行为改掉。
        """
        raw = self._conf.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(f"[toolbox] 配置 {key}={raw!r} 不是数字，改用默认值 {default}")
            return default

    def _search_url(self) -> str:
        """搜索地址：显式填了 search_url 就用它，否则按 search_engine 预设。"""
        explicit = self._conf_str("search_url")
        if explicit:
            return explicit
        engine = self._conf_str("search_engine", "bing").lower()
        return SEARCH_PRESETS.get(engine, DEFAULT_SEARCH_URL)

    def _normalize_rp_mode(self) -> None:
        """沉浸模式与半沉浸模式【互斥】：两个都开着就关掉半沉浸并写回配置。

        ★ 为什么不在面板上做联动：AstrBot 的 _conf_schema 只有
          int/float/bool/string/text/list/file/object/template_list/dict，
          **没有下拉框/单选控件**，插件也收不到"用户点了哪个开关"的事件。
          所以只能在加载时纠正 + 落盘，重载后面板就一致了。
        ★ 判定优先级和 _rp_mode() 保持一致：沉浸 > 半沉浸 > 关。
        """
        conf = getattr(self, "_conf", None)
        if not isinstance(conf, dict):
            return
        if not (conf.get("rp_mode") and conf.get("rp_mode_half")):
            return
        logger.warning(
            "[toolbox] 沉浸模式与半沉浸模式同时开着 —— 沉浸模式优先，"
            "已自动关掉半沉浸并写回配置"
        )
        try:
            conf["rp_mode_half"] = False
            if hasattr(conf, "save_config"):
                conf.save_config({"rp_mode_half": False})
            logger.info("[toolbox] 半沉浸模式已关闭（配置已更新，重载后面板显示一致）")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 写回配置失败（本轮仍按沉浸模式跑）: {exc}")

    def _immersion_level(self) -> str:
        """沉浸档位：off / half / full。

        full（`rp_mode`，默认开）
            把那段工具说明【整段】拿掉。RP 最干净。
        half（`rp_mode_half`，默认关）
            **只拿掉语气分句，留下行为约束那条**（"never return an empty response" /
            "do not invent parameters"）。给小模型用。

            ★ 为什么需要这一档（实测动机）：切到 4B 之后，模型开始
              「只回一句台词、根本不真调用工具」——
                你说"你也发一张表情包" → 它回"好呀主人！(凑近你耳边小声说)"
              而整段剥掉的那句里恰好就有 "never return an empty response"，
              还有 "follow the tool schema exactly"。27B 不需要这两句也照做
              （同配置下一天 214 次工具调用），**4B 就必须靠它们撑着**。
              也就是说：沉浸模式的收益来自语气分句，代价落在行为约束上，
              而"代价"只在弱模型上暴露。
        两者都开时 **full 优先** —— 档位可预期，不做"按模型自动判断"的魔法。
        """
        if self._conf_bool("rp_mode", True):
            return "full"
        if self._conf_bool("rp_mode_half", False):
            return "half"
        return "off"

    # ------------------------------------------------------------------
    #  浏览器
    # ------------------------------------------------------------------

    async def _get_browser(self) -> Browser:
        async with self._browser_lock:
            if self._browser is None:
                self._browser = Browser(
                    edge_path=self._conf_str("edge_path") or find_edge(),
                    debug_port=self._conf_int("debug_port", 9222),
                    profile_dir=self._conf_str("profile_dir")
                    or default_profile_dir(),
                    headless=self._conf_bool("headless", False),
                    search_url=self._search_url(),
                    page_timeout=self._conf_int("page_timeout", 25),
                )
            return self._browser

    async def terminate(self) -> None:
        """插件卸载/重载时回收我们自己拉起的 Edge，避免留下孤儿进程。"""
        if self._browser is not None:
            self._browser.shutdown()
            self._browser = None

    # ------------------------------------------------------------------
    #  沉浸模式 + 自检
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    #  消息合并（同一个人连发几条 → 合成一条再回一次）
    # ------------------------------------------------------------------

    @filter.on_waiting_llm_request()
    async def _merge_waiting(self, event: AstrMessageEvent) -> None:
        """把同一用户 merge_window 秒内连发的消息合并成一条，只回一次。

        支持**连续图片**：图片/表情包不再被跳过，而是把后续消息里的
        Image 组件并进第一条事件，模型一次就能看到全部图片。
        纯文字、图文混发、多图连发都走同一套窗口。
        """
        try:
            if not self._conf_bool("merge_enabled", True):
                return
            text = str(getattr(event, "message_str", "") or "").strip()
            try:
                comps = list(event.get_messages() or [])
            except Exception:  # noqa: BLE001
                comps = []
            images = [c for c in comps if isinstance(c, Comp.Image)]
            # 既没文字也没图片（戳一戳、空消息）不参与合并
            if not text and not images:
                return
            # 指令不合并（带图也一样）
            if text.startswith("/"):
                return

            umo = str(getattr(event, "unified_msg_origin", "") or "")
            key = (umo, str(event.get_sender_id()))
            window = float(self._conf_int("merge_window", 3) or 3)

            if self._conf_bool("merge_debug", False):
                logger.info(
                    f"[merge-debug] umo={umo[-14:]} sender={key[1]} "
                    f"text={text[:30]!r} comps={[type(c).__name__ for c in comps]}"
                )

            notify = False
            async with self._merge_lock:
                st = self._merge_buf.get(key)
                if st is None:
                    st = {
                        "texts": [text] if text else [],
                        "images": list(images),
                        "first": time.time(),
                        "notified": False,
                    }
                    self._merge_buf[key] = st
                    leader = True
                else:
                    if text:
                        st["texts"].append(text)
                    st["images"].extend(images)
                    leader = False
                    if self._conf_bool("merge_notice", True) and not st["notified"]:
                        st["notified"] = True
                        notify = True

            if not leader:
                event.stop_event()
                logger.info(
                    f"[toolbox] 消息合并中（文字 {len(st['texts'])} 条 / 图 {len(st['images'])} 张），本条不单独回复"
                )
                if notify:
                    await self.context.send_message(
                        event.unified_msg_origin,
                        MessageChain(
                            chain=[
                                Comp.Plain(
                                    self._conf_str("merge_notice_text")
                                    or "正在排队，请稍等…（你连发的消息会合并成一条处理）"
                                )
                            ]
                        ),
                    )
                return

            wait = window - (time.time() - float(st["first"]))
            if wait > 0:
                await asyncio.sleep(wait)
            async with self._merge_lock:
                texts = list(st["texts"])
                images_all = list(st["images"])
                self._merge_buf.pop(key, None)

            limit = max(1, self._conf_int("merge_max_messages", 10))
            texts = texts[:limit]

            # ★ 领头事件自己已经带着自己的图；只把「别人」的图补进来
            own_ids = {id(c) for c in (event.get_messages() or [])}
            extra_images = [c for c in images_all if id(c) not in own_ids]
            if extra_images:
                try:
                    base = list(getattr(event.message_obj, "message", []) or [])
                    event.message_obj.message = base + extra_images
                    logger.info(f"[toolbox] 合并图片：追加 {len(extra_images)} 张")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[toolbox] 合并图片失败（保留文字合并）: {exc}")

            if len(texts) > 1:
                merged = "\n".join(texts)
                event.message_str = merged
                event.set_extra("ncd_merged_text", merged)
                logger.info(f"[toolbox] 已合并 {len(texts)} 条消息交给模型：{merged[:80]!r}")
            elif not texts and extra_images:
                # 纯图片连发：文字留空，让模型直接看图
                event.message_str = ""
                logger.info(f"[toolbox] 已合并 {len(extra_images) + 1} 张图片（无文字）")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[toolbox] 消息合并失败（按原样继续）: {exc}")

    @filter.on_llm_request()
    async def _tune_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """调整本轮请求：处理 AstrBot 注入的机械工具说明，并屏蔽自带工具。

        为什么能做：AstrBot 先 build_main_agent() 拼好 req（含那段工具说明），
        之后才触发 OnLLMRequestEvent（internal.py:352），所以这里改得到。

        AstrBot 注入的那段原文（astr_main_agent_resources.TOOL_CALL_PROMPT）：
            "When using tools: never return an empty response; briefly explain the
             purpose when starting a new type of task, but not before every tool call;
             follow the tool schema exactly and do not invent parameters; keep the
             conversation style consistent."

        ★ 它是【一句话里四个分句】，两类混在一起：
            行为约束类 → "never return an empty response"
                        "follow the tool schema exactly and do not invent parameters"
            机械语气类 → "briefly explain the purpose when starting a new type of task…"
                        "keep the conversation style consistent."
          语气类对角色扮演人格干扰明显；但行为约束类是【模型真干活的推力】，
          整段剥掉会把它们一起带走 —— 详见 _immersion_level()。
        """
        # 消息合并的兜底：waiting 阶段把合并文本写进了 event.message_str，
        # 正常情况 build 会照抄；这里再兜一次，防止别处在中间改了它。
        try:
            merged = event.get_extra("ncd_merged_text")
            if merged and str(getattr(req, "prompt", "") or "") != str(merged):
                req.prompt = str(merged)
                logger.info("[toolbox] 合并文本已回填到 req.prompt")
        except Exception:  # noqa: BLE001
            pass

        level = self._immersion_level()
        if level != "off":
            for text in _tool_prompt_variants():
                if not text or text not in req.system_prompt:
                    continue
                # full：整段拿掉（RP 最干净）；half：只拿掉语气分句，留下行为约束
                replacement = "" if level == "full" else _half_immersion_prompt(text)
                req.system_prompt = _collapse_blank_lines(
                    req.system_prompt.replace(text, replacement)
                )
                logger.info(
                    f"[toolbox] 沉浸模式({level})：工具说明段 "
                    f"{'已整段移除' if level == 'full' else '已裁成只留行为约束'}"
                )

            if req.func_tool:
                before = [tool.name for tool in req.func_tool.tools]
                for name in before:
                    if name not in TOOL_NAMES and name not in FOREIGN_KEEP:
                        req.func_tool.remove_tool(name)

        # 工具菜单：只留 ncd_tools 常驻，其余等模型点开才给（见 _apply_tool_menu）。
        # ★ 放在沉浸模式的过滤【之后】：两步都要跑，顺序不能反 ——
        #   先确定"本插件的工具"，再在这个范围内做菜单收窄。
        if self._conf_bool("tool_menu", True):
            self._apply_tool_menu(req, event)
            # 模型只知道有菜单还不够，得告诉它"这会什么"，否则不会想到去点。
            self._inject_menu_hint(req)
        else:
            # ★ 菜单关掉时，把 ncd_tools 也从本轮工具集里【摘掉】。
            #   不摘的话模型（尤其 DeepSeek）会看到"有个能列清单的工具"，
            #   于是先点它一下、再调真正的工具 —— 白花一次往返和几百 token，
            #   而且它列出来的清单里还有它自己，看着更乱。
            self._hide_menu_tool(req)
        # 画图工具已拆到独立插件 astrbot_plugin_ncd_draw（2026-10-19 整理）。
        self._attach_pending_images(req, event)
        # 工具描述的覆盖（不管菜单开没开都生效：用户可能只想改某句措辞）。
        self._apply_tool_descriptions(req)
        # 记下这一轮最终发出去的系统提示词（面板要能摊开看，见 _record_prompt）
        self._record_prompt(req, event)

        # 告诉模型这是群聊还是私聊（人设靠这个判断该不该收敛）。
        # ★ 注入到 extra_user_content_parts（用户消息后面），不是 system_prompt：
        #   跟 AstrBot 自己的 <system_reminder> 同一套做法，离当前这一轮更近；
        #   而且监控面板会把「注入块」单独画出来，一眼能确认它有没有生效。
        if self._conf_bool("session_header", False):
            try:
                if TextPart is None:
                    raise RuntimeError("拿不到 TextPart（AstrBot 结构变了？）")
                req.extra_user_content_parts.append(
                    TextPart(text=_session_header_text(event))
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[toolbox] 注入会话归属失败（不影响本轮）: {exc}")

        # 自检：每条请求记一行工具清单。
        # ★ 这行跑在【本插件过滤之后、AstrBot 清空工具之前】，
        #   所以它是"模型接下来会看到哪些工具"的近似值。
        #   真正决定能不能调用还看 provider 的 modalities 里有没有 tool_use
        #   （见 README「工具没被调用」一节）。
        #   预期：开了工具菜单时是 1/9（只有 ncd_tools），点开菜单后变 9/9。
        names = [tool.name for tool in req.func_tool.tools] if req.func_tool else []
        mine = [name for name in names if name in TOOL_NAMES]
        logger.info(
            f"[toolbox] 本轮请求工具 {len(names)} 个，本插件 {len(mine)}/{len(TOOL_NAMES)} 个"
            f"{' ' + str(names) if names else ' ← 一个工具都没有！'}"
        )

    # ------------------------------------------------------------------
    #  工具菜单（ncd_tools）
    # ------------------------------------------------------------------

    def _hide_menu_tool(self, req: Any) -> None:
        """菜单功能关掉时，把 ncd_tools 从本轮的请求里移除。

        ★ 为什么必须移除，而不是"留着但没人点"：只要它在工具集里，模型就有机会点。
          DeepSeek 实测会先调 ncd_tools 拿清单、再调真正的工具 —— 多一次往返、
          多几百 token，而且清单里还列着它自己，看着更糊涂。
        ★ 只动这一轮请求，不改注册状态：以后重新打开菜单，工具还在（不用重载）。
        ★ 全包 try：改别人的请求，出错绝不能挡对话。
        """
        try:
            if not req.func_tool:
                return
            names = [t.name for t in req.func_tool.tools]
            if MENU_TOOL in names:
                req.func_tool.remove_tool(MENU_TOOL)
                logger.info(
                    "[toolbox] 工具菜单已关闭，本轮把 ncd_tools 从工具集移除"
                    f"（剩 {len(names) - 1} 个）"
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 移除 ncd_tools 失败（不影响本轮）: {exc}")

    def _apply_tool_menu(self, req: Any, event: AstrMessageEvent) -> None:
        """把本插件除 ncd_tools 以外的工具收起来，直到这个会话点开过菜单。

        ★ 为什么这么做：6 个工具的 schema 每轮都塞进提示词，4B 在多个工具里
          挑一个会明显变笨（挑错、编工具名、只说不做）。收窄到 1 个入口，
          模型调一次拿到清单再动手，首轮负担从 6 个降到 1 个。
        ★ 为什么按会话记：群 A 点开过不代表群 B 要看到。不同会话的上下文
          互不相干，状态混在一起会让"我这次没让你用工具"变得不可控。
        ★ 全包 try：这是改别人的请求，出错绝不能挡对话。
        """
        try:
            if not req.func_tool:
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            opened = umo in self._revealed
            for name in [t.name for t in req.func_tool.tools]:
                if name not in TOOL_NAMES or name == MENU_TOOL:
                    continue
                if not opened:
                    req.func_tool.remove_tool(name)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 应用工具菜单失败（不影响本轮）: {exc}")

    # ---- 缓存辅助（搜索 / 抓取 / 总结共用）----
    def _cache_ttl(self) -> float:
        """缓存有效期（秒）。0 = 关闭缓存，每次都真去搜/抓。"""
        try:
            return float(self._conf_int("search_cache_ttl", 600) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _cache_get(self, key: str) -> str | None:
        """取缓存；过期或关闭时返回 None。命中会挪到队尾（LRU 语义）。"""
        ttl = self._cache_ttl()
        if ttl <= 0:
            return None
        hit = _CACHE.get(key)
        if hit is None:
            return None
        ts, value = hit
        if time.time() - ts > ttl:
            _CACHE.pop(key, None)
            return None
        _CACHE[key] = _CACHE.pop(key)  # 挪到队尾，淘汰时从队首开始
        return value

    def _cache_put(self, key: str, value: str) -> None:
        """写缓存；关闭时直接返回。超过上限就淘汰最久没用的那条。"""
        if self._cache_ttl() <= 0:
            return
        _CACHE[key] = (time.time(), value)
        while len(_CACHE) > CACHE_MAX_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)), None)

    def _tool_desc_overrides(self) -> dict[str, str]:
        """配置里的工具描述覆盖表（工具名 -> 文本）。

        ★ 配置里存的是 **JSON 字符串**，不是对象：AstrBot 的 schema 里
          type=object 会被当成"对象数组"，缺 items 字段会让插件直接加载失败
          （实测踩过 KeyError: 'items'，插件整个起不来）。
        ★ 同时兼容 dict：老配置/测试里直接塞字典也能用。
        """
        conf = getattr(self, "_conf", None) or {}
        if not isinstance(conf, dict):
            return {}
        raw = conf.get("tool_descriptions")
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("[toolbox] tool_descriptions 不是合法 JSON，按空处理")
                return {}
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        return {}

    def _apply_tool_descriptions(self, req: Any) -> None:
        """按配置覆盖「模型看到的工具描述」。

        ★ 为什么能改：`FunctionTool` 是 dataclass，`description` 是普通字段
          （实测可赋值）。注册表里的对象是全局共享的，所以这里【每次都按配置
          重设一遍】—— 用户把覆盖清空后，下一轮就自动回到代码里的原文。
        ★ 原文只在第一次见到该工具时记一次，之后都以它为基；不能拿"当前的
          description"当原文，否则覆盖过一次之后就再也回不去了。
        ★ 包 try：改别人的对象，出错不能让这一轮挂掉。
        """
        try:
            if not req.func_tool:
                return
            overrides = self._tool_desc_overrides()
            for tool in req.func_tool.tools:
                name = getattr(tool, "name", None)
                if name not in TOOL_NAMES:
                    continue
                original = self._orig_descriptions.get(name)
                if original is None:
                    original = getattr(tool, "description", "") or ""
                    self._orig_descriptions[name] = original
                want = (overrides.get(name) or "").strip() or original
                if getattr(tool, "description", None) != want:
                    tool.description = want
                    logger.info(
                        f"[toolbox] 工具 {name} 的描述已用配置覆盖"
                        if want != original
                        else f"[toolbox] 工具 {name} 的描述已还原为代码原文"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 覆盖工具描述失败（不影响本轮）: {exc}")

    def _record_prompt(self, req: Any, event: AstrMessageEvent) -> None:
        """记下这一轮【最终】要发给模型的系统提示词（含本插件注入的部分）。

        ★ 为什么需要：注入到底生效没有，以前只能靠日志猜 —— 面板上显示的是
          "你填的那段提示词"，不是"真正发出去的那段"。看起来就像没生效。
        ★ 只留最近 5 条，全文一起存（几 KB 而已），方便直接摊开对照。
        """
        try:
            sp_text = str(getattr(req, "system_prompt", None) or "")
            hint = self._menu_hint_text()
            self._prompt_log.appendleft(
                {
                    "ts": time.time(),
                    "umo": str(getattr(event, "unified_msg_origin", "") or ""),
                    "chars": len(sp_text),
                    "has_hint": bool(hint) and hint in sp_text,
                    "prompt": sp_text,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 记录系统提示词失败（不影响本轮）: {exc}")

    @filter.on_decorating_result()
    async def _plain_text_reply(self, event: AstrMessageEvent) -> None:
        """发送前把回复里的 Markdown 洗成纯文本（QQ 不渲染 Markdown）。

        ★ 为什么挂这个钩子：它跑在 result_decorate 阶段的【发送前】，
          而且改完 chain 之后框架会重新取一次结果
          （AstrBot 的 result_decorate/stage.py 里明确写了
          "插件可能直接对 chain 进行了替换"），所以就地改 Plain.text 生效。
        ★ 流式输出下这个钩子仍会跑，只是框架会提示"依赖发送前钩子的插件
          在流式下可能不对"。我们的用法是改文本，聚合发送时依然有效。
        ★ 全包 try：洗文本失败也绝不能挡住这条消息。
        """
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None) if result is not None else None
            if not chain:
                return
            changed = False
            for comp in chain:
                if isinstance(comp, Comp.Plain):
                    old = getattr(comp, "text", "") or ""
                    new = strip_markdown(old)
                    if new != old:
                        comp.text = new
                        changed = True
            if changed:
                logger.info("[toolbox] 出站回复已洗掉 Markdown 标记")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 清洗出站 Markdown 失败（不影响发送）: {exc}")

    def _menu_hint_text(self) -> str:
        """取能力提示：配置里有这个键就按配置（留空 = 不注入），没有就用默认。"""
        conf = getattr(self, "_conf", None) or {}
        if isinstance(conf, dict) and "tool_menu_hint" in conf:
            return str(conf.get("tool_menu_hint") or "").strip()
        return DEFAULT_MENU_HINT

    def _inject_menu_hint(self, req: Any) -> None:
        """把「你会什么」那段提示塞进系统提示词。

        ★ 为什么必须注入：开了菜单之后模型第一轮只看得到 ncd_tools 一个工具，
          它得先"知道自己会什么"才会想到去点。不告诉它，它就永远不会点。
        ★ 幂等：同一轮被处理两次时不再追加第二遍。
        """
        try:
            hint = self._menu_hint_text()
            if not hint:
                return
            sp_text = getattr(req, "system_prompt", None) or ""
            if hint in sp_text:
                return
            # ★ 必须先把原提示词尾部的空白收掉再拼：原文本很可能以换行结尾，
            #   直接 "\n\n" + hint 会变成三个换行，留下连续空行
            #   （测试-沉浸模式.py 就是靠这个判"剥离后不留空行"的）。
            #   收完再用 _collapse_blank_lines 兜一遍，和沉浸模式那条路保持同一口径。
            req.system_prompt = _collapse_blank_lines(
                sp_text.rstrip() + "\n\n" + hint
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 注入能力提示失败（不影响本轮）: {exc}")

    # ------------------------------------------------------------------
    #  工具 1/2：浏览器搜索与抓正文
    # ------------------------------------------------------------------

    def _web_backend(self) -> str:
        """deepseek（默认，官方接口） / browser（本机 Edge）。"""
        raw = (self._conf_str("web_backend") or "deepseek").strip().lower()
        return "browser" if raw in ("browser", "local", "edge", "bing") else "deepseek"

    def _deepseek_base(self) -> str:
        return self._conf_str("deepseek_api_base") or "https://api.deepseek.com"

    def _deepseek_key(self) -> str:
        key = self._conf_str("deepseek_api_key")
        if key:
            return key
        # ★ 2026-10-30：原来这里写死 get_provider_by_id("deepseek")，
        #   但 "deepseek" 是 provider_sources 的 id，真实 provider id 是
        #   "deepseek/deepseek-flash"，于是每次都刷一条
        #   "Provider deepseek was not found" 的警告（功能还能靠下面的文件回退救回来）。
        #   改成直接从 provider 实例表里按前缀找，找不到再看配置文件。
        try:
            pm = getattr(self.context, "provider_manager", None)
            inst = dict(getattr(pm, "inst_map", {}) or {})
            prov = next((v for k, v in inst.items() if str(k).startswith("deepseek/")), None)
            if prov is not None:
                cfg = dict(getattr(prov, "provider_config", {}) or {})
                raw = cfg.get("key")
                if isinstance(raw, (list, tuple)) and raw:
                    return str(raw[0])
                if isinstance(raw, str) and raw:
                    return raw
        except Exception:  # noqa: BLE001
            pass
        try:
            path = Path(__file__).resolve().parents[3] / "data" / "cmd_config.json"
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            for src in data.get("provider_sources") or []:
                if str(src.get("id") or "") == "deepseek":
                    raw = src.get("key")
                    if isinstance(raw, (list, tuple)) and raw:
                        return str(raw[0])
                    if isinstance(raw, str) and raw:
                        return raw
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _official_search(
        self, query: str, count: int, with_snippet: bool, cache_key: str,
        ori_question: str = "",
    ):
        """返回给模型的 JSON 字符串；None 表示应回退本地浏览器。"""
        key = self._deepseek_key()
        if not key:
            logger.warning("[toolbox] 官方搜索缺少 DeepSeek key")
            if self._conf_bool("web_fallback_browser", False):
                return None
            return json.dumps(
                {"error": "官方搜索缺少 DeepSeek API key（可在插件配置里填，或检查 deepseek 供应商）"},
                ensure_ascii=False,
            )
        t0 = time.time()
        try:
            items = await deepseek_web.search(
                self._deepseek_base(), key, query,
                count=count, ori_question=ori_question,
            )
        except Exception as exc:  # noqa: BLE001
            if not self._conf_bool("web_fallback_browser", False):
                return json.dumps(
                    {"error": f"官方搜索失败：{exc}（本地浏览器回退已关闭）"},
                    ensure_ascii=False,
                )
            logger.warning(f"[toolbox] 官方搜索失败，回退本机浏览器: {exc}")
            return None

        rows = []
        for i, item in enumerate(items[:count], 1):
            row = {
                "i": i,
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
            }
            if with_snippet:
                row["snippet"] = str(item.get("snippet") or "")
            rows.append(row)
        stats_add(search=1, search_ms=(time.time() - t0) * 1000, search_results=len(rows))
        payload = json.dumps(
            {
                "query": query,
                "engine": "deepseek",
                "results": rows,
                "note": (
                    "以上来自 DeepSeek 官方搜索。要看某一页正文，用 fetch_page(url=...)。"
                    "结果不对题就换更具体的关键词再搜。"
                ),
            },
            ensure_ascii=False,
        )
        self._cache_put(cache_key, payload)
        return payload

    async def _official_fetch(self, url: str, max_chars: int):
        key = self._deepseek_key()
        if not key:
            logger.warning("[toolbox] 官方抓取缺少 DeepSeek key")
            if self._conf_bool("web_fallback_browser", False):
                return None
            raise RuntimeError("官方抓取缺少 DeepSeek API key")
        got = await deepseek_web.fetch(self._deepseek_base(), key, url)
        text = str(got.get("content") or "")
        return {
            "url": url,
            "title": str(got.get("title") or ""),
            "text": text[:max_chars],
            "total_chars": len(text),
            "status": 200,
            "engine": "deepseek",
        }

    @filter.llm_tool(name="web_search_bing")
    async def web_search_bing(
        self, event: AstrMessageEvent, query: str, count: int = 8
    ) -> str:
        """搜索网页。默认走 DeepSeek 官方搜索（按次计费）。
        只有配置 web_fallback_browser 开启时，官方失败才会回退本机浏览器；
        该开关默认关闭，关闭时失败直接报错。
        只返回结果的序号、标题和网址，不返回正文。

        Args:
            query(string): 搜索关键词，用自然语言描述要找什么。
            count(number): 返回多少条结果，1 到 20，默认 8。
        """
        query = str(query or "").strip()
        if not query:
            return json.dumps(
                {"error": "搜索关键词是空的，请给出具体要查什么。"}, ensure_ascii=False
            )
        # ★ 清洗客套话：模型常把「帮我搜一下 xxx」原样当 query，
        #   这些词对搜索引擎毫无帮助，还会稀释关键词。
        cleaned = clean_query(query)
        if cleaned != query:
            logger.info(f"[toolbox] 搜索词清洗：{query!r} → {cleaned!r}")
        query = cleaned

        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 8
        count = max(1, min(MAX_SEARCH_COUNT, count))

        # 时间过滤（Bing 的 qft 参数），只对 Bing 生效；空/none = 不限。
        time_filter = (self._conf_str("search_time_filter") or "").strip().lower()
        if time_filter in ("none", "off", "0"):
            time_filter = ""

        # ★ 缓存键要包含所有影响结果的参数：关键词 + 条数 + 是否带摘要 + 时间过滤。
        #   少一个就会出现"换个条数/换个时间范围却拿到旧结果"。
        with_snippet = self._conf_bool("search_with_snippet", False)
        cache_key = (
            f"s|{_norm_query(query)}|{count}|{int(with_snippet)}|{time_filter}"
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            stats_add(cache_hit=1)
            logger.info(f"[toolbox] 搜索命中缓存：{query}")
            return cached

        await self._web_notice(event, "🔍 开始搜索了…")
        if self._web_backend() == "deepseek":
            official = await self._official_search(
                query, count, with_snippet, cache_key,
                ori_question=await self._event_text(event),
            )
            if official is not None:
                return official
            logger.info("[toolbox] 官方搜索回退到本地浏览器")

        try:
            browser = await self._get_browser()
            t0 = time.time()
            items = await browser.search(query, min(count, 10), time_filter=time_filter)
            # 一页只给 10 条；要更多就再抓第二页（默认 8 条，走不到这里）
            if count > 10:
                more = await browser.search(
                    query, min(count - len(items), 10), time_filter=time_filter, page=2
                )
                items = list(items) + list(more)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 搜索失败: {exc}", exc_info=True)
            return json.dumps(
                {
                    "error": f"搜索失败：{type(exc).__name__}: {exc}",
                    "hint": "浏览器可能没起来或本机没有 Edge；可以把失败原因告诉用户。",
                },
                ensure_ascii=False,
            )

        if not items:
            return json.dumps(
                {"query": query, "results": [], "note": "这个关键词没有搜到结果，换个说法再试。"},
                ensure_ascii=False,
            )

        # ★ 去重 + 限制单站点条数。Bing 第一页常被同一个大站刷满
        #   （知乎/CSDN/百家号），10 条里 6 条同源，看着像"搜到很多"其实一个来源。
        before = len(items)
        items = dedupe_results(items)[:count]
        stats_add(search=1, search_ms=(time.time() - t0) * 1000, search_results=len(items))
        if len(items) != before:
            logger.info(f"[toolbox] 搜索结果去重/限站：{before} → {len(items)} 条")

        # ★ 默认只回「序号 + 标题 + 网址」。
        #   实测带摘要时 8 条 = 617 token；只回标题网址 ≈ 250 token。
        #   而摘要本来就不足以回答具体问题，模型终究要抓页面 —— 那就把预算留给
        #   fetch_page（它会先总结再给，一份正文只花约 300 token）。
        #   （with_snippet 在上面算缓存键时就取过了）
        results = []
        for index, item in enumerate(items, 1):
            row: dict[str, Any] = {
                "i": index,
                "title": item.get("title") or "",
                "url": item.get("url") or "",
            }
            if with_snippet:
                row["snippet"] = item.get("snippet") or ""
            results.append(row)

        payload = json.dumps(
            {
                "query": query,
                "results": results,
                "note": (
                    "这里只有标题和网址。要看某一页的内容，"
                    "用 fetch_page(url=...) —— 它会先把正文总结成要点再给你，"
                    "一次只占很少上下文。\n"
                    "★ 如果这些结果都不对题，换更具体的关键词【再搜一次】（同一轮可以搜多轮），"
                    "别只凭标题猜答案。"
                    if not with_snippet
                    else "要看某一页的完整内容，用 fetch_page(url=...)。"
                    "结果不对题就换关键词再搜一次，别凭标题猜。"
                ),
            },
            ensure_ascii=False,
        )
        self._cache_put(cache_key, payload)
        return payload

    @filter.llm_tool(name="fetch_page")
    async def fetch_page(self, event: AstrMessageEvent, url: str, query: str = "") -> str:
        """打开一个网页，把正文总结成要点后返回。默认走 DeepSeek 官方抓取。
        只有配置 web_fallback_browser 开启时，官方失败才会回退本机浏览器；
        该开关默认关闭，关闭时失败直接报错。
        搜索结果里的标题看着相关就用它读内容。

        Args:
            url(string): 要打开的完整网址，必须以 http:// 或 https:// 开头。
            query(string): 你想从这一页找什么（关键词）。填了会只返回相关段落，
                比整页正文更准也更省；不填就用用户当前这句话里的关键词。
        """
        url = str(url or "").strip()
        guard_on = self._conf_bool("block_private_urls", True)
        # ★ 官方 fetch 是 DeepSeek 服务器去抓，本地 hosts/DNS 不该参与判定；
        #   只有走本机浏览器（或 PDF 本地解析）时才做私网检查。
        official_backend = self._web_backend() == "deepseek"
        is_pdf = url.split("?")[0].lower().endswith(".pdf")
        problem = (
            public_url_error(url)
            if guard_on and (not official_backend or is_pdf)
            else None
        )
        if problem:
            return json.dumps(
                {
                    "error": f"打不开这个网址：{problem}",
                    "hint": "换成公网网址再试。本机/内网地址被有意禁止"
                    "（插件配置里的 block_private_urls 可以关掉这个检查）。",
                },
                ensure_ascii=False,
            )

        try:
            fetch_chars = self._conf_int("fetch_max_chars", 4000)
            # ★ PDF 直接解析，别塞给浏览器渲染（渲染出来是阅读器界面，抽不到正文）。
            if url.split("?")[0].lower().endswith(".pdf"):
                await self._web_notice(event, "📄 开始读取 PDF…")
                return await self._read_pdf(url, fetch_chars)
            official = False
            page = None
            page_key = f"p|{_norm_url(url)}|{fetch_chars}"
            cached_page = self._cache_get(page_key)
            if cached_page is None:
                await self._web_notice(event, "📄 开始读取页面…")
            if cached_page is not None:
                stats_add(cache_hit=1)
                logger.info(f"[toolbox] 抓正文命中缓存：{url}")
                page = json.loads(cached_page)
            elif self._web_backend() == "deepseek":
                try:
                    page = await self._official_fetch(url, fetch_chars)
                    official = page is not None
                except Exception as exc:  # noqa: BLE001
                    if not self._conf_bool("web_fallback_browser", False):
                        return json.dumps(
                            {"url": url, "error": f"官方抓取失败：{exc}（本地浏览器回退已关闭）"},
                            ensure_ascii=False,
                        )
                    logger.warning(f"[toolbox] 官方抓取失败，回退本机浏览器: {exc}")
                    page = None

            if page is None:
                # ★ 官方没返回（无 key / 被跳过）时也不能绕过“关回退”的开关
                if not self._conf_bool("web_fallback_browser", False):
                    return json.dumps(
                        {
                            "url": url,
                            "error": "官方抓取不可用（未返回内容），且已关闭本地浏览器回退",
                        },
                        ensure_ascii=False,
                    )
                # 回退浏览器前再查一次（官方失败、本地抓的时候才需要）
                if guard_on:
                    landed_problem = public_url_error(url)
                    if landed_problem:
                        return json.dumps(
                            {"url": url, "error": f"本地浏览器打不开这个网址：{landed_problem}"},
                            ensure_ascii=False,
                        )
                browser = await self._get_browser()
                kws = keywords_of(query or await self._event_text(event))
                t0 = time.time()
                page = await browser.fetch_text(url, fetch_chars, kws)
                stats_add(fetch=1, fetch_ms=(time.time() - t0) * 1000,
                          fetch_chars=len(page.get("text") or ""))
            self._cache_put(page_key, json.dumps(page, ensure_ascii=False))
            if official:
                stats_add(fetch=1, fetch_chars=len(page.get("text") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 抓正文失败 {url}: {exc}", exc_info=True)
            return json.dumps(
                {"error": f"打开 {url} 失败：{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )

        # ★ 落地地址要再查一次：上面只验了「模型给的那个 URL」，
        #   而 302 跳转可以把它带到内网去（page["url"] 是导航结束后的真实地址）。
        landed_problem = (
            public_url_error(str(page.get("url") or ""))
            if guard_on and page.get("engine") != "deepseek"
            else None
        )
        if landed_problem:
            logger.warning(f"[toolbox] {url} 被重定向到不允许的地址，已中止读取")
            return json.dumps(
                {
                    "url": page.get("url"),
                    "error": f"这个网址最终跳转到了不允许的地址，已中止读取：{landed_problem}",
                },
                ensure_ascii=False,
            )

        if not page["text"]:
            # 错误页（403/404 等）也是"没有正文"，但要明确告诉模型这是页面问题，
            # 不是内容为空 —— 否则它会拿标题硬猜。
            status = page.get("status") or 0
            note = page.get("note") or "页面没有可读正文。"
            if status:
                note = f"该网址返回 HTTP {status}。{note}"
            return json.dumps(
                {
                    "url": page["url"],
                    "title": page["title"],
                    "engine": page.get("engine") or "browser",
                    "status": status,
                    "error": note,
                },
                ensure_ascii=False,
            )

        # ★ 先总结再给模型。
        #   实测一篇正文不总结是 2604 token（占 8192 的 32%），而且这些字会一直
        #   留在对话历史里；总结后约 300 token。短页面跳过总结，省掉一次模型调用。
        threshold = self._conf_int("summarize_threshold", 1200)
        if self._conf_bool("summarize_page", True) and len(page["text"]) >= threshold:
            summary = await self._summarize_page(event, page["text"])
            if summary:
                stats_add(summarize=1)
                return json.dumps(
                    {
                        "url": page["url"],
                        "title": page["title"],
                        "engine": page.get("engine") or "browser",
                        "status": page.get("status") or 0,
                        "summary": summary,
                        "summarized": True,
                        "source_chars": page["total_chars"],
                        "note": "以上是这一页的要点（已总结）。要引用原文细节请再抓一次具体页面。",
                    },
                    ensure_ascii=False,
                )
            logger.warning("[toolbox] 总结失败，退回截断原文")

        page.setdefault("engine", "browser")
        return json.dumps(page, ensure_ascii=False)

    async def _event_text(self, event: AstrMessageEvent) -> str:
        try:
            return (event.get_message_str() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    async def _read_pdf(self, url: str, max_chars: int) -> str:
        """PDF 走 pypdf，不进浏览器。"""
        import urllib.request

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = await asyncio.to_thread(
                lambda: urllib.request.urlopen(req, timeout=30).read(30 * 1024 * 1024)
            )
            tmp = Path(tempfile.gettempdir()) / f"ncd_pdf_{abs(hash(url)) % 10**10}.pdf"
            tmp.write_bytes(data)
            from pypdf import PdfReader

            reader = PdfReader(str(tmp))
            parts = [(p.extract_text() or "") for p in reader.pages[:8]]
            text = "\n".join(x for x in parts if x.strip())
            stats_add(fetch=1, fetch_chars=len(text))
            if not text.strip():
                return json.dumps(
                    {"url": url, "error": "这个 PDF 里抽不到文字（可能是扫描件/图片版）。"},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"url": url, "title": "", "engine": "pdf", "text": text[:max_chars],
                 "total_chars": len(text), "note": "PDF 直接解析的正文（前 8 页）。"},
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[toolbox] PDF 解析失败 {url}: {exc}")
            return json.dumps(
                {"url": url, "error": f"PDF 解析失败：{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )

    @filter.llm_tool(name="web_read_many")
    async def web_read_many(
        self,
        event: AstrMessageEvent,
        urls: str,
        query: str = "",
        per_page_chars: int = 1200,
    ) -> str:
        """一次并发读多个网页（最多 4 个），每个只取相关段落。

        比一条条 fetch_page 快得多：研究类问题先用 web_search_bing 拿到网址，
        再用这里把前 3 条一起读了，一轮就能比较。

        Args:
            urls(string): 网址列表，逗号或换行分隔，最多 4 个。
            query(string): 要重点找的内容（关键词）；不填就用用户当前这句话。
            per_page_chars(number): 每个页面最多返回多少字，默认 1200，上限 2500。
        """
        raw = re.split(r"[\s,，;；]+", str(urls or ""))
        cand = [u.strip() for u in raw if u.strip().startswith("http")]
        seen: list[str] = []
        for u in cand:
            if u not in seen:
                seen.append(u)
        seen = seen[:4]
        if not seen:
            return json.dumps({"error": "没有有效的 http(s) 网址。"}, ensure_ascii=False)

        # ★ 走官方 backend 时不能用本地 DNS 判断（hosts/代理会把域名指到 127.0.0.1）
        official_backend = self._web_backend() == "deepseek"
        guard_on = self._conf_bool("block_private_urls", True)
        if official_backend:
            good = list(seen)
        else:
            good = [u for u in seen if not (guard_on and public_url_error(u))]
        if not good:
            return json.dumps({"error": "这些网址都不允许访问（内网/本机地址）。"}, ensure_ascii=False)

        await self._web_notice(event, f"📄 开始读取 {len(good)} 个页面…")
        chars = max(400, min(2500, int(per_page_chars or 1200)))
        kws = keywords_of(query or await self._event_text(event))
        fallback = self._conf_bool("web_fallback_browser", False)
        sem = asyncio.Semaphore(3)

        async def one(u: str) -> dict:
            async with sem:
                page = None
                engine = "browser"
                if official_backend:
                    try:
                        page = await self._official_fetch(u, chars)
                        if page is not None:
                            engine = "deepseek"
                    except Exception as exc:  # noqa: BLE001
                        if not fallback:
                            return {"url": u, "error": f"官方抓取失败：{exc}"}
                        logger.warning(f"[toolbox] 批量读官方失败，回退浏览器: {exc}")
                        page = None
                if page is None:
                    if not fallback:
                        return {"url": u, "error": "官方抓取不可用，且已关闭本地浏览器回退"}
                    if guard_on:
                        problem = public_url_error(u)
                        if problem:
                            return {"url": u, "error": problem}
                    browser = await self._get_browser()
                    page = await browser.fetch_text(u, chars, kws)
                    engine = "browser"
                return {
                    "url": page.get("url") or u,
                    "title": page.get("title") or "",
                    "engine": engine,
                    "status": page.get("status") or 0,
                    "text": (page.get("text") or "")[:chars],
                    "total_chars": page.get("total_chars") or 0,
                }

        t0 = time.time()
        items = await asyncio.gather(*(one(u) for u in good))
        stats_add(read_many=1, fetch=len(good), fetch_ms=(time.time() - t0) * 1000)
        return json.dumps(
            {"query": query, "count": len(items), "elapsed_ms": int((time.time() - t0) * 1000),
             "results": items},
            ensure_ascii=False,
        )

    async def _summarize_page(self, event: AstrMessageEvent, text: str) -> str | None:
        """把网页正文总结成与「用户当前问题」相关的要点。失败返回 None（调用方退回原文）。"""
        if Message is None:
            logger.warning("[toolbox] 取不到 Message 类，跳过总结")
            return None

        limit = self._conf_int("summary_max_chars", 400)

        question = ""
        try:
            question = (event.get_message_str() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        if len(question) > 300:
            question = question[:300]

        # ★ 总结的键里【必须】带"用户这一问"：同一页面、不同问题，
        #   要点本来就不一样。只按 url 缓存会让"换个问题再问同一页"
        #   拿到上一问的要点 —— 那是错的，而且很难被发现。
        sum_key = f"m|{hash(text)}|{hash(question)}|{limit}"
        cached_summary = self._cache_get(sum_key)
        if cached_summary is not None:
            logger.info(f"[toolbox] 网页总结命中缓存（{len(text)} 字原文）")
            return cached_summary

        try:
            provider = await self.context.get_using_provider_async(
                event.unified_msg_origin
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[toolbox] 取当前 provider 失败，跳过总结: {exc}")
            return None
        if provider is None:
            logger.warning("[toolbox] 没有可用的对话模型，跳过总结")
            return None

        prompt = (
            f"{_INTERNAL_REQUEST_MARKER}\n"
            "下面是一个网页的正文。请只提取与【用户的问题】相关的事实，"
            f"用中文写成不超过 {limit} 字的要点。\n"
            "输出格式（硬性要求，违反就重写）：\n"
            "1) 第一行先给一句话结论，然后 3~6 条短句，每条只讲一件事；\n"
            "2) 纯文本。不要 Markdown：不要 ## 标题、不要 |表格|、"
            "不要 ** 加粗、不要 ```代码块```；\n"
            "3) 【不要写成对比表/矩阵】—— 即使正文在比较两个东西，也用连续短句说"
            "（「A 适合…；B 适合…」），不要按维度左右并排；\n"
            "4) 不要用「维度 / 对比项 / 表格」这类词，直接说事实。\n"
            "其它要求：不要客套开场；不要复述无关内容；不要编造正文里没有的信息；"
            "数字、型号、价格这类关键信息要原样保留。"
            "如果正文里确实没有与问题相关的内容，只回一句「网页里没有相关信息」。\n\n"
            f"【用户的问题】{question or '（没有取到，按通用要点提取）'}\n\n"
            f"【网页正文】\n{text}"
        )

        try:
            resp = await provider.text_chat(
                contexts=[Message(role="user", content=prompt)]
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 总结调用失败: {exc}")
            return None

        summary = (getattr(resp, "completion_text", "") or "").strip()
        if not summary:
            return None
        # 硬兜底：模型不听话时也不能让它把上下文吃回去
        if len(summary) > limit * 2:
            summary = summary[: limit * 2] + "…"
        logger.info(
            f"[toolbox] 网页总结：原文 {len(text)} 字 → 要点 {len(summary)} 字"
        )
        # ★ 总结也洗一遍 Markdown：这张表往往就是模型从网页总结里抄来的，
        #   在工具返回这一层就洗掉，模型转述时就拿不到 || 和 ## 了。
        summary = strip_markdown(summary)
        self._cache_put(sum_key, summary)
        return summary

    # ------------------------------------------------------------------
    #  工具 3/4/5：QQ 收藏表情
    # ------------------------------------------------------------------

    def _sticker_seed(self, event: AstrMessageEvent) -> str | None:
        """打乱的种子：一条用户消息 = 一个种子。

        为什么用 message_id 而不是「每次调用」：同一个序号要在三次调用之间
        保持指向同一张（list 看列表 → preview 看图 → send 发出去）。如果每次
        调用都重排，序号就失去意义 —— 模型预览的是 5 号，发出去的会是另一张。

        所以规则是：同一条消息内顺序锁死，换新消息才换一份新排列。返回值
        同时用于算缓存键，拿不到消息 ID 时返回 None（那就不打乱，宁可不换也不出错）。
        """
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if not message_id:
            return None
        return f"{getattr(event, 'unified_msg_origin', '') or ''}|{message_id}"

    async def _sticker_items(
        self, event: AstrMessageEvent, *, restart_scan: bool = False
    ) -> tuple[list[dict], bool]:
        """取收藏表情，按配置决定是否打乱顺序。返回 (列表, 这一份是不是打乱过的)。

        第二个返回值专供 list_stickers：只有真打乱了才该告诉模型「顺序是随机的」，
        否则等于告诉它序号不可靠，它反而不肯翻页、不肯复用序号。

        顺序规则（对应 _conf_schema.json 的 sticker_shuffle）：
          · 模型「重新打开」列表（list_stickers 从第 1 页看）→ 换一份新排列；
          · 除此之外一律沿用当前这份，包括：同一轮里翻页、换了消息后接着翻页，
            以及 preview_sticker / send_sticker。

        ★ 为什么翻页（哪怕跨了消息）也不能换：序号是三次调用之间唯一的凭据。
          一旦第 1 页和第 2 页来自两份排列，这两页会互相重叠又互相漏，
          模型记着的序号也会指向别的表情。
        """
        items = await fetch_all(event)
        if not items or not self._conf_bool("sticker_shuffle", False):
            return items, False

        seed = self._sticker_seed(event)
        if seed is None:
            return items, False

        umo = getattr(event, "unified_msg_origin", "") or ""
        fingerprint = collection_key(items)
        cached = self._sticker_orders.get(umo)

        if cached is not None and cached[1] == fingerprint:
            same_scan = cached[0] == seed
            if same_scan or not restart_scan:
                return cached[2], True

        ordered = shuffled(items, seed)
        # ★ 溢出时踢【最旧】的一份，绝不能 clear()：
        #   clear 会把别的会话正在翻的那一份也删掉，于是它下一次翻页
        #   拿到的是「新排列的第 N 页」—— 正是本模块最不想要的那种错位。
        #   dict 保插入序，所以先删再插 = 最近用过的排到队尾，队首就是最旧的。
        self._sticker_orders.pop(umo, None)
        while len(self._sticker_orders) >= MAX_STICKER_ORDERS:
            oldest = next(iter(self._sticker_orders), None)
            if oldest is None:
                break
            self._sticker_orders.pop(oldest, None)
        self._sticker_orders[umo] = (seed, fingerprint, ordered)
        logger.info(
            f"[toolbox] 收藏表情已打乱顺序：共 {len(ordered)} 张，"
            f"本会话本次排列的种子来自消息 {seed.rsplit('|', 1)[-1]}"
        )
        return ordered, True

    @filter.llm_tool(name="list_stickers")
    async def list_stickers(self, event: AstrMessageEvent, page: int = 1) -> str:
        """列出本 QQ 账号收藏的表情包（只给序号和文字备注，不给图片）。想发表情包时先调用它。

        收藏多的时候顺序是随机打乱的：每次重新从第 1 页查看都是新的一批，但翻页不会重排，序号一直有效。

        挑好之后自己决定发哪张，不要问用户要哪一张。

        Args:
            page(number): 第几页，从 1 开始，默认 1。每页条数见插件配置。
        """
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 1

        try:
            items, shuffled_order = await self._sticker_items(
                event, restart_scan=page <= 1
            )
            return render_list(
                items,
                page,
                self._conf_int("sticker_page_size", 10),
                shuffled=shuffled_order,
            )
        except StickerError as exc:
            return f"读取收藏表情失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 列收藏表情失败: {exc}", exc_info=True)
            return f"读取收藏表情失败：{type(exc).__name__}: {exc}"

    @filter.llm_tool(name="preview_sticker")
    async def preview_sticker(self, event: AstrMessageEvent, index: int) -> mcp.types.CallToolResult:
        """按序号查看一张收藏表情的实际图片，用于发送前确认。看完再决定是否 send_sticker。

        Args:
            index(number): list_stickers 给出的全局序号。
        """
        try:
            index = int(index)
        except (TypeError, ValueError):
            return _text_result(f"序号必须是整数，收到 {index!r}")

        try:
            items, _ = await self._sticker_items(event)
            item = find_by_index(items, index)
            jpeg = await load_preview(item, self._conf_int("sticker_max_side", 224))
        except StickerError as exc:
            return _text_result(f"预览失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 预览表情失败: {exc}", exc_info=True)
            return _text_result(f"预览失败：{type(exc).__name__}: {exc}")

        note = (item.get("desc") or "").strip() or "（这张没有备注）"
        return mcp.types.CallToolResult(
            content=[
                mcp.types.TextContent(
                    type="text",
                    text=(
                        f"这是序号 {index} 的收藏表情，备注：{note}。"
                        f"确认它符合当前语境后，调用 send_sticker(index={index}) 发出去；"
                        "不合适就换一个序号重新预览。不要问用户要哪张，自己决定。"
                    ),
                ),
                mcp.types.ImageContent(
                    type="image",
                    data=base64.b64encode(jpeg).decode(),
                    mimeType="image/jpeg",
                ),
            ]
        )

    @filter.llm_tool(name="send_sticker")
    async def send_sticker(self, event: AstrMessageEvent, index: int) -> str:
        """把指定序号的收藏表情发到当前会话。通常应该先用 preview_sticker 看过再发。

        Args:
            index(number): list_stickers 给出的全局序号。
        """
        try:
            index = int(index)
        except (TypeError, ValueError):
            return f"序号必须是整数，收到 {index!r}"

        try:
            items, _ = await self._sticker_items(event)
            item = find_by_index(items, index)
            return await send_sticker(event, item)
        except StickerError as exc:
            return f"发送失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 发送表情失败: {exc}", exc_info=True)
            return f"发送失败：{type(exc).__name__}: {exc}"











    # ---------- 底模（checkpoint）列表 ----------














    def _attach_pending_images(self, req: Any, event: AstrMessageEvent) -> None:
        """把 view_avatar 取到的图塞进本轮请求，让主模型自己看。

        ★ 只进请求，**不发送** —— 图不会出现在聊天里。
        ★ 用完立刻清空：同一会话的下一次请求不该再重复带这张图
          （否则每轮都塞一张，白花钱还污染上下文）。
        """
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            pending = self._pending_images.pop(umo, None)
            if not pending:
                return
            urls = list(getattr(req, "image_urls", None) or [])
            for path in pending:
                if path not in urls:
                    urls.append(path)
            req.image_urls = urls
            logger.info(
                f"[toolbox] 已把 {len(pending)} 张头像放进本轮请求"
                "（只给模型看，不发送）"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 挂载待看图失败（不影响本轮）: {exc}")

    def _avatar_target(
        self, who: str, event: AstrMessageEvent
    ) -> tuple[str, str, str, str]:
        """把 who 解析成 (类型, 号码, 称呼, 说明)。**永不抛异常。**

        ★ 为什么要这么宽容：模型看不到 QQ 号（上下文里没有），它只会填昵称或
          群名。日志里就出现过 who='老八说和做'（群名）被判非法、直接失败。
          现在的原则是——认不出的就回落到"当前说话的人"，并说明一句，
          总比整个工具失败好。

        类型：user / group。号码认不出来时返回空串（调用方会报错）。
        """
        text = str(who or "").strip()
        low = text.lower()
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        sender_name = str(getattr(event, "get_sender_name", lambda: "")() or "")

        # 1) 空 → 当前说话的人（最常用）
        if not text:
            return ("user", sender_id, "当前说话的人", "")

        # 2) 关键字
        if low in ("me", "self", "自己", "我", "你自己", "机器人"):
            return ("user", self_id, "你自己", "")
        if low in ("group", "群", "本群", "群头像", "群聊"):
            if group_id:
                return ("group", group_id, "当前群", "")
            # 私聊里没有群 —— 回落成说话人，别返回空号码让上层报错
            return ("user", sender_id, "当前说话的人", "这里是私聊，没有群头像可看")
        if low in ("at", "@", "他", "她", "对方", "刚才at的人", "被at的人"):
            at = self._first_at_qq(event)
            if at:
                return ("user", at[0], f"被 @ 的 {at[1] or at[0]}", "")
            return ("user", sender_id, "当前说话的人", "这条消息里没有 @ 到别人")

        # 3) 文本里带数字 → 当号码（去掉 @ / 全角）
        digits = re.sub(r"[^0-9]", "", text)
        if is_qq(digits):
            return ("user", digits, f"QQ {digits}", "")

        # 4) 昵称：先看 @ 提及，再看说话人/机器人自己的昵称
        for qq, name in self._at_list(event):
            if name and (name == text or text in name or name in text):
                return ("user", qq, f"被 @ 的 {name}", "")
        if sender_name and (sender_name == text or text in sender_name or sender_name in text):
            return ("user", sender_id, f"当前说话的人（{sender_name}）", "")

        # 5) 认不出：有 @ 就用第一个，否则当说话人
        at = self._first_at_qq(event)
        if at:
            return (
                "user",
                at[0],
                f"被 @ 的 {at[1] or at[0]}",
                f"没认出「{text}」，按这条消息里被 @ 的人处理",
            )
        return (
            "user",
            sender_id,
            "当前说话的人",
            f"没认出「{text}」，按正在跟你说话的人处理",
        )

    @staticmethod
    def _at_list(event: AstrMessageEvent) -> list[tuple[str, str]]:
        """消息里被 @ 的人：[(qq, 昵称)]。取不到就返回空列表。"""
        out: list[tuple[str, str]] = []
        try:
            for comp in event.get_messages() or []:
                if isinstance(comp, Comp.At):
                    qq = str(getattr(comp, "qq", "") or "")
                    if qq and qq != "all":
                        out.append((qq, str(getattr(comp, "name", "") or "")))
        except Exception:  # noqa: BLE001
            return []
        return out

    def _first_at_qq(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        ats = self._at_list(event)
        return ats[0] if ats else None

    @filter.llm_tool(name="recall_history")
    async def recall_history(self, event: AstrMessageEvent, count: int = DEFAULT_COUNT) -> str:
        """把更长的聊天历史调出来重看（上下文超感知）。

        平时你只看得到最近一小段；想回想"前面说过什么"时调这个。

        Args:
            count(number): 要回溯多少条，只能选 30 / 50 / 70 / 90，默认 30。
                填其它数字会就近折算（比如 40 → 30，80 → 90）。
                越大的档位越费上下文，只在真的需要往前翻时才用大的。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        want = ALLOWED_COUNTS[0] if DEFAULT_COUNT not in ALLOWED_COUNTS else DEFAULT_COUNT
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if cm is None:
                return "读历史失败：拿不到会话管理器。"
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return "读历史失败：这个会话还没有对话记录（先说几句话再试）。"
            conv = await cm.get_conversation(umo, cid)
            # ★ 真实对象里的字段是 history（**JSON 字符串**），不是 content ——
            #   _convert_conv_from_v2_to_v1 里写的是 history=json.dumps(...)。
            #   读 content 会永远拿到 None，表现成"这个会话还没有历史"。
            raw = getattr(conv, "history", None)
            if raw is None:
                raw = getattr(conv, "content", None)
            if isinstance(raw, str):
                try:
                    messages = json.loads(raw)
                except (TypeError, ValueError):
                    messages = []
            elif isinstance(raw, list):
                messages = raw
            else:
                messages = []
            text = format_transcript(messages, limit=count)
            logger.info(
                f"[toolbox] 超感知：回溯历史 count={count}，"
                f"原始 {len(messages)} 条，输出 {len(text)} 字"
            )
            return text
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 读历史异常: {exc}", exc_info=True)
            return f"读历史失败：{type(exc).__name__}: {exc}"
        finally:
            _ = want  # 仅用于类型提示，实际档位由 format_transcript 归一化

    @filter.llm_tool(name="view_avatar")
    async def view_avatar(self, event: AstrMessageEvent, who: str = "", refresh: bool = False) -> str:
        """看头像。**图只给你自己看，不会发给用户。**

        ★★ 你【也能看自己的形象】—— 填 "me" 就是你自己。
           用户问"你长什么样 / 你是不是猫娘 / 你头发什么颜色 / 发张自拍"
           这类关于你外貌的问题时，先看自己的头像再回答，
           别凭印象瞎编（人设和实际形象对不上最出戏）。
        ★ 其它该看的时机：用户问"我的头像怎么样"、有人换了头像、
          想知道刚才被 @ 的人长什么样。
        ★ 不该看的时候：跟长相无关的对话就别看，一次出图要花一次请求。

        用法：拿到结果后直接说出你看到了什么（用自己的话说，别念标签）。
        不要假装看过 —— 不调这个工具你是看不到头像的。

        ★ 大多数情况【不要填 who】：留空就是"当前说话的人"。
          你看不到别人的 QQ 号，别去猜号码或填昵称。
          看自己填 "me"；看群头像填 "group"。

        Args:
            refresh(bool): 强制重新拉取（忽略缓存）。用户说"我刚换头像"、
                或你觉得看到的不像最新的，就填 True。
            who(string): 看谁的头像，不确定就留空。
                · 留空 → 当前正在跟你说话的人（最常用）
                · "me" → 你自己（机器人）
                · "group" → 当前这个群
                · 想看在消息里被 @ 的那个人 → 填 "at" 或直接留空
                · 确实知道 QQ 号时，才填 5~12 位数字
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        kind, key, label, note = self._avatar_target(who, event)
        if not is_qq(key):
            return (
                f"看头像失败：没拿到有效的号码（{key!r}）。"
                "这一般是因为不在群里/私聊里。"
            )
        try:
            path, cached = await fetch_avatar(
                kind=kind,
                key=key,
                size=self._conf_int("avatar_size", 640),
                cache_dir=Path(ASTRBOT_ROOT) / "data" / "plugin_data" / "ncd_toolbox" / "avatars",
                ttl=0.0 if refresh else float(self._conf_int("avatar_cache_ttl", 600) or 0),
            )
        except AvatarError as exc:
            return f"看头像失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 取头像异常: {exc}", exc_info=True)
            return f"看头像失败：{type(exc).__name__}: {exc}"

        # 只排队给模型看，不发送
        bucket = self._pending_images.setdefault(umo, [])
        if str(path) not in bucket:
            bucket.append(str(path))
        del bucket[:-2]  # 一轮最多带 2 张，防止模型连点把上下文灌爆
        logger.info(
            f"[toolbox] 取到头像 {label}（{kind}={key}，"
            f"{'缓存命中' if cached else '新下载'}）→ 只给模型看"
        )
        return (
            f"已取到{label}的头像（{path.name}，"
            f"{'缓存命中' if cached else '刚下载'}，只给你自己看，不要发给用户）。"
            "图已经放进下一轮上下文了 —— 直接说你看到了什么就行。"
            + (f"（注意：{note}）" if note else "")
        )


    # ------------------------------------------------------------------
    #  工具菜单入口：模型先调这个，才看得到下面那些工具
    # ------------------------------------------------------------------

    # ★ name 这里写死字面量（不用 MENU_TOOL）：测试脚本用正则从源码里抓
    #   `name="..."` 来和 TOOL_NAMES 双向比对，写成变量会让那条护栏失效。
    #   一致性由 测试-工具菜单.py 断言 MENU_TOOL == "ncd_tools" 兜住。
    @filter.llm_tool(name="ncd_tools")
    async def ncd_tools(self, event: AstrMessageEvent) -> str:
        """查出我现在能用的外部工具，并把这些工具解锁到本会话。

        要搜东西 / 看网页 / 发表情包 / 画图 / 看头像之前先调用这个，
        拿到清单再按名字调用。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        already = umo in self._revealed
        self._revealed[umo] = time.time()
        # 会话别无限涨：超过 200 个就只留最近 100 个
        if len(self._revealed) > 200:
            keep = sorted(self._revealed.items(), key=lambda kv: kv[1])[-100:]
            self._revealed = dict(keep)

        # ★ 2026-10-17 实测：4B 会【连点两次菜单】（并行发两个一样的调用），
        #   然后拿着两份一样的长清单结束这一轮，一次具体工具都没调 ——
        #   用户发「画自画像」，结果既没画也没回。
        #   所以第二次起只回一句短提醒：省上下文，也把它往"下一步该调谁"推。
        if already:
            logger.warning(
                f"[toolbox] 工具菜单被【重复】点开（{umo or '未知会话'}）—— "
                f"模型没去调具体工具，只回一句提醒"
            )
            return (
                "工具已经解锁过了，别重复调用 ncd_tools。\n"
                "现在就按清单里的名字直接调用你要的那个工具，"
                "比如搜网页就是 web_search_bing(query, count)。"
            )

        logger.info(f"[toolbox] 工具菜单被点开：{umo or '（未知会话）'}")
        # ★ 清单由 TOOL_NAMES 驱动，不再手写 —— 2026-10-18 加 view_avatar 时，
        #   工具注册里有了、各模式也能看到，但这段手写清单没跟上，
        #   菜单模式下模型就完全不知道有"看头像"这个工具。
        lines = ["已解锁。接下来可以直接调用下面的工具（括号里是参数）："]
        for name in TOOL_NAMES:
            if name == MENU_TOOL:
                continue
            lines.append("· " + TOOL_SUMMARY.get(name, f"{name}（新增工具，参数见工具定义）"))
        lines += [
            "",
            "画图在另一个插件（链路出图 · ComfyUI）：让模型调 comfy_tools 进入，四个工作流 sdxl / krea2 / anima / moss。",
        ]
        return "\n".join(lines)





    # ------------------------------------------------------------------



    async def api_tools_info(self):
        """工具提示词页：当前生效的提示 + 全部工具的说明。

        ★ 工具说明从【运行时注册表】读，不是我手抄的常量 ——
          面板上看到的必须就是模型真正收到的那份，否则这个页面会骗人。
        ★ 注册表 `llm_tools` 是全局的，按 TOOL_NAMES 过滤出本插件的那些。
        """
        try:
            tools: list[dict[str, Any]] = []
            overrides = self._tool_desc_overrides()
            try:
                from astrbot.core.provider.register import llm_tools as _registry

                for t in _registry.func_list:
                    name = getattr(t, "name", None)
                    if name not in TOOL_NAMES:
                        continue
                    spec = getattr(t, "parameters", None) or {}
                    props = spec.get("properties") or {}
                    required = set(spec.get("required") or [])
                    name_str = str(name)
                    original = self._orig_descriptions.get(name_str)
                    if original is None:
                        original = (getattr(t, "description", "") or "").strip()
                        self._orig_descriptions[name_str] = original
                    override = (overrides.get(name_str) or "").strip()
                    tools.append(
                        {
                            "name": name,
                            "description": override or original,
                            "original": original,
                            "override": override,
                            "params": [
                                {
                                    "name": k,
                                    "type": (v or {}).get("type", ""),
                                    "required": k in required,
                                    "description": (v or {}).get("description", ""),
                                }
                                for k, v in props.items()
                            ],
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[toolbox] 读取工具说明失败（页面会只有提示词）: {exc}")

            # 菜单工具排最前，其余按名字
            tools.sort(key=lambda x: (x["name"] != MENU_TOOL, x["name"]))
            return json_response(
                {
                    "ok": True,
                    "hint": self._menu_hint_text(),
                    "default_hint": DEFAULT_MENU_HINT,
                    "tool_menu": self._conf_bool("tool_menu", True),
                    "tools": tools,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 工具提示词页 info 失败: {exc}", exc_info=True)
            return json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

    async def api_tools_save_hint(self):
        """保存能力提示。留空 = 不注入（由 _menu_hint_text 解释）。"""
        try:
            payload = await web_request.json(default={}) or {}
        except Exception as exc:  # noqa: BLE001
            return json_response(
                {"ok": False, "error": f"请求体不是 JSON: {exc}"}, status_code=400
            )
        if not isinstance(payload, dict):
            return json_response({"ok": False, "error": "请求体应是 JSON 对象"}, status_code=400)
        if "hint" not in payload:
            return json_response({"ok": False, "error": "缺少 hint 字段"}, status_code=400)

        hint = str(payload.get("hint") or "")
        try:
            conf = getattr(self, "_conf", None)
            # AstrBot 传进来的通常是 AstrBotConfig（dict 子类）→ 能直接落盘
            if hasattr(conf, "save_config"):
                conf.save_config({"tool_menu_hint": hint})
            # ★ 同步内存：提示是每轮从 self._conf 读的，改了立刻生效，
            #   不用重载插件（其余配置项仍然要重载，这里只有它特殊）
            if isinstance(conf, dict):
                conf["tool_menu_hint"] = hint
            elif conf is not None:
                try:
                    conf["tool_menu_hint"] = hint  # type: ignore[index]
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 保存工具提示词失败: {exc}", exc_info=True)
            return json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

        logger.info(
            f"[toolbox] 工具提示词已保存（{len(hint)} 字，"
            f"{'会注入' if hint.strip() else '留空=不注入'}）"
        )
        return json_response(
            {"ok": True, "hint": hint, "injected": bool(hint.strip()), "length": len(hint)}
        )

    async def api_tools_prompt(self):
        """给「工具提示词」页面：最近几轮真正发给模型的系统提示词（含注入）。"""
        try:
            hint = self._menu_hint_text()
            return json_response(
                {
                    "ok": True,
                    "hint": hint,
                    "menu_on": self._conf_bool("tool_menu", True),
                    "items": list(self._prompt_log),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

    async def api_tools_save_descriptions(self):
        """保存「每个工具的描述覆盖」。传空串/不传 = 该工具恢复代码里的原文。"""
        try:
            payload = await web_request.json(default={}) or {}
        except Exception as exc:  # noqa: BLE001
            return json_response(
                {"ok": False, "error": f"请求体不是 JSON: {exc}"}, status_code=400
            )
        if not isinstance(payload, dict):
            return json_response({"ok": False, "error": "请求体应是 JSON 对象"}, status_code=400)
        raw = payload.get("descriptions")
        if not isinstance(raw, dict):
            return json_response(
                {"ok": False, "error": "descriptions 必须是「工具名 -> 文本」的对象"},
                status_code=400,
            )

        # ★ 只收本插件的工具名：别的键一律丢掉，免得把无关配置写进这个字段。
        clean: dict[str, str] = {}
        for name, text in raw.items():
            if not isinstance(name, str) or name not in TOOL_NAMES:
                continue
            if isinstance(text, str) and text.strip():
                clean[name] = text.strip()

        try:
            conf = getattr(self, "_conf", None)
            # 存成 JSON 字符串（schema 的 type 是 string，见 _tool_desc_overrides 的说明）
            blob = json.dumps(clean, ensure_ascii=False)
            if hasattr(conf, "save_config"):
                conf.save_config({"tool_descriptions": blob})
            if conf is not None:
                try:
                    conf["tool_descriptions"] = blob
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 保存工具描述覆盖失败: {exc}", exc_info=True)
            return json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

        logger.info(
            f"[toolbox] 工具描述覆盖已保存：{len(clean)} 个被改，"
            f"其余 {len(TOOL_NAMES) - len(clean)} 个用代码原文"
        )
        return json_response(
            {"ok": True, "count": len(clean), "saved": clean, "total": len(TOOL_NAMES)}
        )




def _tool_prompt_variants() -> tuple[str, ...]:
    """取 AstrBot 会注入的那几段工具说明原文，用于从 system prompt 里摘掉它们。

    导入的是 AstrBot 内部模块。取不到时返回空元组 —— 沉浸模式静默失效，
    只影响「剥提示词」这一半功能，不影响工具本身和其它一切。
    """
    try:
        from astrbot.core.astr_main_agent_resources import (
            TOOL_CALL_PROMPT,
            TOOL_CALL_PROMPT_SKILLS_LIKE_MODE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[toolbox] 取不到 AstrBot 的工具说明常量（{type(exc).__name__}: {exc}），"
            "本轮只屏蔽自带工具，不剥提示词"
        )
        return ()
    return (TOOL_CALL_PROMPT, TOOL_CALL_PROMPT_SKILLS_LIKE_MODE)


def _collapse_blank_lines(text: str) -> str:
    """摘掉一段话后会留下连续空行，这里收拾干净（不改首尾，避免动到别的块）。"""
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


# 半沉浸模式：这些分句属于【机械语气】，剥掉它们不影响模型干活的意愿。
# ★ 判据用关键词而不是写死整句 —— AstrBot 改了措辞也不会整个失效；
#   认不出来的一律【保留】（保守：宁可留着语气，也不能把行为约束丢掉）。
# ★ 第二种措辞（SKILLS_LIKE 变体）里同一句写作
#   "Keep the role-play and style consistent throughout the conversation."，
#   所以单靠第一种的写法会漏 —— 实测就是这么发现的。
_STYLE_CLAUSE_HINTS = (
    "briefly explain the purpose",
    "provide a brief explanatory message",
    "keep the conversation style consistent",
    "keep the role-play and style consistent",
)


def _is_style_clause(clause: str) -> bool:
    """这个分句是不是纯语气（可以剥）。认不出来一律当【不是】。"""
    low = clause.lower()
    if any(h in low for h in _STYLE_CLAUSE_HINTS):
        return True
    # 兜底：任何「keep the … style … consistent」的写法都算语气
    return "keep the" in low and "style" in low and "consistent" in low


def _half_immersion_prompt(text: str) -> str:
    """把注入的工具说明裁成「只留行为约束」的版本（半沉浸模式用）。

    按分号/句号切子句，丢掉 _STYLE_CLAUSE_HINTS 命中的那些，其余原样保留。
    实测效果（AstrBot 当前那段）：
        "When using tools: never return an empty response; briefly explain the purpose
         when starting a new type of task, but not before every tool call; follow the
         tool schema exactly and do not invent parameters; keep the conversation style
         consistent."
      → "When using tools: never return an empty response; follow the tool schema
         exactly and do not invent parameters."
    """
    parts = re.split(r"(?<=[;.])", text)
    kept = [p for p in parts if not _is_style_clause(p)]
    return "".join(kept).strip()


def _session_header_text(event: AstrMessageEvent) -> str:
    """拼一句「你在群聊还是私聊、对面是谁」，注入给模型。

    ★ 为什么要【明说】（2026-10-14 实测）：人设里写「群聊不许 NSFW」不生效。
      查下来 AstrBot 只给了一行 `Group name: xxx` 藏在 `<system_reminder>` 里
      （astr_main_agent.py:911-920），要靠模型自己推理出
      「有群名 → 我在群里 → 那条禁令现在生效」。而人设是强烈的直接指令，
      这种间接线索基本必输 —— 不是模型笨，是判据太弱。
      所以这里把结论直接写出来。

    格式（跟 AstrBot 自己的 <system_reminder> 同一套写法）：
        群聊：<system_reminder>当前对话场景：【群聊中】（群名：X）（昵称：Y，QQ：Z）</system_reminder>
        私聊：<system_reminder>当前对话场景：【私聊中】（昵称：Y，QQ：Z）</system_reminder>

    ★ 每一项都独立降级：拿不到群名就不写群名，拿不到昵称就不写昵称 ——
      绝不能把 Python 的 None 注给模型（那比不写更糟）。
    """
    group_id = ""
    try:
        group_id = event.get_group_id() or ""
    except Exception:  # noqa: BLE001
        group_id = ""

    nick = ""
    uid = ""
    try:
        nick = event.get_sender_name() or ""
        uid = event.get_sender_id() or ""
    except Exception:  # noqa: BLE001
        pass

    if nick and uid:
        who = f"（昵称：{nick}，QQ：{uid}）"
    elif nick:
        who = f"（昵称：{nick}）"
    elif uid:
        who = f"（QQ：{uid}）"
    else:
        who = ""

    if group_id:
        group_name = ""
        try:
            group = getattr(event.message_obj, "group", None)
            if group is not None:
                group_name = getattr(group, "group_name", "") or ""
        except Exception:  # noqa: BLE001
            group_name = ""
        where = f"【群聊中】（群名：{group_name}）" if group_name else "【群聊中】"
    else:
        where = "【私聊中】"

    # ★ 2026-10-30：会话时间戳的时区坑，必须明说给模型。
    #   AstrBot 4.29 的 TimestampMixin 用 datetime.now(timezone.utc) 写 updated_at，
    #   而 4.28 时代遗留的数据是本地时间（CST）。两者混在同一列里，
    #   直接看会出现「明明刚聊过，时间却是 8 小时前」的错觉。
    #   不写清楚的话，模型会把 UTC 当本地时间去算「多久以前」，结论全错。
    tz_note = (
        "<system_reminder>时间戳说明：数据库里的会话时间按 UTC 存储，"
        "比本地时间（CST, UTC+8）早 8 小时；需要「现在几点」时请以注入的 "
        "Current datetime 为准，不要拿时间戳当本地时间推算。</system_reminder>"
    )
    return f"<system_reminder>当前对话场景：{where}{who}</system_reminder>{tz_note}"


def _text_result(text: str) -> mcp.types.CallToolResult:
    """把一句话包成工具结果，让 preview_sticker 的失败分支也能用统一返回类型。"""
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=text)]
    )





