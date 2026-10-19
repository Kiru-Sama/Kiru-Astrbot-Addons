"""链路工具箱：自建网页检索 + QQ 收藏表情发送 + 本机 SDXL 画图。

这几件事都刻意不走 AstrBot 的内置工具：
  · 内置的 web_search_* 全部依赖第三方付费 API key，且只回 URL + 摘要，抓不到正文；
  · 内置没有「以收藏表情样式发出动画表情」的能力（send_message_to_user 只能发普通图片/文件）；
  · 内置没有画图能力。

本插件改为：
  · 搜索/抓正文走你本机的 Edge —— 真浏览器，能渲染 JS、复用登录态、不被无头风控误伤；
  · 发表情走 QQ 账号自己的收藏表情夹；
  · 画图走独立的 diffusers 子进程（SDXL，见 sd_worker.py / sd_local.py），
  不依赖 ComfyUI —— 只要一个 python 环境和一个 .safetensors 底模。

暴露给模型的工具：
  web_search_bing   搜网页，返回标题/网址/摘要
  fetch_page        打开某个网址，取渲染后的正文
  list_stickers     列出收藏表情（分页，只给文字，不给图）
  preview_sticker   按序号取一张的缩略图给模型看（真·看图确认）
  send_sticker      按序号把收藏表情发到当前会话
  generate_image    用本机 SDXL 画图并发到当前会话

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
    from .lora_catalog import (
        SKIPPED as LORA_SKIPPED,
        LoraError,
        catalog_text,
        resolve as resolve_loras,
        scan as scan_loras,
    )
    from .browser import DEFAULT_SEARCH_URL, Browser, find_edge, public_url_error
    from .sd_local import SDBackendError, StandaloneClient
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
    from lora_catalog import (
        SKIPPED as LORA_SKIPPED,
        LoraError,
        catalog_text,
        resolve as resolve_loras,
        scan as scan_loras,
    )
    from browser import DEFAULT_SEARCH_URL, Browser, find_edge, public_url_error
    from sd_local import SDBackendError, StandaloneClient
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
TOOL_NAMES = (
    MENU_TOOL,
    "web_search_bing",
    "fetch_page",
    "list_stickers",
    "preview_sticker",
    "send_sticker",
    "draw_tools",
    "generate_image",
    "switch_model",
    "view_avatar",
    "recall_history",
)
"""本插件提供的全部工具名。

★ 沉浸 / 半沉浸模式会把【不在这个元组里】的工具从 req.func_tool 里移除。
  所以新增任何一个 `@filter.llm_tool` 都必须同步加到这里 ——
  否则它在沉浸模式下会被静默过滤掉，表现是"机器人死活不调这个工具"。
  `测试-工具注册.py` 会用源码里的装饰器名和这个元组做双向比对，漏一个就红。
"""

# 画图相关的两个工具（顺序也是"先画图、再换底模"的顺序）。
# ★ 2026-10-19 定案：它们**常驻**工具列表，不再藏进二级目录。
#   藏起来的那版实测会让模型从历史里记得名字、直接调用 → runner 只回
#   「未找到指定的工具，将跳过」→ 每次画图空转 1~2 轮。
#   现在 `draw_tools` 只是【可选的说明书】：长规范（提示词四条硬规则 + 画风/底模清单）
#   放在它返回的内容里，不画图的轮次不用读；想画图的人也可以完全不调它。
DRAW_TOOLS = ("generate_image", "switch_model")

# 每个工具给一行"说人话"的说明，给 ncd_tools 的清单用。
# ★ 必须覆盖 TOOL_NAMES 里的每一个（MENU_TOOL 除外）；测试会核对。
#   漏了也不会消失 —— 清单里会显示"（新增工具，参数见工具定义）"，
#   但那样模型容易填错参数，所以还是补上更好。
TOOL_SUMMARY: dict[str, str] = {
    "web_search_bing": "web_search_bing(query, count) —— 用本机浏览器搜网页，只给标题和网址",
    "fetch_page": "fetch_page(url) —— 打开一个网页，把正文总结成要点",
    "list_stickers": "list_stickers(page) —— 列出收藏表情包（只给序号和备注）",
    "preview_sticker": "preview_sticker(index) —— 看第 index 张表情的实际图片",
    "send_sticker": "send_sticker(index) —— 把第 index 张表情发到当前会话",
    "generate_image": "generate_image(prompt, style, reference, size) —— 按描述画一张图"
    "并直接发出来（size 可写 832x1216，或 竖/横/方，留空=配置默认）",
    "switch_model": "switch_model(model) —— 换画图用的底模（不带参数=看有哪些可选）；"
    "换完之后的出图都用新模型，要重新加载约十几秒",
    "draw_tools": "draw_tools() —— 画图说明书（可选）：给出提示词规则、可选画风、"
    "可选底模和当前出图参数。不调它也能直接调 generate_image",
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
    "画图（含参考图生图）、换画图底模、以及看某个人的头像（图只给你自己看，不会发给用户）。\n"
    "要查东西 / 想发表情包 / 想画图之前，先调用工具： ncd_tools "
    "拿到当轮可用的工具清单，再按清单里的名字去调用具体工具。不要凭印象编工具名。\n"
    "画图直接调 generate_image（参考图用 reference、画幅用 size）；"
    "想先看提示词规则和可选画风/底模清单，可以调 draw_tools（可选，不是前置步骤）。\n"
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


def looks_chinese(text: str) -> bool:
    """这段文字里有没有汉字 / 全角标点。

    用途：画图的 prompt 必须是英文（SD1.5 的文本编码器只认英文）。
    有汉字就【提醒模型自己翻译】再调用，而不是硬塞进去白等十几秒出一张废图。

    ★ 只判断"有没有"，不做翻译 —— 翻译要额外一次模型调用，
      而且 4B 翻出来的画面描述往往比自己写的还差。提醒它自己写更好。
    """
    for ch in text or "":
        code = ord(ch)
        if 0x3400 <= code <= 0x9FFF:      # CJK 统一表意文字 + 扩展 A
            return True
        if 0xF900 <= code <= 0xFAFF:      # 兼容表意文字
            return True
        if 0xFF00 <= code <= 0xFFEF:      # 全角标点（「，」「。」也是中文输入的痕迹）
            return True
    return False


# 画幅三档（默认值按 12 GB 显卡定；可在插件设置里改 sd_max_side / sd_max_mp）：
#   · 单边 ≤ 上限的 3/4（默认 1440）：直接画
#   · 单边在 (1440, 上限]：工具【先返回警告】，让模型去问用户；同意后带 force=True 才画
#   · 单边 > 上限，或总面积 > 上限面积（默认 1920×1440）：直接拒绝
#   —— 实测 1920×1920（3.69M 像素）在 12 GB 卡上就把显存逼到临界，
#     而 1920×1440（2.76M）是能稳住的最大画布。
SIZE_MIN_SIDE = 256
SIZE_MAX_SIDE = 1920
SIZE_MAX_PIXELS = 1920 * 1440
# 警告线 = 上限 × 这个比例（默认 1440）。★ 不做成第三个配置项：
# 三个数字互相打架只会让面板更难解释，比例是合理的不变量。
SIZE_WARN_RATIO = 0.75


def warn_side(max_side: int) -> int:
    """警告线：单边超过它就先问用户。默认 1920 → 1440。"""
    return max(SIZE_MIN_SIDE, int(int(max_side) * SIZE_WARN_RATIO))

# 画幅预设（SDXL 原生约 1MP）。★ 用固定档位而不是让模型自由填 933×1217：
# 非标准比例在 SDXL 上更容易构图崩（双头、断肢），而且慢。
SIZE_PRESETS: dict[str, tuple[int, int]] = {
    "竖": (832, 1216),
    "竖版": (832, 1216),
    "portrait": (832, 1216),
    "横": (1216, 832),
    "横版": (1216, 832),
    "landscape": (1216, 832),
    "方": (1024, 1024),
    "方形": (1024, 1024),
    "square": (1024, 1024),
    "宽": (1216, 832),
}


def check_canvas(
    width: int, height: int, max_side: int | None = None, max_pixels: int | None = None
) -> None:
    """画幅硬约束：单边 ≥256 且 ≤ max_side，总面积 ≤ max_pixels。不合法抛 ValueError。

    ★ 为什么按【面积】也卡一道：单边都在范围内，但 1920×1920 的面积是
      1920×1440 的 1.33 倍、1024² 的 3.5 倍 —— 12 GB 卡实测就会被逼爆。
      只卡单边挡不住这种情况。
    ★ 上限从配置来（sd_max_side / sd_max_mp），默认 1920 / 2.76 MP：
      换张 24 GB 的卡，用户自己把这两个调大就行，不用改代码。
    """
    side_cap = int(max_side or SIZE_MAX_SIDE)
    px_cap = int(max_pixels or SIZE_MAX_PIXELS)
    w, h = int(width), int(height)
    if not (SIZE_MIN_SIDE <= w <= side_cap and SIZE_MIN_SIDE <= h <= side_cap):
        raise ValueError(
            f"画幅 {w}×{h} 超出范围：任一边都要在 {SIZE_MIN_SIDE}~{side_cap} 之间"
        )
    if w * h > px_cap:
        raise ValueError(
            f"画幅 {w}×{h} 面积太大（{w * h / 1e6:.2f} MP）：上限是 {px_cap / 1e6:.2f} MP"
            f"（默认 1920×1440）—— 再大显存会爆。"
            f"想要更大的画面请用 1920x1440 / 1440x1920 / 1600x1200 这类比例，"
            f"或在插件设置里调大 sd_max_side / sd_max_mp。"
        )


def parse_size(
    text: str, max_side: int | None = None, max_pixels: int | None = None
) -> tuple[int, int] | None:
    """把画幅解析成 (宽, 高)：支持 "832x1216" / "832*1216" / "竖" / "横" / "方"。

    空串返回 None（= 用配置里的默认尺寸）。解析不出来抛 ValueError，
    由调用方转成给模型看的人话 —— 不要静默回落到默认，否则模型以为
    自己指定了画幅，实际画出来是另一个。

    ★ 这里只管【硬上限】。"单边超过 3/4 上限要先问用户"那条规则不在这里
      —— 它需要 force 参数，属于 generate_image 的事。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    key = raw.lower().replace(" ", "")
    if key in SIZE_PRESETS:
        return SIZE_PRESETS[key]
    m = re.fullmatch(r"(\d{2,4})\s*[x*×,，:]\s*(\d{2,4})", key)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        check_canvas(w, h, max_side, max_pixels)
        return (w // 8 * 8, h // 8 * 8)
    raise ValueError(
        f"看不懂的画幅「{raw}」。可以写 832x1216 这种（宽x高），"
        "或者直接写 竖 / 横 / 方。"
    )


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
        # 独立 SD 后端：懒建 + 按需拉起子进程（SDXL）
        self._sd: StandaloneClient | None = None
        # 待给模型看的图（umo -> [本地路径]）。模型调 view_avatar 后填，
        # 下一轮请求由 _tune_request 塞进 req.image_urls，然后立刻清空。
        self._pending_images: dict[str, list[str]] = {}
        # 最近一次生成的图（路径, 文件名）—— 给生图页面的「下载原图」用
        self._last_image: tuple[Path, str] | None = None
        # 工具菜单：哪些会话已经点开过菜单（umo -> 时间戳）。
        # 只有点开过的会话才看得到 ncd_tools 以外的工具，见 _apply_tool_menu。
        self._revealed: dict[str, float] = {}
        # 画图说明书被调过的会话（只用于"别重复要同一份长文案"）
        self._draw_revealed: dict[str, float] = {}
        # 出图串行锁：同一时刻只允许一条管线在跑（见 _render_image）
        self._render_lock = asyncio.Lock()
        # 沉浸 / 半沉浸互斥（详情见 _normalize_rp_mode）
        self._normalize_rp_mode()
        # 扫一遍画风目录，把结果写进配置项（面板可见）
        self._refresh_lora_status()
        # 工具描述的"代码原文"快照。用户覆盖后还能一键还原，靠的就是它。
        self._orig_descriptions: dict[str, str] = {}
        # 最近几轮【真正发出去】的系统提示词（含本插件注入的部分）。
        # 只为让面板能摊开看，不参与任何逻辑。
        self._prompt_log: deque[dict[str, Any]] = deque(maxlen=5)

        # ---- 生图页面用的 Web API（前端用 AstrBotPluginPage.apiGet/apiPost 调）----
        # 注意路由格式：/<插件目录名>/<自定义路径>
        try:
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-imggen/status",
                self.api_imggen_status,
                ["GET"],
                "生图页：后端类型、当前参数、worker 状态",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-imggen/generate",
                self.api_imggen_generate,
                ["POST"],
                "生图页：按参数生成一张图（返回 base64）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-imggen/progress",
                self.api_imggen_progress,
                ["GET"],
                "生图页：当前出图进度（步数）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/ncd-imggen/last",
                self.api_imggen_last,
                ["GET"],
                "生图页：最近一次生成的图（下载用）",
            )
            logger.info(
                f"[toolbox] 已注册生图页 API，路由前缀 /{PLUGIN_DIR_NAME}/ncd-imggen/"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 注册生图页 API 失败: {exc}")

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
        # 独立 SD worker 同理：它自己也会空闲超时退出，但重载时立刻收掉更干净
        if self._sd is not None:
            self._sd.stop()
            self._sd = None

    # ------------------------------------------------------------------
    #  沉浸模式 + 自检
    # ------------------------------------------------------------------

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
                    if name not in TOOL_NAMES:
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
        # ★ 2026-10-19 定案：画图工具【不再藏】。
        #   之前把 generate_image / switch_model 收进二级目录，结果是：模型从历史里
        #   记得这两个名字，直接调 → runner 只回一句「未找到指定的工具，将跳过」，
        #   模型拿不到可操作反馈，每次画图都要空转 1~2 轮（实测连续复现）。
        #   现在两个工具常驻，draw_tools 退化成【可选的说明书】：
        #   想先看提示词规则/画风清单就调它，不想看就直接调 generate_image。
        #   省 token 的目的仍达到 —— 长规范只在 draw_tools 里，不画图的轮次不用读。
        self._attach_pending_images(req, event)
        # 本地模型不给绘图工具（小模型提示词质量差）
        self._hide_image_tool_for_local(req, event)
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
        #   预期：开了工具菜单时是 1/7（只有 ncd_tools），点开菜单后变 7/7。
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
                # ★ 画风 / 底模清单挂到【二级菜单】上：它们只跟画图有关，
                #   挂在 generate_image 上会让普通聊天的提示词白白变长。
                if name == "draw_tools":
                    lora_hint = self._lora_catalog_hint()
                    if lora_hint and "【可选画风" not in want:
                        want = want + lora_hint
                    model_hint = self._model_catalog_hint()
                    if model_hint and "【可选底模" not in want:
                        want = want + model_hint
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

    @filter.llm_tool(name="web_search_bing")
    async def web_search_bing(
        self, event: AstrMessageEvent, query: str, count: int = 8
    ) -> str:
        """用本机浏览器在搜索引擎上查网页。只返回结果的序号、标题和网址，不返回正文。

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
            logger.info(f"[toolbox] 搜索命中缓存：{query}")
            return cached

        try:
            browser = await self._get_browser()
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
    async def fetch_page(self, event: AstrMessageEvent, url: str) -> str:
        """打开一个网页，把正文总结成要点后返回。搜索结果里的标题看着相关就用它读内容。

        Args:
            url(string): 要打开的完整网址，必须以 http:// 或 https:// 开头。
        """
        url = str(url or "").strip()
        guard_on = self._conf_bool("block_private_urls", True)
        problem = public_url_error(url) if guard_on else None
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
            browser = await self._get_browser()
            fetch_chars = self._conf_int("fetch_max_chars", 4000)
            # ★ 缓存键只含影响读取结果的参数：网址 + 字数上限。
            #   放在这里（而不是函数开头）：上面的内网地址检查必须每次都跑，
            #   被拒的地址不应该进缓存。
            page_key = f"p|{_norm_url(url)}|{fetch_chars}"
            cached_page = self._cache_get(page_key)
            if cached_page is not None:
                logger.info(f"[toolbox] 抓正文命中缓存：{url}")
                page = json.loads(cached_page)
            else:
                page = await browser.fetch_text(url, fetch_chars)
                self._cache_put(page_key, json.dumps(page, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 抓正文失败 {url}: {exc}", exc_info=True)
            return json.dumps(
                {"error": f"打开 {url} 失败：{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )

        # ★ 落地地址要再查一次：上面只验了「模型给的那个 URL」，
        #   而 302 跳转可以把它带到内网去（page["url"] 是导航结束后的真实地址）。
        landed_problem = (
            public_url_error(str(page.get("url") or "")) if guard_on else None
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
                {"url": page["url"], "title": page["title"], "status": status, "error": note},
                ensure_ascii=False,
            )

        # ★ 先总结再给模型。
        #   实测一篇正文不总结是 2604 token（占 8192 的 32%），而且这些字会一直
        #   留在对话历史里；总结后约 300 token。短页面跳过总结，省掉一次模型调用。
        threshold = self._conf_int("summarize_threshold", 1200)
        if self._conf_bool("summarize_page", True) and len(page["text"]) >= threshold:
            summary = await self._summarize_page(event, page["text"])
            if summary:
                return json.dumps(
                    {
                        "url": page["url"],
                        "title": page["title"],
                        "status": page.get("status") or 0,
                        "summary": summary,
                        "summarized": True,
                        "source_chars": page["total_chars"],
                        "note": "以上是这一页的要点（已总结）。要引用原文细节请再抓一次具体页面。",
                    },
                    ensure_ascii=False,
                )
            logger.warning("[toolbox] 总结失败，退回截断原文")

        return json.dumps(page, ensure_ascii=False)

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


    @staticmethod
    def _save_generated(data: bytes, filename: str, tag: str = "sd") -> Path:
        """把生成的图落到 AstrBot 的临时目录。

        ★ 用 AstrBot 的 temp 目录而不是自己建目录：那边有 TempDirCleaner
          定时清理，不用我们管回收，也不会把磁盘越占越多。
        """
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            base = Path(get_astrbot_temp_path())
        except Exception:  # noqa: BLE001
            base = Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "image.png"
        # ★ 前缀由调用方传（现在固定 sd_）：以前固定写 comfy_，
        #   后来独立后端出的图也叫 comfy_xxx.png，排查时误导。
        #   注意这里是 @staticmethod —— 不能读 self，所以前缀只能从参数进来
        #   （2026-10-18 实测踩过：在里面写 self 会 NameError，图画不出来）。
        path = base / f"{tag}_{uuid.uuid4().hex[:8]}_{safe}"
        path.write_bytes(data)
        return path

    def _discard_worker(self) -> None:
        """把当前 worker 判为脏：停掉并丢弃，下次出图重新拉起。

        ★ 用在「出图超时」之后：asyncio.wait_for 只取消了协程，
          asyncio.to_thread 里的线程停不下来、GPU 还在算。
          如果这时只放锁不管 worker，下一个请求会卡在 worker 的线程锁上，
          再被自己的 urlopen 超时打断 —— 连环报错；而且第一个请求的响应
          还会往已关闭的 socket 上写（BrokenPipe 噪音）。
        """
        try:
            if self._sd is not None:
                self._sd.stop()
                logger.warning("[toolbox] worker 被判为脏并停止（下次出图会重新拉起）")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 停止 worker 失败（忽略）: {exc}")
        finally:
            self._sd = None

    def _sd_client(self) -> StandaloneClient:
        """独立 SD 后端客户端（懒建）。

        ★ 不做常驻服务：worker 是【按需拉起的子进程】，空闲 sd_idle_timeout 秒
          自己退出把显存还回去。这里只记住句柄，每次用之前探活、挂了自动重拉。
        """
        if self._sd is None:
            self._sd = StandaloneClient(
                python=self._conf_str("sd_python"),
                checkpoint=self._conf_str("sd_checkpoint"),
                port=self._conf_int("sd_port", 8199),
                idle_timeout=float(self._conf_int("sd_idle_timeout", 300)),
                gen_timeout=float(self._conf_int("image_timeout", 70) or 70),
                # LoRA：留空 = 纯底模。只在 worker 启动时加载一次。
                lora=self._conf_str("sd_lora"),
                lora_scale=self._conf_float("sd_lora_scale", 0.8),
                # cuda / cpu。cpu 不吃显存（但慢一个数量级）。
                device=self._conf_str("sd_device") or "cuda",
                # auto=按文件大小猜；SDXL 融合模型 6~7 GB，SD1.5 约 2 GB
                arch=self._conf_str("sd_arch") or "auto",
                # auto=SDXL 开 offload（显存 7GB -> 3GB），SD1.5 关
                offload=self._conf_str("sd_offload") or "auto",
                # euler（欧拉+普通，默认）/ dpmpp2m_karras
                sampler=self._conf_str("sd_sampler") or "euler",
                # 单边像素上限（默认 1920）：worker 会把超出的尺寸夹到这个值
                max_side=self._conf_int("sd_max_side", SIZE_MAX_SIDE) or SIZE_MAX_SIDE,
                log_path=Path(ASTRBOT_ROOT)
                / "data"
                / "plugin_data"
                / "ncd_toolbox"
                / "sd_worker.log",
            )
        return self._sd

    async def _render_standalone(
        self, prompt: str, opts: dict | None = None
    ) -> tuple[bytes, str]:
        """独立后端文生图。

        ★ 画风与参考图都在【调用方】解析好、放进 opts 里传进来
          （`loras` / `image` / `strength`）。这里只负责拼 payload 交给 worker
          —— 早先误把 style / reference 的解析写进这里，导致
          `NameError: name 'style' is not defined`，而且参考图分支还会自己调自己。
        """
        opts = opts or {}
        client = self._sd_client()

        data, seed = await client.txt2img(
            {
                "prompt": prompt,
                # ★ 当前底模：worker 会自己比对，不一样就当场换（约十几秒）。
                #   带上它的好处是"谁也不需要记得上次换过没有"。
                "checkpoint": opts.get("checkpoint") or self._current_model(),
                # ★ 画风由调用方解析好放进 opts，worker 只负责装卸。
                "loras": opts.get("loras") or [],
                # ★ 负面词走配置（留空 = 用 worker 里的默认）。以前插件从不传，
                #   standalone 永远吃 worker 那份写死的默认，换模型也改不了。
                "negative": self._conf_str("sd_negative"),
                "sampler": self._conf_str("sd_sampler") or "euler",
                "width": int(opts.get("width") or self._conf_int("sd_width", 512) or 512),
                "height": int(opts.get("height") or self._conf_int("sd_height", 512) or 512),
                "steps": int(opts.get("steps") or self._conf_int("sd_steps", 25)),
                "cfg": float(opts.get("cfg") or self._conf_float("sd_cfg", 7.0)),
                "seed": opts.get("seed"),
            }
        )
        return data, f"sd_{seed}.png"

    async def _render_standalone_img2img(
        self, prompt: str, opts: dict
    ) -> tuple[bytes, str]:
        """参考图生图（只有独立后端支持）。

        ★ 分辨率不在这里给 —— img2img 的输出尺寸跟随参考图
          （diffusers 的 img2img `__call__` 没有 width/height，实测确认）。
        """
        client = self._sd_client()
        data, seed = await client.img2img(
            {
                "prompt": prompt,
                "checkpoint": opts.get("checkpoint") or self._current_model(),
                "sampler": self._conf_str("sd_sampler") or "euler",
                "negative": self._conf_str("sd_negative"),
                "image": opts["image"],
                # 画风也要传给 img2img：worker 那边两条路都会 apply_loras
                "loras": opts.get("loras") or [],
                "strength": float(opts.get("strength") or 0.6),
                "steps": int(opts.get("steps") or self._conf_int("sd_steps", 25)),
                "cfg": float(opts.get("cfg") or self._conf_float("sd_cfg", 7.0)),
                "seed": opts.get("seed"),
            }
        )
        return data, f"sd2i_{seed}.png"

    def _progress_text(
        self, info: dict, st: dict, now: float
    ) -> str | None:
        """看一次 /progress 快照，决定这一轮说什么。None = 这轮闭嘴。

        ★ 为什么把判定抽成纯函数：聊天的进度汇报有"静默期 / 去重 / 条数上限"
          三重规则，只看代码很难确认它对不对；抽出来以后可以用假时钟把
          每条规则都测到，不用真的睡 10 秒。
        ★ st 是跨轮次的状态（不在这里 sleep、不在这里发消息）。
        ★ 加载和出图【共用一个配额】（默认 5 条）：加载慢的时候不该把出图的
          份额也吃掉，反过来也一样 —— 用户要的是"整轮最多 5 条"，不是每段各 5 条。
        """
        limit = max(1, int(st.get("max_msgs") or 5))
        sent = int(st.get("sent") or 0)
        if sent >= limit:
            st["done"] = True
            return None

        def _bump(text: str) -> str:
            st["sent"] = sent + 1
            st["done"] = st["sent"] >= limit
            return text

        # —— 在出图：报步数 ——
        if info.get("busy"):
            step = int(info.get("step") or 0)
            total = int(info.get("total") or 0)
            full = int(info.get("steps_full") or total or 0)
            # ★ 图生图只重绘 steps×strength 步 —— 不写清楚，用户会问
            #   "我设的 25 步，怎么进度是 17 步？"
            if info.get("mode") == "img2img" and full and total and full != total:
                return _bump(
                    f"🎨 图生图 {step}/{total} 步"
                    f"（{full} 步 × 强度 {total / full:.2f}）"
                )
            return _bump(f"🎨 出图中… {step}/{total} 步" if total else "🎨 出图中…")

        # —— 还没就绪：模型在加载 ——
        if info.get("ready") is False:
            if st.get("load_from") is None:
                # 第一次看到"没就绪"，从此刻开始算静默期
                st["load_from"] = now
                return None
            # ★ 静默期：worker 刚拉起来那几秒是 `启动中 0%`（python 和 torch
            #   还在 import），这段时间报出去全是 0% —— 看着像卡死，纯噪音。
            if now - float(st["load_from"]) < float(st.get("grace") or 0):
                return None
            pct = int(info.get("phase_pct") or 0)
            if pct <= 0:
                # 百分比一点没动 = 还没进真正的"读权重"，同样报不出信息
                return None
            phase = str(info.get("phase") or "加载模型")
            key = f"{phase}|{pct}"
            if key == st.get("last_load"):
                return None  # 同一档进度不重复刷
            st["last_load"] = key
            return _bump(f"📦 {phase} {pct}%")

        # 就绪但还没开始出图：什么都别说，等它忙起来
        return None

    async def _chat_progress(self, event: AstrMessageEvent, max_msgs: int = 5) -> None:
        """出图期间往会话里报进度（加载 + 步数）。

        ★ 为什么是"发消息"而不是"进度条"：QQ 没有可编辑消息，也没有进度条。
          能做的是隔一段时间发一句「12/20 步」。
        ★ 为什么限条数：慢一点就发几十条会把群聊刷爆。默认整轮最多 5 条
          （加载 + 出图共用这个配额），间隔 sd_progress_interval 秒（默认 8）。
        ★ 为什么加载要等 10 秒（sd_load_grace）：worker 先起 HTTP 服务、
          再在后台线程加载模型，所以最前面那几秒能查到的是 `启动中 0%`。
          那时候就报，用户看到的是一串 0%，反而以为卡死了。
        ★ 出图结束/失败由调用方 cancel 掉这个任务。
        """
        interval = max(3, self._conf_int("sd_progress_interval", 8))
        grace = max(0, self._conf_int("sd_load_grace", 10))
        st: dict = {"max_msgs": max(1, int(max_msgs)), "grace": float(grace)}
        # 兜底上限：正常一张图几十秒，这里只是防止任务在异常路径上永生。
        deadline = time.time() + float(self._conf_int("image_timeout", 180) or 180) + 120.0
        try:
            while time.time() < deadline:
                await asyncio.sleep(interval)
                if self._sd is None:
                    return
                info = await self._sd.progress()
                text = self._progress_text(info, st, time.time())
                if text:
                    await self.context.send_message(
                        event.unified_msg_origin,
                        MessageChain(chain=[Comp.Plain(text=text)]),
                    )
                if st.get("done"):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[toolbox] 汇报出图进度失败（忽略）: {exc}")

    def _local_model_state(self, umo: str) -> tuple[bool, str]:
        """当前会话用的是不是【本地模型】。返回 (是否本地, 模型标识)。

        ★ 为什么要这个：本地小模型（4B 之类）写 SDXL 提示词的质量明显差 ——
          标签少、爱写无效质量词、还会照抄上一次。与其让它画出废图浪费显存，
          不如在本地模型下直接把绘图工具收起来，等换回云端模型再放开。

        ★ 判定依据：provider 的 id 里含 local_model_keywords 里的任一词
          （默认 lmstudio / ollama）。拿不到 provider 就当成"不是本地"，
          宁可不禁用，也别把绘图功能莫名其妙锁死。
        """
        try:
            prov = self.context.get_using_provider(umo)
            pid = ""
            conf = getattr(prov, "provider_config", None)
            if isinstance(conf, dict):
                pid = str(conf.get("id") or "")
            if not pid:
                meta = getattr(prov, "meta", None)
                if callable(meta):
                    pid = str(getattr(meta(), "id", "") or "")
            raw = str(self._conf_str("local_model_keywords") or "lmstudio,ollama")
            keys = [k.strip().lower() for k in raw.split(",") if k.strip()]
            low = pid.lower()
            return (any(k in low for k in keys), pid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[toolbox] 判断本地模型失败（按云端处理）: {exc}")
            return (False, "")

    def _hide_image_tool_for_local(self, req: Any, event: AstrMessageEvent) -> None:
        """本地模型时把 generate_image 从本轮工具集移除。"""
        try:
            if not self._conf_bool("disable_image_on_local", True):
                return
            if not req.func_tool:
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            is_local, pid = self._local_model_state(umo)
            if not is_local:
                return
            names = [t.name for t in req.func_tool.tools]
            if "generate_image" not in names:
                return
            req.func_tool.remove_tool("generate_image")
            logger.info(
                f"[toolbox] 当前是本地模型（{pid}），本轮移除 generate_image —— "
                "本地小模型写的提示词画出来质量差，换云端模型会自动恢复"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 本地模型禁用绘图失败（不影响本轮）: {exc}")

    # ---------- 底模（checkpoint）列表 ----------
    def _size_limits(self) -> tuple[int, int, float, int]:
        """画幅上限：(单边上限, 面积上限像素, 面积 MP, 警告线)。

        ★ 这三个数以前是写死的（1920 / 1920×1440 / 1440），其实是【12 GB 卡】
          的结论。换张 24 GB 的卡就该能画更大 —— 所以做成配置项：
          sd_max_side（单边上限）、sd_max_mp（面积上限，单位 MP）。
        ★ 警告线不单独配置，按上限的 3/4 算（默认 1920→1440）：
          三个数字互相打架只会让面板更难解释。
        """
        try:
            cap = int(self._conf_int("sd_max_side", SIZE_MAX_SIDE) or SIZE_MAX_SIDE)
        except (TypeError, ValueError):
            cap = SIZE_MAX_SIDE
        cap = max(SIZE_MIN_SIDE, cap)
        try:
            mp = float(self._conf_float("sd_max_mp", SIZE_MAX_PIXELS / 1e6) or 0)
        except (TypeError, ValueError):
            mp = SIZE_MAX_PIXELS / 1e6
        if mp <= 0:
            mp = SIZE_MAX_PIXELS / 1e6
        px = int(mp * 1_000_000)
        return cap, px, mp, warn_side(cap)

    def _model_dir(self) -> Path:
        """底模目录：配置优先，否则用 sd_checkpoint 所在目录。

        ★ 为什么不把路径写死：这是要给别人用的插件。写死"我的桌面"换台机器就废。
          留空时跟着 sd_checkpoint 走 —— 你换底模目录只要改那一项。
        """
        raw = str(self._conf_str("sd_model_dir") or "").strip()
        if raw:
            return Path(raw)
        ckpt = str(self._conf_str("sd_checkpoint") or "").strip()
        if ckpt:
            return Path(ckpt).parent
        return Path(ASTRBOT_ROOT) / "data" / "plugin_data" / "ncd_toolbox" / "models"

    def _model_list(self) -> list[dict]:
        """扫目录里能当底模用的文件。

        ★ 靠【文件大小】过滤：LoRA 和底模经常被丢在同一个目录里
          （这台机器上就是），而 <sd_model_min_gb>（默认 3 GB）能把
          几十~几百 MB 的 LoRA 全部排除掉，不用维护黑白名单。
        ★ 不缓存：这个目录一般就几个文件，stat 一遍比维护失效逻辑便宜。
        """
        directory = self._model_dir()
        try:
            min_bytes = float(self._conf_float("sd_model_min_gb", 3.0) or 3.0) * 1024**3
        except (TypeError, ValueError):
            min_bytes = 3.0 * 1024**3
        current = os.path.abspath(self._current_model()) if self._current_model() else ""
        out: list[dict] = []
        try:
            files = [p for p in directory.iterdir() if p.is_file()]
        except Exception:  # noqa: BLE001
            return out
        for path in files:
            if path.suffix.lower() not in (".safetensors", ".ckpt"):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < min_bytes:
                continue
            out.append(
                {
                    "name": path.stem,
                    "file": path.name,
                    "path": str(path),
                    "size_mb": round(size / 1024**2, 1),
                    "current": os.path.abspath(str(path)) == current,
                }
            )
        out.sort(key=lambda x: x["name"].lower())
        return out

    def _current_model(self) -> str:
        """现在用哪个底模：换过就用换过的，否则用配置里的默认。"""
        path = getattr(self, "_model_override", None)
        if path:
            return str(path)
        return str(self._conf_str("sd_checkpoint") or "").strip()

    def _resolve_model(self, want: str) -> str:
        """底模名字 → 绝对路径。

        接受：绝对路径 / 文件名 / 不带扩展名的名字 / 唯一的一段子串（如 "noob"）。
        全都不区分大小写。★ 匹配不到就报错【并列出可选值】——
        不猜，猜错了要白等十几秒加载一个用户根本没选的模型。
        """
        want = str(want or "").strip()
        if not want:
            raise ModelError("model 是空的")
        if os.path.isfile(want):
            return os.path.abspath(want)

        models = self._model_list()
        if not models:
            raise ModelError(
                f"底模目录里没有可用模型（目录：{self._model_dir()}；"
                f"小于 {self._conf_float('sd_model_min_gb', 3.0)} GB 的文件会被当成 LoRA 跳过）"
            )
        low = want.lower()
        for item in models:
            if low in (item["name"].lower(), item["file"].lower()):
                return item["path"]
        # 子串匹配：太短的名字（"v1" 之类）容易同时命中好几个，只对 ≥3 个字符生效
        if len(low) >= 3:
            hits = [m for m in models if low in m["name"].lower()]
            if len(hits) == 1:
                return hits[0]["path"]
            if len(hits) > 1:
                raise ModelError(
                    f"「{want}」同时匹配到 {len(hits)} 个底模，请写得更具体一些："
                    + "、".join(m["name"] for m in hits)
                )
        raise ModelError(
            f"没有叫「{want}」的底模。可选的：" + "、".join(m["name"] for m in models)
        )

    def _model_catalog_hint(self) -> str:
        """给 switch_model 的工具描述追加一段"有哪些底模可用"。"""
        try:
            models = self._model_list()
            if not models:
                return f"\n\n【可选底模】\n（{self._model_dir()} 里没有可用模型）"
            lines = []
            for m in models:
                mark = "（当前）" if m.get("current") else ""
                lines.append(f"· {m['name']}{mark}（{round(m['size_mb'] / 1024, 2)} GB）")
            return (
                "\n\n【可选底模（填在 model 参数里，大小写不敏感，可只写一段子串）】\n"
                + "\n".join(lines)
                + "\n换底模要重新加载权重（约 10~20 秒），换完对之后的每一次出图都生效。"
                "\n只有用户明确说要换模型/换底模/用另一个模型画时才调；不确定有哪些就"
                "先不带参数调一次看看清单。"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 生成底模清单失败（不影响本轮）: {exc}")
            return ""

    def _lora_dir(self) -> Path:
        """画风目录：配置优先，否则用「底模同级的 loras/」——换机器不用改配置。"""
        raw = str(self._conf_str("sd_lora_dir") or "").strip()
        if raw:
            return Path(raw)
        ckpt = str(self._conf_str("sd_checkpoint") or "").strip()
        if ckpt:
            return Path(ckpt).parent / "loras"
        return Path(ASTRBOT_ROOT) / "data" / "plugin_data" / "ncd_toolbox" / "loras"

    def _lora_list(self) -> list:
        """扫目录（按 mtime+大小做轻缓存，不必每轮都读文件头）。"""
        directory = self._lora_dir()
        try:
            sig = tuple(
                sorted((p.name, p.stat().st_size) for p in directory.iterdir() if p.is_file())
            )
        except Exception:  # noqa: BLE001
            sig = ()
        cache = getattr(self, "_lora_cache", None)
        if cache and cache[0] == sig:
            return cache[1]
        loras = scan_loras(directory)
        skipped = list(LORA_SKIPPED)
        self._lora_cache = (sig, loras)
        if loras:
            logger.info(
                f"[toolbox] 画风目录 {directory}：可用 {len(loras)} 个"
                + (f"，跳过 {len(skipped)} 个（{skipped[0][1][:40]}…）" if skipped else "")
            )
        return loras

    def _lora_catalog_hint(self) -> str:
        """给工具描述追加的一段可用画风清单。"""
        try:
            loras = self._lora_list()
            if not loras:
                return ""
            allow = self._conf_bool("sd_allow_nsfw_lora", True)
            body = catalog_text(loras, allow)
            return (
                "\n\n【可选画风（填在 style 参数里）】\n"
                + body
                + "\n可以叠加，用逗号分隔，每个可加强度如 cunnyfunky:0.7（默认 0.8）。"
                "\n留空 = 不用任何画风。只在用户明确提到画风/画师/风格时才选。"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 生成画风清单失败（不影响本轮）: {exc}")
            return ""

    def _refresh_lora_status(self) -> str:
        """扫一遍画风目录，把结果写进配置项 sd_lora_status（面板上能直接看到）。

        ★ 为什么写进配置：AstrBot 的配置面板只能渲染 schema 里声明过的字段，
          没法给插件塞一个"动态列表"。所以用"插件自己维护一个只读文本字段"
          的办法 —— 你在面板里看到的永远是上次加载/重扫时的真实情况。

        ★ 只写不算：内容没变就不落盘，避免每次重载都改配置文件。
        """
        try:
            loras = self._lora_list()
            skipped = list(LORA_SKIPPED)
            if loras:
                head = "可用 %d 个：" % len(loras) + "、".join(
                    f"{lo.name}（{lo.rating}，{lo.size_mb:.0f}MB）" for lo in loras
                )
            else:
                head = f"目录 {self._lora_dir()} 里没有可用的画风"
            lines = [head, f"目录：{self._lora_dir()}"]
            if skipped:
                lines.append(
                    "已跳过 %d 个：" % len(skipped)
                    + "；".join(f"{n}（{why}）" for n, why in skipped)
                )
            text = "\n".join(lines)
            conf = getattr(self, "_conf", None)
            if isinstance(conf, dict) and conf.get("sd_lora_status") != text:
                conf["sd_lora_status"] = text
                if hasattr(conf, "save_config"):
                    conf.save_config({"sd_lora_status": text})
            return text
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 刷新画风状态失败: {exc}")
            return ""

    def _message_image_bytes(self, event: AstrMessageEvent) -> bytes | None:
        """取当前消息里第一张图的内容（给参考图生图用）。取不到返回 None。"""
        try:
            for comp in event.get_messages() or []:
                if not isinstance(comp, Comp.Image):
                    continue
                path = getattr(comp, "path", None) or getattr(comp, "file", None)
                if path and Path(str(path)).is_file():
                    data = Path(str(path)).read_bytes()
                    if data:
                        return data[:12 * 1024 * 1024]
                getter = getattr(comp, "convert_to_file_path", None)
                if callable(getter):
                    # 异步接口，这里拿不到 —— 交给 _reference_bytes 在外层 await
                    return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[toolbox] 读消息里的图失败: {exc}")
        return None

    async def _reference_bytes(self, reference: str, event: AstrMessageEvent) -> bytes | None:
        """把 reference 参数解析成图片字节。

        · ""（留空）→ 不用参考图
        · "message" / "msg" / "图" → 当前消息里附的图
        · "last" / "上一张" → 机器人最近一次生成的那张
        ★ 认不出来就返回 None（当普通文生图），不报错 —— 参考图是加分项，
          不该因为它把整次出图弄失败。
        """
        ref = str(reference or "").strip().lower()
        if not ref or ref in ("none", "off", "无", "不用"):
            return None

        if ref in ("last", "previous", "上一张", "刚才那张", "上一张图"):
            last = getattr(self, "_last_image", None)
            if last and Path(last[0]).is_file():
                return Path(last[0]).read_bytes()
            logger.info("[toolbox] 要求用上一张做参考图，但没有上一张")
            return None

        if ref in ("message", "msg", "image", "图", "消息图", "这张"):
            data = self._message_image_bytes(event)
            if data:
                return data
            # 兜底：有些平台图没有 path，只能异步转
            try:
                for comp in event.get_messages() or []:
                    if isinstance(comp, Comp.Image):
                        getter = getattr(comp, "convert_to_file_path", None)
                        if callable(getter):
                            p = await getter()
                            if p and Path(str(p)).is_file():
                                return Path(str(p)).read_bytes()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[toolbox] 异步取图失败: {exc}")
            logger.info("[toolbox] 要求用消息里的图做参考图，但这条消息没有图")
            return None

        logger.info(f"[toolbox] reference={reference!r} 不认识，按不用参考图处理")
        return None

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
    async def view_avatar(self, event: AstrMessageEvent, who: str = "") -> str:
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
                ttl=float(self._conf_int("avatar_cache_ttl", 21600) or 0),
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

    async def _render_image(
        self, prompt: str, opts: dict | None = None
    ) -> tuple[bytes, str]:
        """出图（带总超时）。

        ★ 70 秒是【一次出图的总时长上限】，含 worker 冷启动。
          为什么不是 40：实测冷启动到过 31.9 秒，再加 5~6 秒推理就 38 秒，
          40 秒很容易把正常请求误杀。宁可宽一点，也别把能成的图拦掉。
        ★ opts 里有 `image` 就是参考图生图，只支持独立后端。
        """
        opts = opts or {}
        timeout = float(self._conf_int("image_timeout", 70) or 70)
        # ★ 纯 CPU 出图比 GPU 慢一个数量级，70 秒根本不够。
        #   这里自动放宽到至少 300 秒 —— 只放宽不缩短，用户配得更大就听用户的。
        if (
            (self._conf_str("sd_device") or "cuda").lower() == "cpu"
        ):
            timeout = max(timeout, 300.0)
        # ★ 这里【不再】为 SDXL 自动放宽超时：那样会把 image_timeout 变成一个
        #   "写了也没用"的配置（实测把测试里 1 秒超时的那条弄坏了）。
        #   SDXL 的耗时请通过 image_timeout 自己配（默认值已按 SDXL 调到 180）。
        if opts.get("image"):
            coro = self._render_standalone_img2img(prompt, opts)
        else:
            coro = self._render_standalone(prompt, opts)
        # ★ 同一时刻只允许【一条】出图管线在跑（聊天 + 生图页可能同时点）。
        #   worker 内部本来也有锁，但两条请求同时进来会让进度条互相覆盖、
        #   超时互相打架，更重要的是显存峰值会叠起来 —— 那正是之前爆显存的成因之一。
        #   这里在插件侧就串行化：后来的排队等，不并发。
        #   超时只包住真正出图那一段，排队时间不算进去。
        lock = getattr(self, "_render_lock", None)
        if lock is None:
            # 兜底：实例不是走 __init__ 建的（单元测试、或将来别的入口）时补一个，
            # 免得整条出图路因为少个属性就 AttributeError。
            lock = asyncio.Lock()
            self._render_lock = lock
        if lock.locked():
            logger.info("[toolbox] 已有出图任务在跑，排队等待…")
        async with lock:
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                # ★ wait_for 只取消了协程；to_thread 里的线程停不下来、GPU 还在算。
                #   如果这时直接放锁，下一个请求会卡在 worker 的线程锁上，
                #   再被自己的 urlopen 超时打断 —— 连环报错 + BrokenPipe 噪音。
                #   所以先把 worker 判为脏（停掉并丢弃），再放锁，下次重新拉起。
                self._discard_worker()
                # 复用 SDBackendError 是因为两个调用点都已经在 catch 它；
                # 消息里写清楚是超时，别让人误以为是后端崩了。
                raise SDBackendError(
                    f"出图超时：超过 {timeout:.0f} 秒还没画完，已中止。"
                    "常见原因：worker 冷启动太慢（换更小的模型）、显存不够被换到内存、"
                    "或者前面还有别的任务在排。调大 image_timeout 或减小分辨率再试。"
                ) from None

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
                "比如画图就是 generate_image(prompt)，"
                "搜网页就是 web_search_bing(query, count)。"
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
            "画图是单独一个目录：想画图 / 换画图模型时，先调 draw_tools()，",
            "它会展开 generate_image / switch_model，并给出提示词规则和可选的画风、底模。",
        ]
        return "\n".join(lines)

    @filter.llm_tool(name="draw_tools")
    async def draw_tools(self, event: AstrMessageEvent) -> str:
        """画图说明书（可选）：给出提示词规则、可选画风/底模清单、当前出图参数。

        ★ 不是前置步骤：generate_image / switch_model 一直都在工具列表里，
          想直接画就直接调；不确定提示词怎么写、或想挑画风/底模时再来调这个。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not hasattr(self, "_draw_revealed"):
            self._draw_revealed = {}
        already = umo in self._draw_revealed
        self._draw_revealed[umo] = time.time()
        if len(self._draw_revealed) > 200:
            keep = sorted(self._draw_revealed.items(), key=lambda kv: kv[1])[-100:]
            self._draw_revealed = dict(keep)
        logger.info(f"[toolbox] 画图说明书被调用：{umo or '（未知会话）'}")

        if already:
            return (
                "这份画图规范已经给过你了，不用重复要。\n"
                "现在可以直接调 generate_image(prompt, style, reference, size) 画图，"
                "或用 switch_model(model) 换底模。"
            )

        model = os.path.basename(self._current_model()) or "（未配置）"
        sampler = self._conf_str("sd_sampler") or "euler"
        sampler_cn = "欧拉 + 普通" if sampler != "dpmpp2m_karras" else "DPM++ 2M + Karras"
        w = self._conf_int("sd_width", 720) or 720
        h = self._conf_int("sd_height", 1024) or 1024
        cap, px, mp, warn = self._size_limits()
        lines = [
            "画图工具的用法（直接调即可，调本工具不是前置步骤）：",
            "· generate_image(prompt, style, reference, size) —— 画一张图并发出去",
            "· switch_model(model) —— 换底模（不带参数=只列可选项）",
            "",
            f"当前参数：底模 {model} · 采样器 {sampler_cn} · 默认 {w}×{h} · "
            f"{self._conf_int('sd_steps', 25)} 步 · CFG {self._conf_float('sd_cfg', 5.5)}",
            "",
            "prompt 四条硬规则：",
            "① 只能是英文（中文会直接出废图）；② 逗号分隔的【短标签】，不要写长句；",
            "③ 上限 77 token（英文约 50~60 词，10~15 个标签就够），超出会被静默截断；",
            "④ 开头写 masterpiece, best quality, highres；"
            "不要写 8k / ultra detailed / award winning（它不认，只占额度）。",
            "画质更靠具体描述：光线（soft lighting / rim light）、"
            "镜头（close-up / from above / upper body）、质感（detailed eyes / glossy hair）。",
            "",
            "size 是画幅：写 832x1216 这样，或者简写 竖 / 横 / 方；留空 = 默认尺寸。",
            f"画幅三档：单边 ≤{warn} 直接画；{warn}~{cap} 之间会先返回警告"
            "（费时费力、可能爆显存），必须先问用户，用户同意后带 force=true 再调；"
            f"单边超过 {cap}、或总面积超过 {mp:.2f} MP 直接拒绝 —— "
            f"最大画布约 {cap}×{int(px / cap)}（再大显存会爆，不给过）。",
            "reference 填 message 表示用这条消息里的图，填 last 表示用你上一张。",
        ]
        hint = self._lora_catalog_hint()
        if hint:
            lines.append(hint.strip())
        mhint = self._model_catalog_hint()
        if mhint:
            lines.append(mhint.strip())
        return "\n".join(lines)

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        style: str = "",
        reference: str = "",
        size: str = "",
        force: bool = False,
    ) -> str:
        """按描述画一张图并发到当前会话。用户明确想看图/要你画东西时才用。

        ★ 画图的完整规范（提示词四条硬规则、可选画风/底模清单）在 draw_tools 里，
          这里只留参数说明 —— 这个描述每轮都会进提示词，太长了白烧 token。

        Args:
            prompt(string): 英文【标签式】描述，逗号分隔的短标签（10~15 个），
                开头写 "masterpiece, best quality, highres"。中文会出废图。
            style(string): 画风（LoRA），留空 = 不用。名字见 draw_tools 里的清单；
                可叠加，如 "cunnyfunky:0.7"。
            reference(string): 参考图来源：留空 = 文生图；
                "message" = 用当前消息里的那张图；"last" = 用你上一张生成的图。
            size(string): 画幅，如 "832x1216"；也可简写 竖 / 横 / 方；留空 = 默认尺寸。
                单边超过 1440 会先返回警告，必须先问用户；用户同意后再填 force=True。
                硬上限：单边 1920、总面积 1920×1440（1920×1920 会被拒绝，显存会爆）。
            force(bool): 只有"画幅超过 1920、且用户已经同意"时才填 True。
                其余情况一律不填（留空 = false）。
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return "画图失败：prompt 是空的。请给一组英文标签，比如 \"1girl, silver hair, forest\"。"

        # ★ 中文提示词提醒：直接传中文不会报错，但会白等十几秒出一张废图。
        #   这里把它拦下来，让模型自己改写后重调 —— 只提醒，不代劳翻译。
        if looks_chinese(prompt):
            logger.info(f"[toolbox] 画图请求里含中文，已提醒模型改写英文：{prompt[:60]}")
            return (
                "先别画：你传的 prompt 里有中文，而底模的文本编码器只认英文，"
                "中文提示词出来的图基本是废的。\n"
                "请把它改写成【英文标签】再调用一次 generate_image：\n"
                "· 逗号分隔的短标签，不要长句\n"
                "· 挑最关键的 10~15 个（上限 77 token，超了会被静默截断）\n"
                "例：中文「银发少女站在森林里，逆光」→ 英文 "
                '"1girl, silver hair, long hair, forest, backlight, depth of field"'
            )

        # ★ 画幅：AI 可以自己选（size），留空就用配置里的默认尺寸。
        #   解析不出来就【报错】而不是悄悄用默认 —— 否则模型以为自己指定了画幅。
        cap, px, mp, warn = self._size_limits()
        try:
            dims = parse_size(size, cap, px)
        except ValueError as exc:
            return f"画图失败：{exc}"

        # ★ 大画幅要先问用户：任一边超过 1440 时，一张图会明显更慢、更吃显存
        #   （实测 1920×1920 就能把 12 GB 显存逼到临界）。所以这里【不画】，
        #   先返回警告让模型去问；用户点头之后模型再带 force=True 调一次。
        # ★ 大画幅要先问用户：单边超过 1440 时，一张图会明显更慢、更吃显存
        #   （实测 1440² 就 9.3 GB、1920×1440 峰值 11.6 GB / 12.2 GB）。
        #   ★ 群聊里直接禁止：群里人多、显存一爆整台机器都卡，不能靠"问一句"兜。
        if dims and max(dims) > warn:
            w, h = dims
            in_group = "GroupMessage" in str(getattr(event, "unified_msg_origin", ""))
            if in_group:
                logger.warning(f"[toolbox] 群聊里请求 {w}x{h} 大画幅，已拒绝")
                return (
                    f"画图失败：群聊内严令禁止使用长宽超过 {warn} 像素的画幅"
                    f"（你给的是 {w}×{h}）。\n"
                    f"请改用 ≤{warn} 的尺寸重画，例如 1216x832 / 1024x1024 / 832x1216；"
                    "实在要更大，请让用户私聊我。"
                )
            if not force:
                logger.info(f"[toolbox] 画幅 {w}x{h} 超过 {warn}，先让模型问用户")
                return (
                    f"⚠️ 画幅 {w}×{h} 的任意一边超过了 {warn}："
                    "这个尺寸费时费力（像素量是默认尺寸的好几倍，单张可能要一两分钟），"
                    "显存也可能撑不住直接失败。\n"
                    f"★ 群聊内严令禁止调用长宽超过 {warn} 像素的画幅 —— 只有当这是私聊、"
                    "并且用户明确同意时才继续。\n"
                    "现在【没有开始画】。请把这一点告诉用户（说清楚会慢、可能爆显存），"
                    "问他要不要就按这个尺寸画。\n"
                    f"用户明确同意后，再调用一次 generate_image，参数不变、额外带上 "
                    f"force=true；用户说换个小的就按新的画。\n"
                    f"（硬上限：单边 {cap}、总面积 {mp:.2f} MP；再大直接拒绝。）"
                )

        # ★ SD1.5 的 CLIP 输入上限是 77 token，超出会被静默截断。
        #   这里只【记日志】（不改行为）：词数明显偏多时留一条痕迹，方便回看
        #   为什么某次出图"后半段提示词没生效"。
        word_count = len(prompt.split())
        if word_count > 60:
            logger.warning(
                f"[toolbox] 画图 prompt 偏长（{word_count} 词），"
                f"超过约 77 token 的部分会被截断：{prompt[:80]}"
            )

        # ★ 出图期间往会话报步数（最多 2 条，见 _chat_progress）。
        #   放在 try 外面：无论成功失败都要在 finally 里 cancel 掉。
        progress_task: asyncio.Task | None = None
        if self._conf_bool("sd_progress_in_chat", True):
            progress_task = asyncio.create_task(self._chat_progress(event))

        # ★ 画风：把名字解析成"具体文件 + 强度"，worker 只负责装卸。
        try:
            picked = resolve_loras(
                style,
                self._lora_list(),
                allow_nsfw=self._conf_bool("sd_allow_nsfw_lora", True),
                default_scale=self._conf_float("sd_lora_scale", 0.8) or 0.8,
            )
        except LoraError as exc:
            if progress_task is not None:
                progress_task.cancel()
            return f"画图失败：{exc}"

        # ★ 参考图：SDXL 的 img2img 比 SD1.5 强得多，所以这条路重新打开，
        #   但【由模型决定用不用】—— reference 留空就是普通文生图。
        ref_bytes = await self._reference_bytes(reference, event)
        opts_img: dict = {"loras": picked}
        if dims:
            opts_img["width"], opts_img["height"] = dims
        if ref_bytes:
            opts_img["image"] = base64.b64encode(ref_bytes).decode("ascii")
            opts_img["strength"] = self._conf_float("img2img_strength", 0.6) or 0.6
            logger.info(
                f"[toolbox] 走参考图生图：{len(ref_bytes) // 1024} KB，"
                f"画风={[p.get('name') for p in picked] or '无'}"
            )

        # ★ finally 里 cancel：以前只在成功路径上 cancel，出图抛别的异常时
        #   这个任务会继续跑到 deadline，对着已经结束的会话报进度。
        try:
            data, filename = await self._render_image(prompt, opts_img)
        except SDBackendError as exc:
            logger.warning(f"[toolbox] 画图失败: {exc}")
            return f"画图失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 画图异常: {exc}", exc_info=True)
            return f"画图失败：{type(exc).__name__}: {exc}"
        finally:
            if progress_task is not None:
                progress_task.cancel()

        path = self._save_generated(
            data,
            filename,
            "sd",
        )
        self._last_image = (path, filename)
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Image.fromFileSystem(str(path))]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 发送生成的图失败: {exc}", exc_info=True)
            return f"图画好了但发送失败：{type(exc).__name__}: {exc}"

        logger.info(
            f"[toolbox] 已发送生成的图 {path.name}（{len(data):,} 字节）→ "
            f"{event.unified_msg_origin}"
        )
        return (
            f"图已经画好并发到会话里了（{filename}，{len(data):,} 字节）。"
            "不要再重复调用本工具；用户还想要就换一个新的画面描述。"
        )

    @filter.llm_tool(name="switch_model")
    async def switch_model(self, event: AstrMessageEvent, model: str = "") -> str:
        """换一个画图用的底模（checkpoint），换完对之后的出图立刻生效。

        Args:
            model(string): 底模名字（可不带 .safetensors，大小写不敏感，也可以只写
                其中一段，比如 "noob"）。可选值见描述末尾的【可选底模】清单。
                留空 = 只列出有哪些底模可选，不换。
        """
        models = self._model_list()
        if not models:
            return (
                "换模型失败：底模目录里没有可用模型。"
                f"（目录：{self._model_dir()}）"
            )
        if not (model or "").strip():
            lines = [f"现在用的是：{os.path.basename(self._current_model()) or '（未配置）'}"]
            lines.append("可选底模（把名字填进 model 参数）：")
            for m in models:
                lines.append(f"· {m['name']}（{round(m['size_mb'] / 1024, 2)} GB）")
            return "\n".join(lines)

        try:
            path = self._resolve_model(model)
        except ModelError as exc:
            return f"换模型失败：{exc}"

        # ★ 先换、再改"当前模型"：换失败时不能把状态改成一个其实没加载上的模型，
        #   否则后面每张图都会卡在同一个坏文件上，而且报错点看起来毫无关联。
        try:
            info = await self._sd_client().switch_model(path)
        except SDBackendError as exc:
            logger.warning(f"[toolbox] 换底模失败: {exc}")
            return f"换模型失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 换底模异常: {exc}", exc_info=True)
            return f"换模型失败：{type(exc).__name__}: {exc}"

        self._model_override = path
        name = os.path.basename(path)
        cost = float(info.get("seconds") or 0)
        if info.get("switched"):
            logger.info(f"[toolbox] 已切换底模 {name}（{cost:.1f} 秒）")
            return (
                f"已切换底模：{name}（加载耗时 {cost:.1f} 秒）。"
                "之后的出图都用这个模型，直到再换或重载插件。"
                "现在可以接着调 generate_image 画图。"
            )
        return f"现在用的就是 {name}，没有重复加载（0 秒）。"

    # ------------------------------------------------------------------
    #  生图页面（让人也能直接用）
    # ------------------------------------------------------------------

    def _imggen_defaults(self) -> dict:
        """生图页的默认参数（全部来自 sd_* 配置）。"""
        return {
            "backend": "standalone",
            "width": self._conf_int("sd_width", 512) or 512,
            "height": self._conf_int("sd_height", 512) or 512,
            "steps": self._conf_int("sd_steps", 25),
            "cfg": self._conf_float("sd_cfg", 7.0),
            "model": os.path.basename(self._current_model()) or "（未配置）",
            "sampler": self._conf_str("sd_sampler") or "euler",
        }

    async def api_imggen_status(self):
        """生图页初始化：默认参数 + 后端状态。"""
        try:
            info = self._imggen_defaults()
            info["ok"] = True
            info["worker_running"] = bool(self._sd and self._sd.running())
            # 给页面下拉框用：有哪些底模可选
            try:
                info["models"] = [
                    {"name": m["name"], "size_mb": m["size_mb"], "current": m["current"]}
                    for m in self._model_list()
                ]
                info["model_dir"] = str(self._model_dir())
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[toolbox] 取底模列表失败: {exc}")
                info["models"] = []
            # 画幅上限也透出去：页面上的提示和输入框上限跟着配置走
            try:
                cap, _px, mp, warn = self._size_limits()
                info["max_side"] = cap
                info["max_mp"] = round(mp, 2)
                info["warn_side"] = warn
            except Exception:  # noqa: BLE001
                pass
            # 给页面下拉框用：有哪些画风可选
            try:
                info["styles"] = [
                    {"name": lo.name, "rating": lo.rating, "size_mb": lo.size_mb}
                    for lo in self._lora_list()
                    if self._conf_bool("sd_allow_nsfw_lora", True) or lo.rating == "SFW"
                ]
                info["lora_status"] = str((self._conf or {}).get("sd_lora_status") or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[toolbox] 取画风列表失败: {exc}")
                info["styles"] = []
            # ★ 把 worker 的 LoRA 状态透出去：LoRA 加载失败时是"静默降级"
            #   （只记日志、继续无 LoRA 出图），用户只会觉得"风格没出来"。
            #   页面上显示一行，有没有挂上一眼就看得见。
            try:
                if self._sd is not None and self._sd.running():
                    health = await asyncio.to_thread(self._sd._health_sync, 2.0)
                    if isinstance(health, dict):
                        info["lora"] = health.get("lora") or ""
                        info["lora_scale"] = health.get("lora_scale") or 0
                        info["arch"] = health.get("arch") or ""
                        info["offload"] = bool(health.get("offload"))
            except Exception:  # noqa: BLE001
                pass
            info["idle_timeout"] = self._conf_int("sd_idle_timeout", 300)
            info["sd_ready"] = bool(
                self._conf_str("sd_python") and self._conf_str("sd_checkpoint")
            )
            return json_response(info)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 生图页 status 失败: {exc}", exc_info=True)
            return json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

    async def api_imggen_generate(self):
        """生图页出图。返回 base64，前端直接塞进 <img>。"""
        try:
            payload = await web_request.json(default={}) or {}
        except Exception as exc:  # noqa: BLE001
            return json_response(
                {"ok": False, "error": f"请求体不是 JSON: {exc}"}, status_code=400
            )
        if not isinstance(payload, dict):
            return json_response({"ok": False, "error": "请求体应是 JSON 对象"}, status_code=400)

        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return json_response({"ok": False, "error": "prompt 不能为空"}, status_code=400)

        opts: dict = {}
        for key in ("width", "height", "steps"):
            if payload.get(key) in (None, ""):
                continue
            try:
                opts[key] = int(payload[key])
            except (TypeError, ValueError):
                return json_response(
                    {"ok": False, "error": f"{key} 必须是整数"}, status_code=400
                )
        if payload.get("cfg") not in (None, ""):
            try:
                opts["cfg"] = float(payload["cfg"])
            except (TypeError, ValueError):
                return json_response({"ok": False, "error": "cfg 必须是数字"}, status_code=400)
        if payload.get("seed") not in (None, ""):
            try:
                opts["seed"] = int(payload["seed"])
            except (TypeError, ValueError):
                return json_response(
                    {"ok": False, "error": "seed 必须是整数"}, status_code=400
                )
        # 画风（可选）：和模型调 generate_image 的 style 同一套解析逻辑
        style = str(payload.get("style") or "").strip()
        if style:
            try:
                opts["loras"] = resolve_loras(
                    style,
                    self._lora_list(),
                    allow_nsfw=self._conf_bool("sd_allow_nsfw_lora", True),
                    default_scale=self._conf_float("sd_lora_scale", 0.8) or 0.8,
                )
            except LoraError as exc:
                return json_response({"ok": False, "error": str(exc)}, status_code=400)

        # 底模（可选）：页面上选的那个。留空 = 用当前底模。
        model = str(payload.get("model") or "").strip()
        if model:
            try:
                opts["checkpoint"] = self._resolve_model(model)
            except ModelError as exc:
                return json_response({"ok": False, "error": str(exc)}, status_code=400)

        # ★ 画幅硬上限对页面同样有效：这里是普通 HTTP 接口，没有"问用户"这一层，
        #   所以 1920×1920 这种必须当场拒 —— 否则一条手滑请求就能把显存打爆。
        if opts.get("width") or opts.get("height"):
            cap, px, _mp, _warn = self._size_limits()
            try:
                check_canvas(
                    int(opts.get("width") or self._conf_int("sd_width", 720) or 720),
                    int(opts.get("height") or self._conf_int("sd_height", 1024) or 1024),
                    cap,
                    px,
                )
            except ValueError as exc:
                return json_response({"ok": False, "error": str(exc)}, status_code=400)

        # 参考图生图（可选）：给了 image 就走 img2img
        img = payload.get("image")
        if isinstance(img, str) and img.strip():
            opts["image"] = img.strip()
            if payload.get("strength") not in (None, ""):
                try:
                    strength = float(payload["strength"])
                except (TypeError, ValueError):
                    return json_response(
                        {"ok": False, "error": "strength 必须是数字"}, status_code=400
                    )
                if not 0.0 < strength <= 1.0:
                    return json_response(
                        {"ok": False, "error": "strength 要在 (0, 1] 之间"},
                        status_code=400,
                    )
                opts["strength"] = strength

        t0 = time.time()
        try:
            data, filename = await self._render_image(prompt, opts)
        except SDBackendError as exc:
            logger.warning(f"[toolbox] 生图页生成失败: {exc}")
            return json_response({"ok": False, "error": str(exc)}, status_code=500)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[toolbox] 生图页生成异常: {exc}", exc_info=True)
            return json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

        elapsed = round(time.time() - t0, 1)
        # ★ 页面选的底模要变成"当前底模"：否则下一次 AI 出图会切回配置默认，
        #   白等十几秒重载一次，用户还会觉得"我刚换的模型怎么没生效"。
        if opts.get("checkpoint"):
            self._model_override = str(opts["checkpoint"])
        path = self._save_generated(
            data,
            filename,
            "sd",
        )
        self._last_image = (path, filename)
        logger.info(f"[toolbox] 生图页出图 {filename} {len(data):,} 字节 {elapsed} 秒")
        return json_response(
            {
                "ok": True,
                "image": "data:image/png;base64," + base64.b64encode(data).decode(),
                "filename": filename,
                "bytes": len(data),
                "elapsed": elapsed,
                "prompt": prompt,
                "model": os.path.basename(self._current_model()),
            }
        )

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

    async def api_imggen_progress(self):
        """生图进度（步数）。没在跑就回 busy=False。"""
        try:
            if self._sd is None:
                return json_response(
                    {"ok": True, "busy": False, "step": 0, "total": 0, "elapsed": 0.0}
                )
            return json_response(await self._sd.progress())
        except Exception as exc:  # noqa: BLE001
            return json_response({"ok": False, "error": str(exc)}, status_code=500)

    async def api_imggen_last(self):
        """把最近一次生成的图当文件返回 —— 页面用它下载。

        ★ 为什么不让前端自己 a.click() 下载：插件页面跑在
          `sandbox="allow-scripts allow-forms allow-downloads"` 的 iframe 里，
          【没有 allow-same-origin】—— 页面是不透明源，手写 data-URL 下载
          实测无效（点「下载原图」没反应）。让父页面用桥接的 download()
          来取这个接口就稳了：父页面不受该 sandbox 限制。
        """
        if not self._last_image:
            return json_response(
                {"ok": False, "error": "还没有生成过图"}, status_code=404
            )
        path, filename = self._last_image
        if not Path(path).exists():
            return json_response(
                {"ok": False, "error": "临时文件已被清理，重新生成一张即可"},
                status_code=404,
            )
        return file_response(str(path), filename=filename, content_type="image/png")


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

    return f"<system_reminder>当前对话场景：{where}{who}</system_reminder>"


def _text_result(text: str) -> mcp.types.CallToolResult:
    """把一句话包成工具结果，让 preview_sticker 的失败分支也能用统一返回类型。"""
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=text)]
    )


