# -*- coding: utf-8 -*-
"""提示词/标签库检索（核心库默认加载，全量库按需加载）。

数据形态（2026-10-20 用户提供的两套）：
  kb/core/标签库-核心/*.txt       每行：tag ｜ 中文名 ｜ 帖数 ｜ 释义   （共 3.4 MB）
  kb/full/标签库/*/按分类/*.txt    同上，但收录角色/作品/画师/长尾（共 16 MB）
  kb/full/tags.csv / tags.jsonl   程序用全量（14 / 36 MB）—— 检索时跳过

检索策略：
  · 标签文件是「一行一条」，所以按【行】打分，直接返回命中的那几行（比整段返回精准得多）；
  · 中文名、帖数、释义都在同一行里，所以中文查询也能命中；
  · 核心库首次调用时建索引；全量库只在明确要 full 时才建（懒加载，平时不付 16 MB 扫描）；
  · 不联网、不依赖向量库。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

KB_DIR = Path(__file__).resolve().parent / "kb"
CORE_DIR = KB_DIR / "core"
FULL_DIR = KB_DIR / "full"

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9_()\-]{2,}")
_SKIP_SUFFIX = (".csv", ".jsonl", ".pkl")

SCOPES = ("core", "full")


@dataclass
class Line:
    file: str
    text: str
    line_no: int


@dataclass
class Index:
    scope: str
    lines: list[Line] = field(default_factory=list)
    df: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)

    def build(self, root: Path) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() in _SKIP_SUFFIX:
                continue
            if path.suffix.lower() not in (".txt", ".md"):
                continue
            rel = path.relative_to(KB_DIR).as_posix()
            self.files.append(rel)
            try:
                text = _read(path)
            except Exception:  # noqa: BLE001
                continue
            for i, raw in enumerate(text.splitlines(), 1):
                s = raw.strip()
                if not s or s.startswith("#") or s.startswith("---"):
                    continue
                if s.startswith(("格式：", "格式:", "数据源", "字段")):
                    continue
                self.lines.append(Line(file=rel, text=s, line_no=i))
        for ln in self.lines:
            for tok in set(tokens(ln.text)):
                self.df[tok] = self.df.get(tok, 0) + 1

    def search(self, query: str, limit: int = 8, max_chars: int = 600) -> list[Line]:
        toks = [t for t in set(tokens(query)) if t]
        if not toks or not self.lines:
            return []
        n = len(self.lines)
        scored: list[tuple[float, Line]] = []
        for ln in self.lines:
            low = ln.text.lower()
            score = 0.0
            for t in toks:
                if t in low:
                    score += math.log(1 + n / (1 + self.df.get(t, 0)))
            if score > 0:
                scored.append((score, ln))
        scored.sort(key=lambda x: x[0], reverse=True)

        out: list[Line] = []
        size = 0
        seen: set[str] = set()
        for _, ln in scored:
            if ln.text in seen:
                continue
            seen.add(ln.text)
            out.append(ln)
            size += len(ln.text)
            if len(out) >= max(1, limit) or size >= max_chars:
                break
        return out


def _read(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def tokens(text: str) -> list[str]:
    low = text.lower()
    toks = _WORD.findall(low)
    cjk = "".join(_CJK.findall(low))
    toks += [cjk[i:i + 2] for i in range(max(0, len(cjk) - 1))]
    return toks


_INDEXES: dict[str, Index] = {}


def index(scope: str = "core") -> Index:
    scope = scope if scope in SCOPES else "core"
    idx = _INDEXES.get(scope)
    if idx is None:
        idx = Index(scope=scope)
        idx.build(CORE_DIR if scope == "core" else FULL_DIR)
        _INDEXES[scope] = idx
    return idx


def has_full() -> bool:
    return FULL_DIR.is_dir() and any(FULL_DIR.rglob("*.txt"))


def render(
    query: str,
    scope: str = "core",
    limit: int = 8,
    max_chars: int = 600,
    catalog_limit: int = 20,
    allow_full: bool = True,
) -> str:
    query = (query or "").strip()
    scope = scope if scope in SCOPES else "core"
    if scope == "full" and (not allow_full or not has_full()):
        scope = "core"

    idx = index(scope)
    if not idx.lines:
        return f"标签库是空的（{scope}）：kb/{scope}/ 下没有可检索的 txt/md。"

    head = "核心库" if scope == "core" else "全量库"
    if not query:
        lines = [f"{head}：{len(idx.files)} 个文件 / {len(idx.lines):,} 行标签"]
        for rel in idx.files[:catalog_limit]:
            lines.append(f"· {rel}")
        if len(idx.files) > catalog_limit:
            lines.append(f"· …还有 {len(idx.files) - catalog_limit} 个文件")
        if has_full() and scope == "core":
            lines.append("")
            lines.append(
                '另有全量库（含角色/作品/画师/长尾）：核心库查不到时用 scope="full" 再查。'
            )
        lines.append("")
        lines.append('查具体标签：draw_kb_search(query="银发 / 姿势 / 哭泣 …")')
        return "\n".join(lines)

    hits = idx.search(query, limit=limit, max_chars=max_chars)
    if not hits:
        extra = ""
        if scope == "core" and allow_full and has_full():
            extra = '\n核心库里没有；如果确实需要，可以带 scope="full" 去全量库查一次。'
        return f"{head}里没有匹配「{query}」的标签。{extra}"

    lines = [f"{head}命中 {len(hits)} 条（query={query!r}）："]
    for ln in hits:
        lines.append(f"· {ln.text}    〔{ln.file}〕")
    if scope == "core" and allow_full and has_full():
        lines.append('（只在核心库查；要角色/作品/画师/长尾时加 scope="full"）')
    return "\n".join(lines)
