# -*- coding: utf-8 -*-
"""QQ 空间说说 —— 给 LLM 的发布工具（所有人可用，带商量/审核/频控护栏）。

工具 qzone_publish：
  · 模型必须先在聊天里和用户商量内容，用户明确同意后 confirmed=true 才发布；
  · 文本先过禁止词粗筛 + 模型文本审核；
  · 配图走「群聊审核」同一套视觉判定（moderation.judge，只喂图片，
    不带提示词/私聊对话进上下文，审核结果也不写会话历史）；
  · 频控：每人每天 1 条、全局每天 5 条、全局间隔 30 分钟（均可在配置/页面调）；
  · 仅好友可用（陌生人拒绝，可配置放开）；
  · 发布记录写 JSONL，页面可查看/删除。

配置在 AstrBot 插件配置页（_conf_schema.json），也可在插件页面里改。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.web import json_response
from PIL import Image

PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_DIR_NAME = PLUGIN_DIR.name
# <AstrBot>/data/plugins/<插件>/main.py → parents[2] = <AstrBot>
ASTRBOT_ROOT = PLUGIN_DIR.parents[2]
DATA_DIR = ASTRBOT_ROOT / "data" / "plugin_data" / "ncd_qzone"
STATE_FILE = DATA_DIR / "state.json"
POSTS_FILE = DATA_DIR / "posts.jsonl"
CONFIG_FILE = ASTRBOT_ROOT / "data" / "config" / f"{PLUGIN_DIR_NAME}_config.json"

MAX_RECORDS = 500
# QQ 空间说说禁止词粗筛（模型审核之外的最后一道线）
BANNED_HINTS = (
    "习近平", "共产党", "政治", "六四", "台独", "港独", "法轮功", "皇军", "靖国神社",
    "幼女", "萝莉性", "强奸", "轮奸", "兽交", "恋童", "未成年做爱", "血洗", "屠杀",
)
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# ---- 成熟发布引擎：qzone_bridge（来自 Skarie/astrbot_plugin_qq_tools，MIT）----
import sys as _sys

if str(PLUGIN_DIR) not in _sys.path:
    _sys.path.insert(0, str(PLUGIN_DIR))
try:
    from qzone_bridge.client import QzoneClient  # noqa: E402
    from qzone_bridge.models import SessionState  # noqa: E402
    from qzone_bridge.onebot_cookie import fetch_cookie_text, fetch_login_uin  # noqa: E402
    from qzone_bridge.parser import parse_cookie_text  # noqa: E402

    QZONE_BRIDGE_OK = True
    QZONE_BRIDGE_ERR = ""
except Exception as _qbridge_exc:  # noqa: BLE001
    QZONE_BRIDGE_OK = False
    QZONE_BRIDGE_ERR = f"{type(_qbridge_exc).__name__}: {_qbridge_exc}"


# ---------------------------------------------------------------------------
#  审核：复用 comfy 插件的群聊图片审核（同一套判定 + prompt）
# ---------------------------------------------------------------------------
def _load_moderation():
    import importlib.util

    p = ASTRBOT_ROOT / "data" / "plugins" / "astrbot_plugin_ncd_comfy" / "moderation.py"
    try:
        spec = importlib.util.spec_from_file_location("ncd_qzone_moderation", str(p))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[qzone] 加载群聊审核模块失败（带图将不允许发布）: {exc}")
        return None


MODERATION = _load_moderation()


# ---------------------------------------------------------------------------
#  持久化
# ---------------------------------------------------------------------------
def _state_load() -> dict:
    today = time.strftime("%Y-%m-%d")
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and str(raw.get("day") or "") == today:
            return raw
    except Exception:  # noqa: BLE001
        pass
    return {"day": today, "total": 0, "users": {}, "last_ts": 0.0}


def _state_save(st: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[qzone] 频控状态保存失败: {exc}")


def _append_post(item: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with POSTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[qzone] 发布记录写入失败: {exc}")


def _posts_list(limit: int = 50) -> list[dict]:
    rows: list[dict] = []
    try:
        if POSTS_FILE.is_file():
            for line in POSTS_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except Exception:  # noqa: BLE001
        return []
    return rows[-limit:][::-1]


def _posts_compact() -> None:
    try:
        rows = _posts_list(MAX_RECORDS * 2)
        if len(rows) <= MAX_RECORDS:
            return
        keep = rows[:MAX_RECORDS][::-1]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = POSTS_FILE.with_name(POSTS_FILE.name + ".tmp")
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in keep) + "\n",
            encoding="utf-8",
        )
        tmp.replace(POSTS_FILE)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
#  参数 / 图片 / 文本
# ---------------------------------------------------------------------------
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _walk_image_bytes(comps, depth: int = 0) -> list[bytes]:
    """从消息组件里提取图片字节（含引用消息）。"""
    out: list[bytes] = []
    if not comps or depth > 3:
        return out
    for c in comps:
        if isinstance(c, Comp.Image):
            for attr in ("path", "file"):
                p = getattr(c, attr, None)
                if p and Path(str(p)).is_file():
                    try:
                        data = Path(str(p)).read_bytes()
                    except OSError:
                        data = b""
                    if data:
                        out.append(data[: 12 * 1024 * 1024])
                    break
        chain = getattr(c, "chain", None)
        if chain and not isinstance(c, Comp.Image):
            out.extend(_walk_image_bytes(chain, depth + 1))
    return out


def _event_images(event: AstrMessageEvent) -> list[bytes]:
    try:
        comps = list(event.get_messages() or [])
    except Exception:  # noqa: BLE001
        comps = []
    return _walk_image_bytes(comps)


def _image_from_spec(spec: str) -> bytes | None:
    """解析配图参数。

    ★ 2026-11-19 修 H6：LLM 传入的 images 参数原来接受任意本地路径/URL，
    可以把本机任意图片（桌面、相册）或内网服务内容发到公开 QQ 空间。
    现在只允许：
      · base64://（模型自己编码的图）
      · http(s) 公网 URL（拒绝 localhost/私网/回环/链路本地地址，防 SSRF）
      · 本地路径（仅限 AstrBot data 目录或系统临时目录内的图片，即聊天图/出图缓存）
    """
    spec = spec.strip()
    if not spec:
        return None
    if spec.startswith("base64://"):
        try:
            return base64.b64decode(spec[len("base64://"):])
        except Exception:  # noqa: BLE001
            return None
    if spec.startswith(("http://", "https://")):
        try:
            import ipaddress
            import socket
            import urllib.request
            from urllib.parse import urlparse as _urlparse

            host = (_urlparse(spec).hostname or "").strip().lower()
            if not host or host == "localhost" or host.endswith(".local"):
                return None
            for info in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(info[4][0])
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                    or ip.is_unspecified
                ):
                    return None
            req = urllib.request.Request(spec, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read(12 * 1024 * 1024)
        except Exception:  # noqa: BLE001
            return None
    # 本地路径：只允许 AstrBot data 目录 / 系统临时目录下的文件
    if spec.startswith("file://"):
        return None
    try:
        import tempfile as _tempfile

        rp = Path(spec).expanduser()
        if not rp.is_file():
            return None
        rp.resolve()
        allowed_roots = [ASTRBOT_ROOT / "data", Path(_tempfile.gettempdir())]
        if not any(_safe_relative_to(rp, root) for root in allowed_roots):
            return None
        return rp.read_bytes()[: 12 * 1024 * 1024]
    except OSError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _safe_relative_to(path: Path, root: Path) -> bool:
    """判断 path 是否在 root 目录内（含 root 本身）。"""
    try:
        p = path.resolve()
        r = root.resolve()
        return p == r or r in p.parents
    except OSError:
        return False


def _to_jpeg(blob: bytes, max_side: int = 1600) -> bytes | None:
    try:
        im = Image.open(io.BytesIO(blob)).convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90, optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _ncd_abs_path(value: str) -> Path | None:
    p = Path(str(value or "").strip())
    try:
        if p.is_file():
            return p
    except OSError:
        pass
    return None

# ---------------------------------------------------------------------------
#  插件本体
# ---------------------------------------------------------------------------
_VIS_MAP = {"public": "1", "公开": "1", "friends": "2", "好友": "2", "private": "3", "仅自己": "3", "自己": "3"}
# 协议端能力缓存（10 分钟）：NapCat 4.18.x 没有空间说说接口
_CAP_CACHE: dict = {"ts": 0.0, "ok": False, "reason": ""}


class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf: dict = config or {}
        self._friend_cache: tuple[float, set[str]] = (0.0, set())
        self._qzone_client_obj = None
        self._qzone_client_ts = 0.0
        self._qzone_lock = asyncio.Lock()
        # ★ 2026-11-19 修 H5：额度检查+预占必须原子
        self._quota_lock = asyncio.Lock()
        self._register_apis()

    # ---------- 配置 ----------
    def _c_int(self, key: str, default: int) -> int:
        try:
            return int(self._conf.get(key, default))
        except (TypeError, ValueError):
            return default

    def _c_bool(self, key: str, default: bool) -> bool:
        raw = self._conf.get(key, default)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是", "开")

    def _limits(self) -> dict:
        return {
            "per_user": max(1, self._c_int("qzone_daily_per_user", 1)),
            "total": max(1, self._c_int("qzone_daily_total", 5)),
            "gap_min": max(0, self._c_int("qzone_gap_minutes", 30)),
            "max_chars": max(50, self._c_int("qzone_max_chars", 1000)),
            "max_images": max(0, min(9, self._c_int("qzone_max_images", 9))),
            "friend_only": self._c_bool("qzone_friend_only", True),
            "text_audit": self._c_bool("qzone_text_audit", True),
            "image_audit": self._c_bool("qzone_image_audit", True),
            "fail_close": self._c_bool("qzone_audit_fail_close", True),
        }

    # ---------- 发布引擎（qzone_bridge） ----------
    async def _qzone_capability(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """发布引擎是否可用：qzone_bridge 导入成功 + 协议端支持 get_cookies。"""
        if not QZONE_BRIDGE_OK:
            return False, f"发布引擎不可用（qzone_bridge 导入失败：{QZONE_BRIDGE_ERR[:200]}）"
        return True, ""

    async def _qzone_client(self, event: AstrMessageEvent):
        """取/复用 qzone_bridge 客户端：OneBot get_cookies → QzoneClient。"""
        if not QZONE_BRIDGE_OK:
            raise RuntimeError(f"qzone_bridge 未就绪: {QZONE_BRIDGE_ERR}")
        async with self._qzone_lock:
            now = time.time()
            if self._qzone_client_obj is not None and now - self._qzone_client_ts < 900:
                return self._qzone_client_obj
            bot = getattr(event, "bot", None)
            if bot is None:
                raise RuntimeError("找不到协议端 bot，无法获取空间 Cookie")
            domain = str(self._conf.get("qzone_cookie_domain") or "qzone.qq.com")
            cookie_text = await fetch_cookie_text(bot, domain=domain)
            cookies = parse_cookie_text(cookie_text or "")
            if not cookies:
                raise RuntimeError("QQ 空间 Cookie 为空（确认已登录且协议端支持 get_cookies）")
            uin = 0
            try:
                uin = int(await fetch_login_uin(bot) or 0)
            except Exception:  # noqa: BLE001
                pass
            client = QzoneClient(SessionState(uin=uin, cookies=cookies, source="onebot"))
            self._qzone_client_obj = client
            self._qzone_client_ts = now
            logger.info(f"[qzone] 发布引擎就绪 uin={uin} cookies={len(cookies)}")
            return client


    # ---------- 好友 ----------
    async def _friend_ids(self, event: AstrMessageEvent) -> set[str] | None:
        ts, ids = self._friend_cache
        if ids and time.time() - ts < 300:
            return ids
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        try:
            data = await asyncio.wait_for(bot.call_action("get_friend_list"), timeout=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[qzone] get_friend_list 失败: {exc}")
            return None
        out: set[str] = set()
        for it in data or []:
            if isinstance(it, dict):
                qq = str(it.get("user_id") or "").strip()
                if qq:
                    out.add(qq)
        self._friend_cache = (time.time(), out)
        return out

    # ---------- 审核 ----------
    async def _audit_provider(self, event: AstrMessageEvent, umo: str):
        pid = str(self._conf.get("qzone_audit_provider") or "").strip()
        if pid:
            try:
                p = self.context.get_provider_by_id(pid)
                if p is not None:
                    return p
            except Exception:  # noqa: BLE001
                pass
        try:
            return await self.context.get_using_provider_async(umo)
        except Exception:  # noqa: BLE001
            return None

    async def _text_audit(self, provider, content: str) -> bool | None:
        prompt = (
            "你是内容审核员。只判断下面这段准备发到 QQ 空间的文字是否包含以下任一：\n"
            "色情/NSFW、露骨性描写、政治敏感、伦理争议、暴力血腥。\n"
            "不要联系任何聊天上下文。只回一个词：PASS 或 REJECT。\n\n"
            f"文字：{content[:2000]}"
        )
        try:
            resp = await asyncio.wait_for(provider.text_chat(prompt=prompt), timeout=30)
            ans = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[qzone] 文本审核失败: {exc}")
            return None
        up = ans.upper()
        if "REJECT" in up or "拒绝" in ans or "不通过" in ans:
            return False
        if "PASS" in up or "通过" in ans:
            return True
        return None

    async def _image_audit(self, provider, blobs: list[bytes]) -> tuple[bool, str]:
        if MODERATION is None:
            return False, "审核模块不可用"
        fail_close = self._c_bool("qzone_audit_fail_close", True)
        for i, blob in enumerate(blobs, 1):
            verdict = await MODERATION.judge(provider, blob, user_text="", image_prompt="")
            if verdict is False:
                return False, f"第 {i} 张配图未通过审核"
            if verdict is None and fail_close:
                return False, f"第 {i} 张配图审核判断不了（保守拒绝）"
        return True, ""

    # ---------- 频控 ----------
    async def _quota_reserve(self, sender: str) -> str:
        """原子「检查+预占」额度（并发调用不会超发）；返回空串=成功，否则=拒绝原因。"""
        lim = self._limits()
        async with self._quota_lock:
            st = _state_load()
            if int(st.get("total") or 0) >= lim["total"]:
                return f"今天全站已经发了 {lim['total']} 条，明天再来吧"
            used = int((st.get("users") or {}).get(sender) or 0)
            if used >= lim["per_user"]:
                return f"你今天已经发过 {lim['per_user']} 条了，明天再来吧"
            gap = lim["gap_min"] * 60
            last = float(st.get("last_ts") or 0.0)
            if gap and last and time.time() - last < gap:
                wait = int((gap - (time.time() - last)) // 60) + 1
                return f"刚发过一条，等 {wait} 分钟再发"
            st["total"] = int(st.get("total") or 0) + 1
            users = st.get("users") or {}
            users[str(sender)] = int(users.get(str(sender)) or 0) + 1
            st["users"] = users
            st["last_ts"] = time.time()
            _state_save(st)
            return ""

    async def _quota_rollback(self, sender: str) -> None:
        """发布/审核失败时归还预占的额度。"""
        async with self._quota_lock:
            st = _state_load()
            st["total"] = max(0, int(st.get("total") or 0) - 1)
            users = st.get("users") or {}
            left = int(users.get(str(sender)) or 0) - 1
            if left > 0:
                users[str(sender)] = left
            else:
                users.pop(str(sender), None)
            st["users"] = users
            _state_save(st)

    # ---------- 工具 ----------
    @filter.llm_tool(name="qzone_publish")
    async def qzone_publish(
        self,
        event: AstrMessageEvent,
        content: str,
        images: str = "",
        confirmed: bool = False,
        visibility: str = "",
    ) -> str:
        """发布一条 QQ 空间说说（动态）。所有人可用，但有严格护栏。

        ★ 使用规则（必须遵守）：
        1. 调用前必须先在聊天里和用户商量好说说正文（和配图），
           用户明确同意后才能调用，并把 confirmed 设置为 true；
        2. 禁止发布 NSFW、色情、政治敏感、伦理争议、暴力血腥内容；
        3. 配图会先过严格审核（只看图片本身，不带聊天上下文），未通过不能发布；
        4. 频控：每人每天 1 条、全站每天 5 条、两条间隔 30 分钟（可配置）；
        5. 仅好友可用（陌生人会被拒绝）。

        Args:
            content(string): 说说正文
            images(string): 配图，多个用英文逗号分隔，可填本地路径或 URL；如果用户在当前消息或引用消息里发了图，写 current 即可
            confirmed(boolean): 用户是否已明确同意发布；没有同意不要填 true
            visibility(string): 可见范围；留空=公开（1），friends=好友（4），private=仅自己（64）
        """
        if not confirmed:
            return "还没发布：请先在聊天里把说说内容和用户确认，用户明确同意后再调用（confirmed=true）。"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sender = ""
        if not sender:
            return "发布失败：无法识别发送者。"
        lim = self._limits()
        ok_cap, why_cap = await self._qzone_capability(event)
        if not ok_cap:
            return (
                f"发布失败：{why_cap}。"
                "如需发 QQ 空间说说，需要换支持空间接口的协议端"
                "（如 SnowLuma 2.2.0+），或使用 onebot-qzone 桥接服务。"
            )
        if lim["friend_only"]:
            friends = await self._friend_ids(event)
            if friends is None:
                return "发布失败：暂时查不到好友列表（协议端不支持或超时），为安全起见先不发布。"
            if sender not in friends:
                return "发布失败：只有好友才能发空间说说。"

        text = str(content or "").strip()
        if not text:
            return "发布失败：说说正文是空的。"
        if len(text) > lim["max_chars"]:
            return f"发布失败：正文 {len(text)} 字，超过上限 {lim['max_chars']} 字。"
        hit = next((w for w in BANNED_HINTS if w in text), "")
        if hit:
            return "发布失败：正文包含不允许发布的内容。"

        # 配图
        blobs: list[bytes] = []
        specs = [s.strip() for s in re.split(r"[,，]", str(images or "")) if s.strip()]
        for spec in specs:
            if spec.lower() == "current":
                blobs.extend(_event_images(event))
                continue
            data = _image_from_spec(spec)
            if data:
                blobs.append(data)
        blobs = [b for b in blobs if b]
        if len(blobs) > lim["max_images"]:
            return f"发布失败：配图 {len(blobs)} 张，超过上限 {lim['max_images']} 张。"
        prepared: list[bytes] = []
        for b in blobs:
            j = _to_jpeg(b)
            if not j:
                return "发布失败：有一张配图读不出来（格式不支持）。"
            prepared.append(j)

        # ★ 2026-11-19 修 H5/M3：先原子预占额度，再做审核/发布；失败回滚（之前先审核再检查，
        #   并发可击穿每天上限，且被限流的请求也会先跑完昂贵审核）。
        reject = await self._quota_reserve(sender)
        if reject:
            return f"发布失败：{reject}。"
        published = False
        try:
            provider = await self._audit_provider(event, umo)
            if provider is None:
                return "发布失败：没有可用的审核模型，不能发布。"
            if lim["text_audit"]:
                tv = await self._text_audit(provider, text)
                if tv is False:
                    return "发布失败：正文未通过审核。"
                if tv is None and lim["fail_close"]:
                    return "发布失败：正文审核判断不了（保守拒绝）。"
            if prepared and lim["image_audit"]:
                ok, why = await self._image_audit(provider, prepared)
                if not ok:
                    return f"发布失败：{why}。"

            _vis = str(visibility or "").strip().lower()
            # QQ 空间网页接口 ugc_right 只接受 1/4/16/64/128：
            #   1=所有人、4=好友、16=部分好友、64=仅自己、128=部分好友不可见
            # ★ 2026-11-19：之前把好友写成 3（非法值）→ QzoneRequestError 参数错误
            vis_val = {
                "public": 1, "公开": 1, "": 1,
                "friends": 4, "好友": 4,
                "private": 64, "仅自己": 64, "自己": 64,
            }.get(_vis, 1)
            if vis_val not in (1, 4, 16, 64, 128):
                vis_val = 1

            photos = [
                {
                    "source": "base64://" + base64.b64encode(b).decode(),
                    "name": f"qzone_{i}.jpg",
                    "mime_type": "image/jpeg",
                }
                for i, b in enumerate(prepared)
            ]
            try:
                client = await self._qzone_client(event)
                resp = await asyncio.wait_for(
                    client.publish_mood(text, photos=photos or None, ugc_right=vis_val),
                    timeout=150,
                )
            except Exception as exc:  # noqa: BLE001
                detail = getattr(exc, "detail", None)
                short = f"{type(exc).__name__}: {exc}"[:300]
                if detail:
                    short += f" | detail={str(detail)[:260]}"
                logger.warning(f"[qzone] 发布失败: {short}")
                low = short.lower()
                auth_failed = (
                    "auth" in low or "cookie" in low or "登录" in short or "1401" in short
                )
                if auth_failed:
                    # ★ 2026-11-19 修 M1：登录态失效时清掉客户端缓存，重登后立即生效
                    self._qzone_client_obj = None
                    self._qzone_client_ts = 0.0
                _append_post(
                    {
                        "ts": time.time(),
                        "umo": umo,
                        "sender": sender,
                        "content": text[:300],
                        "images": len(prepared),
                        "ok": False,
                        "error": short,
                    }
                )
                _posts_compact()
                hint = (
                    "（空间登录态失效：请重新登录 QQ 或更新 Cookie）"
                    if auth_failed
                    else ""
                )
                return f"发布失败：{short}{hint}"
            tid = ""
            if isinstance(resp, dict):
                data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                for key in ("tid", "fid", "post_id"):
                    if resp.get(key):
                        tid = str(resp.get(key))
                        break
                if not tid:
                    for key in ("tid", "fid"):
                        if data.get(key):
                            tid = str(data.get(key))
                            break
            published = True
            _append_post(
                {
                    "ts": time.time(),
                    "umo": umo,
                    "sender": sender,
                    "content": text[:300],
                    "images": len(prepared),
                    "image_hashes": [_sha(b) for b in prepared],
                    "ok": True,
                    "tid": tid,
                }
            )
            _posts_compact()
            logger.info(f"[qzone] 已发布说说 sender={sender} images={len(prepared)} tid={tid}")
            return f"已发布到 QQ 空间（配图 {len(prepared)} 张）" + (
                f"，tid={tid}" if tid else ""
            ) + "。"
        finally:
            if not published:
                await self._quota_rollback(sender)

    async def terminate(self) -> None:  # noqa: D401
        """★ 2026-11-19：插件卸载/重载时关闭 qzone_bridge 的 httpx 连接池。"""
        client = self._qzone_client_obj
        self._qzone_client_obj = None
        self._qzone_client_ts = 0.0
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- Web API（页面） ----------
    def _register_apis(self) -> None:
        apis = (
            ("overview", self.api_overview, ["GET"], "QQ空间：今日统计 + 最近记录"),
            ("config", self.api_config, ["GET", "POST"], "QQ空间：读取/保存插件配置"),
            ("records-clear", self.api_records_clear, ["POST"], "QQ空间：清空发布记录"),
            ("probe", self.api_probe, ["GET"], "QQ空间：探测协议端支持的动作（只读）"),
            ("vision-test", self.api_vision_test, ["GET"], "QQ空间：测试当前模型能否读图审核"),
        )
        for name, handler, methods, desc in apis:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_DIR_NAME}/ncd-qzone/{name}", handler, methods, desc
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[qzone] 注册 {name} 失败: {exc}")
        logger.info("[qzone] 已注册 Web API（ncd-qzone）")

    @staticmethod
    def _config_load() -> dict:
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _config_save(data: dict) -> None:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_FILE)

    async def api_vision_test(self, **kwargs):
        """用当前 provider 发一张测试图，验证 AstrBot 的图片输入通道（审核用）。"""
        import tempfile

        from PIL import Image as _Image, ImageDraw as _ImageDraw

        provider = None
        try:
            provider = await self.context.get_using_provider_async("")
        except Exception:  # noqa: BLE001
            provider = None
        if provider is None:
            try:
                pm = getattr(self.context, "provider_manager", None)
                for cand in getattr(pm, "provider_insts", []) or []:
                    cfg = getattr(cand, "provider_config", {}) or {}
                    if isinstance(cfg, dict) and cfg.get("enable"):
                        provider = cand
                        break
            except Exception:  # noqa: BLE001
                provider = None
        if provider is None:
            return json_response({"ok": False, "error": "没有可用 provider"})
        pid = ""
        try:
            pid = str(getattr(provider.meta(), "id", "") or "")
        except Exception:  # noqa: BLE001
            pid = type(provider).__name__
        im = _Image.new("RGB", (220, 120), "white")
        draw = _ImageDraw.Draw(im)
        draw.rectangle([20, 20, 90, 90], fill="red")
        draw.rectangle([120, 20, 190, 90], fill="blue")
        tmp = Path(tempfile.mktemp(suffix=".png"))
        try:
            im.save(tmp)
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt="图里左边方块是什么颜色？右边方块是什么颜色？只回答两个颜色词。",
                    image_urls=[str(tmp)],
                ),
                timeout=60,
            )
            ans = str(getattr(resp, "completion_text", "") or "").strip()
            return json_response({"ok": True, "provider": pid, "answer": ans[:300]})
        except Exception as exc:  # noqa: BLE001
            return json_response(
                {"ok": False, "provider": pid, "error": f"{type(exc).__name__}: {exc}"[:400]}
            )
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    async def api_overview(self, **kwargs):
        lim = self._limits()
        st = _state_load()
        cfg = self._config_load() or dict(self._conf)
        recent = _posts_list(30)
        return json_response(
            {
                "ok": True,
                "limits": lim,
                "today": {
                    "day": st.get("day"),
                    "total": int(st.get("total") or 0),
                    "users": int(len(st.get("users") or {})),
                    "last_ts": st.get("last_ts") or 0,
                },
                "config": {k: cfg.get(k, v) for k, v in self._defaults().items()},
                "recent": recent,
                "audit_ready": MODERATION is not None,
            }
        )

    @staticmethod
    def _defaults() -> dict:
        return {
            "qzone_daily_per_user": 1,
            "qzone_daily_total": 5,
            "qzone_gap_minutes": 30,
            "qzone_max_chars": 1000,
            "qzone_max_images": 9,
            "qzone_friend_only": True,
            "qzone_text_audit": True,
            "qzone_image_audit": True,
            "qzone_audit_fail_close": True,
            "qzone_audit_provider": "",
            "qzone_cookie_domain": "qzone.qq.com",
        }

    async def api_config(self, **kwargs):
        from astrbot.api.web import request as _req

        try:
            body = await _req.json({}) or {}
        except Exception:  # noqa: BLE001
            body = {}
        if isinstance(body, dict) and body.get("save"):
            patch = body.get("config") or {}
            defaults = self._defaults()
            cfg = self._config_load() or dict(self._conf)
            for key in defaults:
                if key not in patch:
                    continue
                val = patch[key]
                if isinstance(defaults[key], bool):
                    val = str(val).strip().lower() in ("1", "true", "yes", "on", "开", "是")
                elif isinstance(defaults[key], int):
                    try:
                        val = int(val)
                    except (TypeError, ValueError):
                        continue
                cfg[key] = val
            try:
                self._config_save(cfg)
            except OSError as exc:
                return json_response({"ok": False, "error": f"保存失败: {exc}"})
            return json_response({"ok": True, "need_reload": True, "config": cfg})
        cfg = self._config_load() or dict(self._conf)
        return json_response(
            {
                "ok": True,
                "config": {k: cfg.get(k, v) for k, v in self._defaults().items()},
                "need_reload": False,
            }
        )

    async def api_records_clear(self, **kwargs):
        try:
            if POSTS_FILE.is_file():
                POSTS_FILE.unlink()
        except OSError as exc:
            return json_response({"ok": False, "error": str(exc)})
        return json_response({"ok": True})

    async def api_probe(self, **kwargs):
        """探测发布引擎与 Cookie 获取（只读）。"""
        bot = None
        platform_name = ""
        try:
            for inst in self.context.platform_manager.get_insts():
                if hasattr(inst, "bot") and hasattr(inst.bot, "call_action"):
                    bot = inst.bot
                    platform_name = str(getattr(inst.meta(), "name", "") or "")
                    break
        except Exception as exc:  # noqa: BLE001
            return json_response({"ok": False, "error": f"找不到协议端: {exc}"})
        result = {
            "ok": True,
            "platform": platform_name,
            "bridge_ok": QZONE_BRIDGE_OK,
            "bridge_error": QZONE_BRIDGE_ERR[:300],
            "version": "",
            "cookie_ok": False,
            "cookie_error": "",
            "cookie_count": 0,
            "uin": 0,
        }
        if bot is None:
            result["ok"] = False
            result["error"] = "没有可用的协议端 bot 实例"
            return json_response(result)
        try:
            info = await asyncio.wait_for(bot.call_action("get_version_info"), timeout=10)
            if isinstance(info, dict):
                result["version"] = (
                    f"{info.get('app_name') or ''} {info.get('app_version') or ''}".strip()
                )
        except Exception:  # noqa: BLE001
            pass
        if QZONE_BRIDGE_OK:
            try:
                domain = str(self._conf.get("qzone_cookie_domain") or "qzone.qq.com")
                cookie_text = await asyncio.wait_for(
                    fetch_cookie_text(bot, domain=domain), timeout=30
                )
                cookies = parse_cookie_text(cookie_text or "")
                result["cookie_count"] = len(cookies)
                result["cookie_ok"] = bool(cookies)
                if not cookies:
                    result["cookie_error"] = "Cookie 为空（协议端可能不支持 get_cookies）"
                else:
                    try:
                        result["uin"] = int(await fetch_login_uin(bot) or 0)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                result["cookie_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return json_response(result)

