"""QQ 收藏表情（收藏的表情包）的读取、缩略图与发送。

数据来自 SnowLuma 的 OneBot 扩展 API，已实测可经 AstrBot 的 OneBot 连接调用：
  · fetch_custom_face_detail(count) -> [{emoji_id, resId, url, md5, desc}]
      desc 就是用户在 QQ 里给收藏表情写的备注，未设置时为空串。
  · send_custom_face(emoji_id, group_id|user_id)
      发出去的是「动画表情」本身，不是一张普通图片。

注意：这些 action 是 SnowLuma（NapCat 系）的扩展，换别的 OneBot 实现不一定有。
"""

from __future__ import annotations

import hashlib
import io
import json

import aiohttp
from PIL import Image, ImageSequence

from astrbot.api import logger

# 一次向 SnowLuma 最多索要多少张。它没有 offset 参数，分页只能本地做。
MAX_FETCH = 200

# 缩略图缓存：md5 -> JPEG 字节。避免每次预览都重新下载几百 KB 的动图。
_PREVIEW_CACHE: dict[str, bytes] = {}
_PREVIEW_CACHE_LIMIT = 64

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=45)


class StickerError(RuntimeError):
    """收藏表情相关的可读错误，直接回给模型即可。"""


async def fetch_all(event, count: int = MAX_FETCH) -> list[dict]:
    """取回当前账号的全部收藏表情。"""
    try:
        data = await event.bot.call_action("fetch_custom_face_detail", count=count)
    except Exception as exc:  # noqa: BLE001
        raise StickerError(
            "调用 SnowLuma 的 fetch_custom_face_detail 失败"
            f"（{type(exc).__name__}: {exc}）。"
            "请确认 QQ 客户端已登录、SnowLuma 在运行，且它连接的是支持收藏表情的协议端。"
        ) from exc

    if data is None:
        return []
    if not isinstance(data, list):
        raise StickerError(f"收藏表情接口返回了意外结构：{type(data).__name__}")

    items = [item for item in data if isinstance(item, dict) and item.get("emoji_id")]
    if len(items) != len(data):
        logger.warning(
            f"[toolbox] 收藏表情里有 {len(data) - len(items)} 条缺 emoji_id，已跳过"
        )
    return items


def collection_key(items: list[dict]) -> str:
    """收藏集的指纹：只跟「有哪些表情」有关，跟它们的先后顺序无关。

    用来判断缓存下来的那份打乱顺序还作不作数 —— 你新收藏或删掉一张，
    指纹就变了，序号映射必须重算，否则会指向不存在的表情。
    """
    ids = sorted(str(item.get("emoji_id") or "") for item in items)
    return hashlib.md5("\n".join(ids).encode()).hexdigest()[:16]


def shuffled(items: list[dict], seed: str) -> list[dict]:
    """按种子做一次**可复现**的打乱。

    「可复现」是这个功能的全部关键：list_stickers / preview_sticker /
    send_sticker 是三次独立调用，各自重新拉一遍收藏列表，但它们必须算出
    **同一个序号映射** —— 否则模型预览的是 [5]，发出去的却是另一张。

    做法不是 `random.shuffle`，而是给每张算一个 `md5(种子 + emoji_id)` 当排序键：
      · 同样的种子 + 同样的收藏集 → 永远同样的顺序（跨进程、跨 Python 版本都成立）；
      · 跟接口返回的原始顺序无关 → 接口两次返回顺序不同也不会错位；
      · 种子换了 → 整体换一个排列，但仍是同一批表情、不重不漏。
    """
    def order_key(item: dict) -> tuple[str, str]:
        emoji_id = str(item.get("emoji_id") or "")
        return hashlib.md5(f"{seed}:{emoji_id}".encode()).hexdigest(), emoji_id

    return sorted(items, key=order_key)


def _paginate(items: list[dict], page: int, page_size: int) -> tuple[list[dict], int, int, int]:
    """返回 (本页条目, 实际页码, 总页数, 本页首条的全局序号)。"""
    page_size = max(1, int(page_size))
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page)), pages)
    start = (page - 1) * page_size
    return items[start : start + page_size], page, pages, start + 1


def render_list(items: list[dict], page: int, page_size: int, shuffled: bool = False) -> str:
    """把一页收藏表情渲染成给模型看的文字列表。

    有备注的显示备注，没有备注的只显示序号 —— 那种需要先 preview_sticker 看图。

    shuffled=True 时会多告诉模型一句「顺序是打乱的、翻页不会重排」。
    ★ 只有真的打乱了才能说 —— 否则模型会以为序号不可靠，反而不敢再用翻页。
    """
    if not items:
        return "这台 QQ 账号的收藏表情是空的（一张都没有）。"

    chunk, page, pages, first_index = _paginate(items, page, page_size)
    header = f"收藏表情共 {len(items)} 张，第 {page}/{pages} 页，序号是全局的、跨页可复用。"
    if shuffled:
        header += (
            "这次看到的顺序是随机打乱的（表情太多，每次重新打开都换一批让你挑）；"
            "但翻页不会重排，序号一直有效。"
        )
    lines = [
        header,
        "带文字的是用户在 QQ 里写的备注；只有序号没有文字的代表没有备注。",
        "",
    ]
    for offset, item in enumerate(chunk):
        index = first_index + offset
        note = (item.get("desc") or "").strip()
        lines.append(f"[{index}] {note}" if note else f"[{index}]")

    if pages > 1 and page < pages:
        lines.append("")
        lines.append(f"想看下一页：list_stickers(page={page + 1})")
    lines.append("")
    # ★ 末句特意点明"别回头问用户"：实测 4B 拿到列表后会回「你要哪一个呢？」
    #   把决定推回给用户 —— 而用户说"发个表情包"就是想看模型自己挑。
    lines.append(
        "选好之后先 preview_sticker(index) 看实际图片确认，确认合适再 send_sticker(index) 发出。"
        "★ 自己挑一张直接发，不要回头问用户「要哪一张」——用户让你发表情，就是想看你挑。"
    )
    return "\n".join(lines)


def find_by_index(items: list[dict], index: int) -> dict:
    """按 1 起的全局序号取一张。越界抛出可读错误。"""
    try:
        index = int(index)
    except (TypeError, ValueError) as exc:
        raise StickerError(f"序号必须是整数，收到 {index!r}") from exc
    if index < 1 or index > len(items):
        raise StickerError(
            f"序号 {index} 超出范围：当前收藏表情共 {len(items)} 张，"
            f"序号范围是 1..{len(items)}。可以先用 list_stickers 查看。"
        )
    return items[index - 1]


def _pick_middle_frame(raw: bytes) -> Image.Image:
    """动图取中间一帧，单帧图直接返回；统一转 RGB。"""
    image = Image.open(io.BytesIO(raw))
    frames = list(ImageSequence.Iterator(image))
    if len(frames) > 1:
        return frames[len(frames) // 2].convert("RGB")
    return image.convert("RGB")


def to_preview_jpeg(raw: bytes, max_side: int = 224) -> bytes:
    """等比缩放，最长边不超过 max_side，输出 JPEG 字节。不拉伸、不补边。"""
    frame = _pick_middle_frame(raw)
    width, height = frame.size
    longest = max(width, height)
    if longest > max_side:
        scale = max_side / longest
        frame = frame.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.LANCZOS,
        )
    buffer = io.BytesIO()
    frame.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


async def load_preview(item: dict, max_side: int = 224) -> bytes:
    """下载并生成一张收藏表情的预览图（带 md5 缓存）。"""
    key = f"{item.get('md5') or item.get('emoji_id')}@{max_side}"
    cached = _PREVIEW_CACHE.get(key)
    if cached is not None:
        return cached

    url = item.get("url") or ""
    if not url:
        raise StickerError(f"这张表情没有图片地址，无法预览（emoji_id={item.get('emoji_id')}）")

    try:
        async with aiohttp.ClientSession(timeout=_FETCH_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise StickerError(f"下载表情图片失败：HTTP {resp.status}")
                raw = await resp.read()
    except StickerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StickerError(f"下载表情图片出错（{type(exc).__name__}: {exc}）") from exc

    jpeg = to_preview_jpeg(raw, max_side)

    if len(_PREVIEW_CACHE) >= _PREVIEW_CACHE_LIMIT:
        _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
    _PREVIEW_CACHE[key] = jpeg
    logger.info(
        f"[toolbox] 生成预览 emoji_id={item.get('emoji_id')} "
        f"原图 {len(raw)} 字节 → {len(jpeg)} 字节 最长边={max_side}"
    )
    return jpeg


def _as_id(value: str) -> int | str:
    """SnowLuma 的 groupId/userId 校验器要正整数，能转就转成 int。"""
    text = str(value).strip()
    return int(text) if text.isdigit() else text


async def send_sticker(event, item: dict) -> str:
    """把一张收藏表情发到当前会话，返回给模型看的说明。"""
    emoji_id = item.get("emoji_id") or ""
    if not emoji_id:
        raise StickerError("这张表情没有 emoji_id，无法发送")

    group_id = event.get_group_id()
    params: dict = {"emoji_id": emoji_id}
    if group_id:
        params["group_id"] = _as_id(group_id)
        target = f"群 {group_id}"
    else:
        user_id = event.get_sender_id()
        if not user_id:
            raise StickerError("既拿不到群号也拿不到用户号，无法确定发送目标")
        params["user_id"] = _as_id(user_id)
        target = f"用户 {user_id}"

    try:
        result = await event.bot.call_action("send_custom_face", **params)
    except Exception as exc:  # noqa: BLE001
        raise StickerError(
            f"发送收藏表情失败（{type(exc).__name__}: {exc}）。"
            "目标可能是 SnowLuma 不支持的协议端，或该 emoji_id 已失效。"
        ) from exc

    message_id = ""
    if isinstance(result, dict):
        message_id = str(result.get("message_id") or "")
    logger.info(
        f"[toolbox] 已发送收藏表情 emoji_id={emoji_id} → {target} message_id={message_id}"
    )
    return json.dumps(
        {
            "sent": True,
            "to": target,
            "message_id": message_id,
            "note": "表情已发出，不要再重复发送同一张。",
        },
        ensure_ascii=False,
    )
