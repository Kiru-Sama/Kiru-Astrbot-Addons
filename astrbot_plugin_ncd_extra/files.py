# -*- coding: utf-8 -*-
"""附件读取：把 QQ 里引用的文件下载下来、按类型抽成文字。

为什么单独一个模块：
  · 解析逻辑（txt/pdf/docx/xlsx）和插件主流程无关，单独拆出来能离线单测；
  · 依赖只在真正读文件时导入，没装也不会拖慢插件启动。
"""

from __future__ import annotations

import os
import re
import tempfile
import urllib.request
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024  # 单文件上限 20 MB

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv", ".log",
    ".py", ".js", ".ts", ".html", ".htm", ".xml", ".yaml", ".yml", ".ini",
    ".toml", ".conf", ".sh", ".bat", ".ps1", ".c", ".h", ".cpp", ".java",
}


class FileError(Exception):
    """可预期的读取失败（给模型看的人话）。"""


def find_file_component(comps, depth: int = 0):
    """在当前消息（含被引用消息的 chain）里找第一个文件组件。"""
    if not comps or depth > 3:
        return None
    for comp in comps:
        # 引用消息：先往下钻
        chain = getattr(comp, "chain", None)
        if chain:
            hit = find_file_component(chain, depth + 1)
            if hit is not None:
                return hit
            continue
        # 鸭子类型：有 name，且至少带 url 或本地 file
        if hasattr(comp, "name") and (
            getattr(comp, "url", "") or getattr(comp, "file", "")
        ):
            return comp
    return None


def safe_name(name: str) -> str:
    """去掉路径分隔符，只留文件名。"""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r"[^\w.\-()（）【】\u4e00-\u9fff ]+", "_", base).strip()
    return base or "attachment"


def download(url: str, dest_dir: Path, max_bytes: int = MAX_BYTES) -> Path:
    """把文件下到临时目录。QQ 的文件 url 可能带签名，直接 GET 即可。"""
    if not url:
        raise FileError("这个文件没有下载地址（可能只有文件名）。")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / safe_name(os.path.basename(url.split("?")[0]) or "attachment")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(max_bytes + 1)
    except Exception as exc:  # noqa: BLE001
        raise FileError(f"下载失败：{type(exc).__name__}: {exc}") from exc
    if len(data) > max_bytes:
        raise FileError(f"文件超过 {max_bytes // 1024 // 1024} MB，太大了。")
    if not data:
        raise FileError("下载到的是空文件。")
    target.write_bytes(data)
    return target


def read_text_file(path: Path, limit: int = 6000) -> str:
    raw = path.read_bytes()[: 2 * 1024 * 1024]
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        try:
            import chardet  # type: ignore

            text = raw.decode(chardet.detect(raw).get("encoding") or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = raw.decode("utf-8", errors="replace")
    return text[:limit]


def read_pdf(path: Path, limit: int = 6000, max_pages: int = 8) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # noqa: BLE001
        raise FileError("没装 pypdf，暂时读不了 PDF。") from exc
    try:
        reader = PdfReader(str(path))
        parts = []
        for page in reader.pages[:max_pages]:
            parts.append(page.extract_text() or "")
        text = "\n".join(p for p in parts if p.strip())
    except Exception as exc:  # noqa: BLE001
        raise FileError(f"PDF 解析失败：{type(exc).__name__}: {exc}") from exc
    if not text.strip():
        raise FileError("这个 PDF 里没抽到文字（可能是扫描件/图片版）。")
    return text[:limit]


def read_docx(path: Path, limit: int = 6000) -> str:
    try:
        import docx  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise FileError("没装 python-docx，暂时读不了 Word。") from exc
    try:
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs if p.text and p.text.strip()]
        for table in d.tables[:5]:
            for row in table.rows[:30]:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        raise FileError(f"Word 解析失败：{type(exc).__name__}: {exc}") from exc
    if not text.strip():
        raise FileError("这个 Word 里没抽到文字。")
    return text[:limit]


def read_xlsx(path: Path, limit: int = 6000) -> str:
    try:
        import openpyxl  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise FileError("没装 openpyxl，暂时读不了 Excel。") from exc
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets[:3]:
            parts.append(f"【工作表 {ws.title}】")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 50:
                    parts.append("……（后面的行省略）")
                    break
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        raise FileError(f"Excel 解析失败：{type(exc).__name__}: {exc}") from exc
    if not text.strip():
        raise FileError("这个 Excel 里没抽到内容。")
    return text[:limit]


def _zip_text(path: Path, limit: int = 200000) -> str:
    """ZIP 容器（docx/xlsx/pptx/自定义打包）里把 XML/文本挑出来。"""
    import zipfile

    parts: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = [
                n for n in zf.namelist()
                if n.lower().endswith((".xml", ".txt", ".md", ".json", ".csv"))
            ]
            for name in names[:40]:
                try:
                    raw = zf.read(name)[:500000]
                    text = raw.decode("utf-8", errors="ignore")
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s{2,}", " ", text).strip()
                    if text:
                        parts.append(f"【{name}】\n{text}")
                except Exception:
                    continue
    except Exception:
        return ""
    return "\n\n".join(parts)[:limit]


def _strings_text(path: Path, limit: int = 200000) -> str:
    """二进制兜底：把可读的连续文本片段抠出来（类似 strings）。"""
    raw = path.read_bytes()[: 8 * 1024 * 1024]
    text = raw.decode("utf-8", errors="ignore")
    chunks = re.findall(
        r"[\u4e00-\u9fffA-Za-z0-9\s，。！？、：；\"'（）()【】\-_.:/@#%&+*=]{4,}", text
    )
    cleaned = []
    for c in chunks:
        c = c.strip()
        if len(c) >= 4 and any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in c):
            cleaned.append(c)
    return "\n".join(cleaned)[:limit]


def media_info(path: Path) -> str:
    """用 ffmpeg 看是不是音视频容器，返回人话；不是就返回空串。"""
    import shutil
    import subprocess

    ff = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ff:
        return ""
    try:
        proc = subprocess.run(
            [ff, "-hide_banner", "-i", str(path)],
            capture_output=True,
            timeout=20,
            text=True,
            errors="replace",
        )
    except Exception:
        return ""
    err = proc.stderr or ""
    if "Duration:" not in err and "Stream #" not in err:
        return ""
    parts = []
    dur = re.search(r"Duration:\s*([0-9:.]+)", err)
    if dur:
        parts.append(f"时长 {dur.group(1)}")
    kinds = re.findall(r"Stream #\d+:\d+.*?: (Audio|Video|Subtitle)", err)
    if kinds:
        parts.append("含 " + "/".join(sorted(set(kinds))))
    return "，".join(parts)


def extract(path: Path, limit: int = 6000, offset: int = 0) -> str:
    """按扩展名抽文字；不认识的格式走兜底（ZIP 容器 → 可读片段）。"""
    text = _extract_all(path)
    start = max(0, int(offset or 0))
    return text[start:start + limit]


def _extract_all(path: Path, limit: int = 10**9) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return read_text_file(path, limit)
    if ext == ".pdf":
        return read_pdf(path, limit)
    if ext == ".docx":
        return read_docx(path, limit)
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx(path, limit)
    if ext == ".xls":
        try:
            import xlrd  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise FileError("没装 xlrd，暂时读不了旧版 .xls。") from exc
        try:
            book = xlrd.open_workbook(str(path))
            parts = []
            for sh in book.sheets()[:3]:
                parts.append(f"【工作表 {sh.name}】")
                for r in range(min(sh.nrows, 50)):
                    cells = [str(sh.cell_value(r, c)) for c in range(sh.ncols)]
                    parts.append(" | ".join(cells))
            return "\n".join(parts)[:limit]
        except Exception as exc:  # noqa: BLE001
            raise FileError(f"xls 解析失败：{type(exc).__name__}: {exc}") from exc
    # ---- 兜底：不认识的格式也尽量给点东西 ----
    try:
        head = path.read_bytes()[:2]
    except Exception:
        head = b""
    if head == b"PK":
        text = _zip_text(path)
        if text.strip():
            return f"〔按 ZIP 容器解出的文本〕\n{text}"
    info = media_info(path)
    if info:
        raise FileError(
            f"这是音视频文件（{info}），没法直接转成文字。\n"
            "要读内容需要语音识别（STT），当前没开；可以让对方导出 mp3/wav 再发，或给文字版。"
        )
    text = _strings_text(path)
    if text.strip():
        return f"〔从二进制里抠出的可读片段，可能不完整〕\n{text}"
    raise FileError(
        f"暂不支持 {ext or '这种'} 格式，也抠不出可读文本"
        "（能读：文本 / pdf / docx / xlsx / xls / zip 类）。"
    )


def temp_dir() -> Path:
    base = Path(tempfile.gettempdir()) / "ncd_attach"
    base.mkdir(parents=True, exist_ok=True)
    return base
