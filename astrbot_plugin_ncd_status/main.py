# -*- coding: utf-8 -*-
"""/设备状态 信息图的背景设置插件（独立入口）。

为什么单独做一个插件：
  AstrBot 的「扩展功能」菜单是按【插件】列出的，插件页面（pages/*）不会
  单独出现在菜单里。把它做成独立插件后，面板左侧「扩展功能」里就会出现
  「状态图背景」这一项，点开就是背景设置页。

数据与 monitor 插件共用同一份目录：
  <AstrBot>/data/plugin_data/ncd_monitor/status_bg/
      settings.json     {mode: builtin/single/random, current, blur, dim}
      *.jpg/png/webp    用户上传的背景图库
  monitor 渲染 /设备状态 时直接读这里，所以这边改完立刻生效。
"""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.request
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.web import json_response
from PIL import Image

PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_DIR_NAME = PLUGIN_DIR.name
# <AstrBot>/data/plugins/<插件>/main.py
#   PLUGIN_DIR.parents[0] = plugins, [1] = data, [2] = <AstrBot>
ASTRBOT_ROOT = PLUGIN_DIR.parents[2]
BG_DIR = ASTRBOT_ROOT / "data" / "plugin_data" / "ncd_monitor" / "status_bg"
SETTINGS = BG_DIR / "settings.json"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
MAX_UPLOAD = 15 * 1024 * 1024


def _settings_load() -> dict:
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _settings_save(data: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    # ★ 2026-11-19 修 M13：tmp 名带 pid/随机后缀，避免和 monitor 同时保存时互相踩
    import os as _os
    import uuid as _uuid

    tmp = SETTINGS.with_name(SETTINGS.name + f".{_os.getpid()}.{_uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SETTINGS)


def _files_list() -> list[dict]:
    out: list[dict] = []
    if not BG_DIR.is_dir():
        return out
    try:
        for f in sorted(BG_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                try:
                    st = f.stat()
                except OSError:
                    continue
                out.append(
                    {"name": f.name, "size": st.st_size, "mtime": st.st_mtime}
                )
    except OSError:
        pass
    return out


def _builtin_bg() -> Path:
    """内置默认背景由 monitor 插件提供，找不到就用纯色。"""
    return ASTRBOT_ROOT / "data" / "plugins" / "astrbot_plugin_ncd_monitor" / "assets" / "status_bg.jpg"


# ---------------------------------------------------------------------------
#  聊天里传图 → 背景图库
# ---------------------------------------------------------------------------
def _walk_image_bytes(comps, depth: int = 0) -> bytes | None:
    """在一组消息段里找第一张图（会钻进被引用消息的 chain 里）。"""
    if not comps or depth > 3:
        return None
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
                        return data[: 12 * 1024 * 1024]
        chain = getattr(c, "chain", None)
        if chain and not isinstance(c, Comp.Image):
            data = _walk_image_bytes(chain, depth + 1)
            if data:
                return data
    return None


def _extract_image_bytes(event: AstrMessageEvent) -> bytes | None:
    """取本条消息（或引用消息）里的图片字节，多路兜底。"""
    try:
        comps = list(event.get_messages() or [])
    except Exception:  # noqa: BLE001
        comps = []
    data = _walk_image_bytes(comps)
    if data:
        return data
    # 兜底：原始 OneBot 消息段里的 file / url
    try:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        segs = None
        if isinstance(raw, dict):
            segs = raw.get("message")
        elif raw is not None:
            segs = getattr(raw, "message", None)
        for seg in segs or []:
            d = seg if isinstance(seg, dict) else {}
            if str(d.get("type") or "") != "image":
                continue
            dd = d.get("data") or {}
            for cand in (dd.get("file"), dd.get("url")):
                cand = str(cand or "").strip()
                if not cand:
                    continue
                p = Path(cand)
                if p.is_file():
                    try:
                        return p.read_bytes()[: 12 * 1024 * 1024]
                    except OSError:
                        pass
                if cand.startswith("http"):
                    try:
                        req = urllib.request.Request(
                            cand, headers={"User-Agent": "Mozilla/5.0"}
                        )
                        with urllib.request.urlopen(req, timeout=20) as resp:
                            return resp.read(12 * 1024 * 1024)
                    except Exception:  # noqa: BLE001
                        pass
    except Exception:  # noqa: BLE001
        pass
    return None


def _safe_bg_name(name: str) -> str:
    """背景文件名清洗：只留安全字符 + 强制图片后缀。"""
    import re as _re

    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    base = _re.sub(r"[^0-9A-Za-z_.\-()\uff08\uff09\u4e00-\u9fff]", "_", base)
    if not base:
        base = "bg.jpg"
    if not base.lower().endswith(IMAGE_EXTS):
        base += ".jpg"
    return base


def _is_real_image(blob: bytes) -> bool:
    """字节是不是可解码图片（防非图片文件混进图库）。"""
    try:
        im = Image.open(io.BytesIO(blob))
        im.verify()
        return True
    except Exception:  # noqa: BLE001
        return False


def _save_bg_from_bytes(blob: bytes) -> str:
    """把聊天图片转成背景图存进图库（最长边压到 2048，JPEG）。"""
    im = Image.open(io.BytesIO(blob)).convert("RGB")
    if max(im.size) > 2048:
        im.thumbnail((2048, 2048), Image.LANCZOS)
    BG_DIR.mkdir(parents=True, exist_ok=True)
    # ★ 2026-11-19：加微秒避免同秒两次上传互相覆盖（Windows strftime 不支持 %f，用 datetime）
    from datetime import datetime as _dt

    name = "bg_" + _dt.now().strftime("%Y%m%d-%H%M%S-%f") + ".jpg"
    im.save(BG_DIR / name, format="JPEG", quality=88, optimize=True)
    return name


# ---------------------------------------------------------------------------
#  内容去重：只按「文件字节完全相同」判断（sha256）
#  ★ 2026-11-18：之前用感知哈希 dHash 自动跳过/删除，把同角色不同姿势的图
#    误判成重复（缩略图构图相似，dHash 距离很小）→ 误删用户图片。
#    现在只做字节级精确去重；dHash 保留但仅作相似提示，不参与自动删除。
# ---------------------------------------------------------------------------
def _sha256_bytes(blob: bytes) -> str:
    import hashlib

    return hashlib.sha256(blob).hexdigest()


def _sha256_file(path: Path) -> str | None:
    import hashlib

    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _dhash_image(im: Image.Image) -> int:
    im = im.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(im.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            left = px[row * 9 + col]
            right = px[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def _find_duplicate(blob: bytes | None = None, dhash: int | None = None) -> str | None:
    """图库里是否已有**字节完全相同**的图（sha256），返回文件名。"""
    if not blob:
        return None
    want = _sha256_bytes(blob)
    for f in _files_list():
        if _sha256_file(BG_DIR / f["name"]) == want:
            return f["name"]
    return None


class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf: dict = config or {}
        self._register_apis()

    # ------------------------------------------------------------------
    #  Web API（页面用）
    # ------------------------------------------------------------------
    def _register_apis(self) -> None:
        apis = (
            ("status-bg", self.api_status_bg, ["GET"], "状态图背景：当前设置 + 图库"),
            ("status-bg-save", self.api_status_bg_save, ["POST"], "状态图背景：保存设置"),
            ("status-bg-upload", self.api_status_bg_upload, ["POST"], "状态图背景：上传图片"),
            ("status-bg-delete", self.api_status_bg_delete, ["POST"], "状态图背景：删除图片"),
            ("status-bg-preview", self.api_status_bg_preview, ["GET"], "状态图背景：缩略图预览"),
        )
        for name, handler, methods, desc in apis:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_DIR_NAME}/ncd-status/{name}", handler, methods, desc
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ncd-status] 注册 {name} 失败: {exc}")
        logger.info(
            f"[ncd-status] 已注册 5 个 Web API，背景目录: {BG_DIR}"
        )

    async def api_status_bg(self, **kwargs):
        return json_response(
            {
                "ok": True,
                "settings": _settings_load(),
                "files": _files_list(),
                "builtin": True,
                "builtin_credit": "内置默认背景",
            }
        )

    async def api_status_bg_save(self, **kwargs):
        from astrbot.api.web import request as _req

        try:
            body = await _req.json({}) or {}
        except Exception:  # noqa: BLE001
            body = {}
        mode = str(body.get("mode") or "builtin").lower()
        if mode not in ("builtin", "single", "random"):
            mode = "builtin"
        data: dict = {"mode": mode}
        current = str(body.get("current") or "").strip()
        if current and "/" not in current and "\\" not in current:
            data["current"] = current
        for key, low, high in (("blur", 1.0, 40.0), ("dim", 0.0, 0.75)):
            if key in body:
                try:
                    val = float(body.get(key))
                except (TypeError, ValueError):
                    continue
                data[key] = max(low, min(high, val))
        try:
            _settings_save(data)
        except OSError as exc:
            return json_response({"ok": False, "error": f"保存失败: {exc}"})
        logger.info(f"[ncd-status] 背景设置已保存: {data}")
        return json_response({"ok": True, "settings": data})

    async def api_status_bg_upload(self, **kwargs):
        from astrbot.api.web import request as _req

        try:
            body = await _req.json({}) or {}
        except Exception:  # noqa: BLE001
            body = {}
        name = str(body.get("name") or "").strip()
        b64 = str(body.get("b64") or "")
        if not name or not b64:
            return json_response({"ok": False, "error": "缺少文件名或内容"})
        # ★ 2026-11-19 修 M6：文件名清洗 + 白名单 + 图片内容校验
        name = _safe_bg_name(name)
        try:
            blob = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return json_response({"ok": False, "error": "base64 解码失败"})
        if not _is_real_image(blob):
            return json_response({"ok": False, "error": "不是有效图片文件"})
        if len(blob) > MAX_UPLOAD:
            return json_response({"ok": False, "error": "图片超过 15MB"})
        dup = _find_duplicate(blob)
        if dup:
            return json_response(
                {"ok": True, "duplicate": True, "name": dup, "files": _files_list()}
            )
        try:
            BG_DIR.mkdir(parents=True, exist_ok=True)
            (BG_DIR / name).write_bytes(blob)
        except OSError as exc:
            return json_response({"ok": False, "error": f"写入失败: {exc}"})
        logger.info(f"[ncd-status] 背景已上传: {name}（{len(blob)} 字节）")
        return json_response({"ok": True, "name": name, "files": _files_list()})

    async def api_status_bg_delete(self, **kwargs):
        from astrbot.api.web import request as _req

        try:
            body = await _req.json({}) or {}
        except Exception:  # noqa: BLE001
            body = {}
        name = str(body.get("name") or "").strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return json_response({"ok": False, "error": "文件名不合法"})
        # ★ 2026-11-19 修 M6：只允许删除图库里真实存在的图片（防止删 settings.json 等）
        allowed = {f["name"] for f in _files_list()}
        if name not in allowed:
            return json_response({"ok": False, "error": "图库里没有这个文件"})
        try:
            (BG_DIR / name).unlink(missing_ok=True)
        except OSError as exc:
            return json_response({"ok": False, "error": str(exc)})
        # ★ 2026-11-19 修低危：删掉的正好是当前背景时，把设置回退到内置默认
        try:
            cur = _settings_load()
            if str(cur.get("current") or "") == name:
                cur["mode"] = "builtin"
                cur.pop("current", None)
                _settings_save(cur)
        except Exception:  # noqa: BLE001
            pass
        return json_response({"ok": True, "files": _files_list()})

    async def api_status_bg_preview(self, name: str = "", **kwargs):
        # ★ AstrBot 只把路径参数注入 handler，查询参数要从 request 里读
        #   （之前 name 永远空 → 所有预览都回退内置图，看起来全一样）
        if not name:
            try:
                from astrbot.api.web import request as _req

                qp = getattr(_req, "query", None)
                if qp is not None:
                    name = str(qp.get("name") or "")
            except Exception:  # noqa: BLE001
                pass
        safe = str(name or "").strip()
        builtin = _builtin_bg()
        if safe in ("", "builtin", "default", "内置", "内置默认"):
            if not builtin.is_file():
                return json_response({"ok": False, "error": "内置背景文件缺失"})
            path = builtin
        elif "/" in safe or "\\" in safe or safe.startswith("."):
            return json_response({"ok": False, "error": "文件名不合法"})
        else:
            path = BG_DIR / safe
            if not path.is_file():
                return json_response({"ok": False, "error": f"图库里没有这个文件: {safe}"})
        try:
            im = Image.open(path).convert("RGB")
            im.thumbnail((480, 480), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
            url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:  # noqa: BLE001
            return json_response({"ok": False, "error": str(exc)[:200]})
        return json_response({"ok": True, "name": path.name, "data_url": url})

    # ------------------------------------------------------------------
    #  指令：在聊天里直接查看/切换 /设备状态 的背景图
    # ------------------------------------------------------------------
    @filter.command("设备状态背景", alias={"设备状态背景图", "状态图背景设置"})
    async def cmd_bg_settings(self, event: AstrMessageEvent):
        """查看/切换 /设备状态 的背景图（改设置需管理员）"""
        raw = str(getattr(event, "message_str", "") or "").strip()
        args = ""
        for prefix in (
            "/设备状态背景图",
            "／设备状态背景图",
            "/设备状态背景",
            "／设备状态背景",
            "/状态图背景设置",
            "／状态图背景设置",
        ):
            if raw.startswith(prefix):
                args = raw[len(prefix) :].strip()
                break
        parts = [p for p in args.replace("\u3000", " ").split(" ") if p]
        head = parts[0].lower() if parts else ""
        rest = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        settings = _settings_load()
        files = _files_list()
        labels = {"builtin": "内置默认", "single": "指定一张", "random": "随机轮换"}
        mode = str(settings.get("mode") or "builtin")
        cur = str(settings.get("current") or "")

        def _admin() -> bool:
            try:
                return bool(event.is_admin())
            except Exception:  # noqa: BLE001
                return False

        usage = (
            "用法：/设备状态背景 内置 | 随机 | 指定 <文件名或序号> | 列表 | 删除 <文件名或序号>"
            " | 去重 | 清空\n"
            "传图：把图片和 /设备状态背景 上传 发在同一条消息里（或引用一张图再发）。"
        )

        if not head or head in ("状态", "status", "查看"):
            lines = [
                f"状态图背景：{labels.get(mode, mode)}"
                + (f"（{cur}）" if mode == "single" and cur else ""),
                f"图库：{len(files)} 张"
                + (f"（示例：{'、'.join(f['name'] for f in files[:5])}）" if files else ""),
            ]
            if not _admin():
                lines.append("改设置需要管理员；查看不受限。")
            lines.append(usage)
            yield event.plain_result("\n".join(lines))
            return

        if head in ("列表", "list", "ls"):
            if not files:
                yield event.plain_result("图库还是空的。可以在这里上传？暂时请在「状态图背景」页面里传。")
                return
            rows = [f"{i + 1}. {f['name']}" for i, f in enumerate(files)]
            more = "" if len(rows) <= 30 else f"\n…还有 {len(rows) - 30} 张"
            yield event.plain_result(
                f"图库（{len(files)} 张）：\n" + "\n".join(rows[:30]) + more
            )
            return

        # ★ 2026-11-19 修 H1：修改类操作（上传/去重/清空/内置/随机/指定/删除）必须管理员；
        #   之前「去重/清空」在权限检查之前执行 → 普通成员可清空图库。
        if not _admin():
            yield event.plain_result("改背景只有管理员能用（查看状态不限）。")
            return

        if head in ("上传", "传", "添加", "add", "upload"):
            if not _admin():
                yield event.plain_result("上传背景需要管理员。")
                return
            blob = _extract_image_bytes(event)
            if not blob:
                yield event.plain_result(
                    "没读到图片：把图片和这条指令发在同一条消息里"
                    "（或引用一张图，再发 /设备状态背景 上传）。"
                )
                return
            dup = _find_duplicate(blob)
            if dup:
                yield event.plain_result(f"这张图图库里已经有了：{dup}（没有重复保存）")
                return
            try:
                name = _save_bg_from_bytes(blob)
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"保存失败：{exc}")
                return
            files_now = _files_list()
            logger.info(f"[ncd-status] 聊天上传背景 {name}（{len(blob)} 字节）")
            yield event.plain_result(
                f"已添加背景 {name}（图库 {len(files_now)} 张）\n"
                f"切换过去：/设备状态背景 指定 {len(files_now)}"
            )
            return

        if head in ("去重", "dedup", "删除重复"):
            seen: set[str] = set()
            removed = 0
            for f in files:
                h = _sha256_file(BG_DIR / f["name"])
                if not h:
                    continue
                if h in seen:
                    try:
                        (BG_DIR / f["name"]).unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        pass
                else:
                    seen.add(h)
            files_now = _files_list()
            yield event.plain_result(
                f"去重完成：删除 {removed} 张完全相同的文件，图库剩 {len(files_now)} 张。"
            )
            return

        if head in ("清空", "clear", "全部删除"):
            removed = 0
            for f in files:
                try:
                    (BG_DIR / f["name"]).unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
            settings = _settings_load()
            settings["mode"] = "builtin"
            settings.pop("current", None)
            _settings_save(settings)
            yield event.plain_result(
                f"已清空图库（删除 {removed} 张），背景回到内置默认。"
            )
            return

        if not _admin():
            yield event.plain_result("改背景只有管理员能用（查看状态不限）。")
            return

        def _pick(target: str) -> dict | None:
            target = target.strip()
            if not target:
                return None
            if target.isdigit():
                idx = int(target) - 1
                if 0 <= idx < len(files):
                    return files[idx]
                return None
            for f in files:
                name = f["name"]
                if name == target:
                    return f
            for f in files:
                if target.lower() in f["name"].lower():
                    return f
            return None

        if head in ("内置", "builtin", "默认"):
            settings["mode"] = "builtin"
            settings.pop("current", None)
            _settings_save(settings)
            yield event.plain_result("已切换：内置默认背景")
            return

        if head in ("随机", "random", "轮换"):
            if not files:
                yield event.plain_result("图库是空的，先往「状态图背景」页面传几张图。")
                return
            settings["mode"] = "random"
            settings.pop("current", None)
            _settings_save(settings)
            yield event.plain_result(f"已切换：随机轮换（图库 {len(files)} 张）")
            return

        if head in ("指定", "single", "用", "选"):
            picked = _pick(rest)
            if not picked:
                yield event.plain_result(
                    f"没找到「{rest or '（空）'}」。发 /设备状态背景 列表 看看序号。"
                )
                return
            settings["mode"] = "single"
            settings["current"] = picked["name"]
            _settings_save(settings)
            yield event.plain_result(f"已切换：指定背景 {picked['name']}")
            return

        if head in ("删除", "del", "remove", "删"):
            picked = _pick(rest)
            if not picked:
                yield event.plain_result(f"没找到「{rest or '（空）'}」。")
                return
            try:
                (BG_DIR / picked["name"]).unlink(missing_ok=True)
            except OSError as exc:
                yield event.plain_result(f"删除失败：{exc}")
                return
            if str(settings.get("current") or "") == picked["name"]:
                settings["mode"] = "builtin"
                settings.pop("current", None)
                _settings_save(settings)
            yield event.plain_result(f"已删除背景 {picked['name']}")
            return

        yield event.plain_result(usage)
