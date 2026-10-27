# -*- coding: utf-8 -*-
"""本地提示词融合查询库：标签库 + 法典。

设计：
- 标签库读「本地提示词库查询项目/词库」的 pkl/tsv，不依赖它的脚本输出。
- 法典读 comfy 的 sfw.txt / nsfw.txt，按“中文标题 + 邻近英文 tag 行”组块。
- 不做出图审核/NSFW 过滤：审核在出图链路做，这个库只负责查得到。
- 纯标准库，可直接被插件 import，也可命令行单跑。

数据形态（实测 2026-10-27）：
    idx2.pkl: tag -> (category, posts, aliases, zh_name, description)
      category: '0' 通用 / '1' 画师 / '3' 作品 / '4' 角色 / '5' meta
    aliases2.tsv: alias<TAB>canonical
    zh_groups.json: "中文分组" -> [[tag, ...], ...]
    codex/*.txt: 中文标题行 + 英文 tag 行交替；没有 markdown 标题。
"""

from __future__ import annotations

import json
import os
import pickle
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "CodexHit",
    "PromptLibrary",
    "QueryResult",
    "TagHit",
    "default_tag_root",
]


def default_tag_root() -> Path | None:
    """优先环境变量；其次工作区约定位置；找不到返回 None。"""
    env = os.environ.get("PROMPT_LIB_DIR", "").strip()
    if env:
        return Path(env)
    candidates = [
        Path(r"F:\Deepseek Harness WorkTable\本地提示词库\本地提示词库查询项目"),
    ]
    for path in candidates:
        if (path / "词库" / "idx2.pkl").is_file():
            return path
    return None


@dataclass
class TagHit:
    tag: str
    posts: int = 0
    category: str = ""
    zh: str = ""
    aliases: str = ""
    desc: str = ""
    score: int = 0

    def __str__(self) -> str:
        posts = f"{self.posts:,}" if self.posts else "0"
        parts = [f"{posts:>9}  {self.tag}"]
        if self.zh:
            parts.append(self.zh)
        if self.aliases:
            parts.append(f"别名={self.aliases}")
        if self.desc:
            parts.append(self.desc[:80])
        return "  ".join(parts)


@dataclass
class CodexHit:
    source: str
    line: int
    section: str
    text: str
    tags: str = ""
    score: int = 0

    def __str__(self) -> str:
        head = f"[{self.source}:{self.line}]"
        if self.section:
            head += f" {self.section}"
        return f"{head}\n  {self.text}\n  tags: {self.tags}" if self.tags else f"{head}\n  {self.text}"


@dataclass
class QueryResult:
    query: str
    characters: list[TagHit] = field(default_factory=list)
    tags: list[TagHit] = field(default_factory=list)
    templates: list[CodexHit] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "characters": [asdict(x) for x in self.characters],
            "tags": [asdict(x) for x in self.tags],
            "templates": [asdict(x) for x in self.templates],
            "note": self.note,
        }


class PromptLibrary:
    """标签库 + 法典的统一查询入口。"""

    CATEGORY = {
        "0": "general",
        "1": "artist",
        "3": "series",
        "4": "character",
        "5": "meta",
    }

    def __init__(
        self,
        tag_root: str | Path | None = None,
        codex_dir: str | Path | None = None,
        *,
        load_codex: bool = True,
    ) -> None:
        root = Path(tag_root) if tag_root else default_tag_root()
        self.tag_root = root
        self.codex_dir = Path(codex_dir) if codex_dir else None
        self._idx: dict[str, tuple] | None = None
        self._aliases: dict[str, str] | None = None
        self._groups: dict[str, list] | None = None
        self._imp_rev: dict[str, list[str]] | None = None
        self._cooc: dict[str, list[tuple[int, str]]] | None = None
        self._tag_group: dict[str, tuple[str, str]] | None = None
        self._cat: dict[str, str] | None = None
        self._codex: dict[str, list[str]] = {}
        self._load_codex = load_codex

    # ------------------------------------------------------------------
    #  加载
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.tag_root and (self.tag_root / "词库" / "idx2.pkl").is_file())

    def _load_tags(self) -> dict[str, tuple]:
        if self._idx is not None:
            return self._idx
        if not self.available:
            self._idx = {}
            return self._idx
        path = self.tag_root / "词库" / "idx2.pkl"  # type: ignore[union-attr]
        try:
            with path.open("rb") as fh:
                raw = pickle.load(fh)
            self._idx = raw if isinstance(raw, dict) else {}
        except Exception as exc:  # noqa: BLE001
            print(f"[promptlib] 读 idx2.pkl 失败: {exc}", file=sys.stderr)
            self._idx = {}
        return self._idx

    def _load_aliases(self) -> dict[str, str]:
        if self._aliases is not None:
            return self._aliases
        self._aliases = {}
        if not self.available:
            return self._aliases
        path = self.tag_root / "词库" / "aliases2.tsv"  # type: ignore[union-attr]
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "\t" not in line:
                    continue
                alias, canon = line.split("\t", 1)
                alias, canon = alias.strip().lower(), canon.strip()
                if alias and canon:
                    self._aliases[alias] = canon
        except OSError:
            pass
        return self._aliases

    def _load_groups(self) -> dict[str, list]:
        if self._groups is not None:
            return self._groups
        self._groups = {}
        if not self.available:
            return self._groups
        path = self.tag_root / "词库" / "zh_groups.json"  # type: ignore[union-attr]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._groups = data
        except Exception:  # noqa: BLE001
            pass
        return self._groups

    def _load_codex_lines(self, cat: str) -> list[str]:
        if cat in self._codex:
            return self._codex[cat]
        lines: list[str] = []
        if self._load_codex and self.codex_dir:
            path = self.codex_dir / f"{cat}.txt"
            try:
                lines = path.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()
            except OSError:
                lines = []
        self._codex[cat] = lines
        return lines

    def _load_imp_rev(self) -> dict[str, list[str]]:
        """蕴含反向表：被哪些标签蕴含（角色 → 皮肤/变体）。"""
        if self._imp_rev is not None:
            return self._imp_rev
        self._imp_rev = {}
        if not self.available:
            return self._imp_rev
        path = self.tag_root / "词库" / "upstream" / "蕴含.json"  # type: ignore[union-attr]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return self._imp_rev
        for item in data or []:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            src, dst, status = str(item[0]), str(item[1]), str(item[2])
            if status != "active":
                continue
            self._imp_rev.setdefault(dst, []).append(src)
        return self._imp_rev

    def _load_cooc(self) -> dict[str, list[tuple[int, str]]]:
        if self._cooc is not None:
            return self._cooc
        self._cooc = {}
        if not self.available:
            return self._cooc
        path = self.tag_root / "词库" / "upstream" / "共现索引.pkl"  # type: ignore[union-attr]
        try:
            with path.open("rb") as fh:
                raw = pickle.load(fh)
            if isinstance(raw, dict):
                self._cooc = raw
        except Exception:  # noqa: BLE001
            pass
        return self._cooc

    def _load_cat(self) -> dict[str, str]:
        """tag -> 官方大类（cat.pkl）。"""
        if self._cat is not None:
            return self._cat
        self._cat = {}
        if not self.available:
            return self._cat
        path = self.tag_root / "词库" / "cat.pkl"  # type: ignore[union-attr]
        try:
            with path.open("rb") as fh:
                raw = pickle.load(fh)
            if isinstance(raw, dict):
                self._cat = {str(k): str(v) for k, v in raw.items()}
        except Exception:  # noqa: BLE001
            pass
        return self._cat

    def _load_tag_group(self) -> dict[str, tuple[str, str]]:
        """tag -> (官方组, 节)；优先细分节，跳过 (总) 总表。"""
        if self._tag_group is not None:
            return self._tag_group
        self._tag_group = {}
        if not self.available:
            return self._tag_group
        path = self.tag_root / "词库" / "upstream" / "tag_groups.json"  # type: ignore[union-attr]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return self._tag_group
        if not isinstance(data, dict):
            return self._tag_group
        for group, obj in data.items():
            sections = (obj or {}).get("sections") or {}
            if not isinstance(sections, dict):
                continue
            for section, tags in sections.items():
                if str(section) == "(总)":
                    continue
                for tag in tags or []:
                    self._tag_group.setdefault(str(tag), (str(group), str(section)))
        return self._tag_group

    # ------------------------------------------------------------------
    #  标签库
    # ------------------------------------------------------------------
    @staticmethod
    def _hit(tag: str, value: tuple, score: int) -> TagHit:
        try:
            category, posts, aliases, zh, desc = value
        except (TypeError, ValueError):
            category, posts, aliases, zh, desc = "", 0, "", "", ""
        return TagHit(
            tag=str(tag),
            posts=int(posts or 0),
            category=PromptLibrary.CATEGORY.get(str(category), str(category or "")),
            zh=str(zh or ""),
            aliases=str(aliases or ""),
            desc=str(desc or ""),
            score=score,
        )

    def search_tags(
        self,
        query: str,
        *,
        limit: int = 8,
        scope: str = "auto",
    ) -> tuple[list[TagHit], list[TagHit]]:
        """返回 (角色, 普通标签)。scope=auto/character/tag。"""
        q = str(query or "").strip()
        if not q:
            return [], []
        idx = self._load_tags()
        if not idx:
            return [], []
        aliases = self._load_aliases()
        terms = [x for x in re.split(r"[\s,，;；]+", q.lower()) if x]
        if not terms:
            terms = [q.lower()]
        qlow = q.lower()

        canon = aliases.get(qlow, "")
        scored: dict[str, int] = {}

        def add(tag: str, score: int) -> None:
            if tag in idx:
                scored[tag] = max(scored.get(tag, 0), score)

        if canon:
            add(canon, 120)
        for term in terms:
            if term in aliases:
                add(aliases[term], 110)
        # 中文分组名 → 组内标签
        groups = self._load_groups()
        group_terms = [t for t in terms if len(t) >= 2]
        if group_terms:
            for group, items in groups.items():
                if all(t in str(group).lower() for t in group_terms):
                    for item in items or []:
                        if isinstance(item, (list, tuple)) and item:
                            add(str(item[0]).strip(), 90)

        for tag, value in idx.items():
            low_tag = tag.lower()
            zh = str(value[3] or "") if len(value) > 3 else ""
            desc = str(value[4] or "") if len(value) > 4 else ""
            alias = str(value[2] or "") if len(value) > 2 else ""
            score = 0
            if low_tag == qlow:
                score = 130
            elif qlow in low_tag:
                score = 80 - min(len(low_tag) // 8, 20)
            if qlow and qlow in zh.lower():
                score = max(score, 70)
            if qlow and qlow in desc.lower():
                score = max(score, 50)
            if qlow and qlow in alias.lower():
                score = max(score, 60)
            for term in terms:
                if term and term in low_tag:
                    score = max(score, 45)
                if term and term in zh.lower():
                    score = max(score, 40)
            if score:
                add(tag, score)

        rows = [self._hit(t, idx[t], s) for t, s in scored.items()]
        # 角色优先单独一组
        chars = [r for r in rows if r.category == "character"]
        others = [r for r in rows if r.category != "character"]
        key = lambda r: (-r.score, -r.posts, r.tag)  # noqa: E731
        chars.sort(key=key)
        others.sort(key=key)
        scope = str(scope or "auto").lower()
        if scope in ("character", "char", "角色"):
            return chars[:limit], []
        if scope in ("tag", "tags", "标签"):
            return [], others[:limit]
        return chars[:limit], others[:limit]

    # ------------------------------------------------------------------
    #  角色详情（皮肤 / 头 / 身体 / 衣服）
    # ------------------------------------------------------------------
    HEAD_GROUPS = {
        "Hair",
        "Hair color",
        "Hair styles",
        "Eyes tags",
        "Ears tags",
        "Face tags",
        "Headwear",
        "Eyewear",
        "Makeup",
    }
    BODY_GROUPS = {
        "Body parts",
        "Breasts tags",
        "Feet",
        "Hands",
        "Ass",
    }
    CLOTHES_GROUPS = {
        "Attire",
        "Legwear",
        "Handwear",
        "Neck and neckwear",
        "Fashion style",
        "Footwear",
        "Uniforms",
    }

    def _profile_hit(self, tag: str, group: str = "", section: str = "") -> dict:
        idx = self._load_tags()
        value = idx.get(tag) or ("", 0, "", "", "")
        return {
            "tag": tag,
            "posts": int(value[1] or 0),
            "zh": str(value[3] or ""),
            "aliases": str(value[2] or ""),
            "desc": str(value[4] or ""),
            "group": group,
            "section": section,
        }

    def character_profile(self, tag: str, *, limit: int = 8, skin_limit: int = 12) -> dict | None:
        """角色详情：皮肤/变体 + 按官方组分的头/身体/衣服/其他。"""
        idx = self._load_tags()
        if not idx:
            return None
        raw = str(tag or "").strip().lower().replace(" ", "_")
        aliases = self._load_aliases()
        # 精确命中优先；别名只做后备（别名表有把正式 tag 指到变体的脏数据）。
        canon = raw
        value = idx.get(canon)
        if not value:
            canon = aliases.get(raw, raw)
            value = idx.get(canon)
        if not value:
            # 中文名/别名：用角色搜索兜底，取最匹配的角色 tag。
            chars, _ = self.search_tags(raw, limit=1, scope="character")
            if chars:
                canon = chars[0].tag
                value = idx.get(canon)
        if not value:
            return None
        category = self.CATEGORY.get(str(value[0]), str(value[0] or ""))
        if category != "character":
            return None

        # 皮肤/变体：什么意味着这个角色
        imp = self._load_imp_rev().get(canon, [])
        skins = sorted(
            (self._profile_hit(t) for t in imp if t in idx),
            key=lambda x: -x["posts"],
        )[:skin_limit]

        # 共现：按官方组分类；用 cat.pkl 大类兜底，避免 tag_groups 的跨组脏数据。
        groups = self._load_tag_group()
        cats = self._load_cat()
        buckets: dict[str, list[dict]] = {
            "head": [],
            "body": [],
            "clothes": [],
            "other": [],
        }
        for count, other in self._load_cooc().get(canon, []):
            if other == canon or other not in idx:
                continue
            group, section = groups.get(other, ("", ""))
            cat = cats.get(other, "")
            if cat in ("series", "character", "artist", "meta"):
                # tag_groups 有跨组脏数据；这些大类不显示错误组。
                group, section = "", ""
            section_low = section.lower()
            is_gaze = any(
                key in section_low for key in ("gaze", "expression", "smile", "viewer")
            )
            if group in self.HEAD_GROUPS and cat in ("feature", "attire") and not is_gaze:
                bucket = "head"
            elif group in self.BODY_GROUPS and cat in ("feature", "attire"):
                bucket = "body"
            elif cat == "attire" and group not in self.HEAD_GROUPS:
                bucket = "clothes"
            else:
                bucket = "other"
            if len(buckets[bucket]) < limit:
                item = self._profile_hit(other, group, section)
                item["cooc"] = int(count)
                item["cat"] = cat
                buckets[bucket].append(item)

        return {
            "character": canon,
            "posts": int(value[1] or 0),
            "zh": str(value[3] or ""),
            "aliases": str(value[2] or ""),
            "desc": str(value[4] or ""),
            "skins": skins,
            **buckets,
        }

    # ------------------------------------------------------------------
    #  法典
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_tag_line(line: str) -> bool:
        text = line.strip()
        if not text or len(text) > 400:
            return False
        if not re.search(r"[a-z]", text.lower()):
            return False
        if not re.search(r"[,，{}()\[\]:_]", text):
            return False
        zh = len(re.findall(r"[\u4e00-\u9fff]", text))
        return zh <= max(4, len(text) // 12)

    @staticmethod
    def _looks_like_heading(line: str) -> bool:
        text = line.strip()
        if not text or len(text) > 60:
            return False
        if text.startswith(("【", "[", "{")):
            return False
        if re.fullmatch(r"[\d\s.、．]+", text):
            return False
        return bool(re.search(r"[\u4e00-\u9fff]", text))

    def _section_for(self, lines: list[str], index: int) -> str:
        for pos in range(index, max(-1, index - 80), -1):
            line = lines[pos].strip()
            if not line:
                continue
            if self._looks_like_heading(line) and not self._looks_like_tag_line(line):
                return line[:60]
        return ""

    def search_codex(self, query: str, *, limit: int = 5) -> list[CodexHit]:
        q = str(query or "").strip()
        if not q:
            return []
        terms = [x for x in re.split(r"[\s,，;；]+", q.lower()) if x]
        if not terms:
            terms = [q.lower()]
        out: list[tuple[int, str, int]] = []
        for cat in ("sfw", "nsfw"):
            lines = self._load_codex_lines(cat)
            for i, line in enumerate(lines):
                low = line.lower()
                if not low.strip():
                    continue
                hits = sum(1 for term in terms if term in low)
                if not hits:
                    continue
                # 中文标题/含中文的行优先；纯 tag 行也保留
                zh = len(re.findall(r"[\u4e00-\u9fff]", line))
                score = hits * 10 + min(zh, 8)
                out.append((score, cat, i))
        out.sort(key=lambda x: -x[0])
        hits: list[CodexHit] = []
        used: set[tuple[str, int]] = set()
        for score, cat, i in out:
            if len(hits) >= limit:
                break
            lines = self._load_codex_lines(cat)
            # 去重：同一段（i-3..i+3）只出一次
            if any((cat, j) in used for j in range(max(0, i - 3), min(len(lines), i + 4))):
                continue
            for j in range(max(0, i - 3), min(len(lines), i + 4)):
                used.add((cat, j))
            text = lines[i].strip()[:400]
            tag_line = ""
            # 标题下面可能是空行/另一个标题；上下各找 3 行，优先向下。
            for j in list(range(i + 1, min(len(lines), i + 4))) + list(
                range(max(0, i - 3), i)
            ):
                if self._looks_like_tag_line(lines[j]):
                    tag_line = lines[j].strip()[:500]
                    break
            hits.append(
                CodexHit(
                    source=cat,
                    line=i + 1,
                    section=self._section_for(lines, i),
                    text=text,
                    tags=tag_line,
                    score=score,
                )
            )
        return hits

    # ------------------------------------------------------------------
    #  统一查询
    # ------------------------------------------------------------------
    def query(
        self,
        query: str,
        *,
        limit: int = 8,
        codex_limit: int = 5,
        scope: str = "auto",
        fallback_codex: bool = True,
    ) -> QueryResult:
        q = str(query or "").strip()
        if not q:
            return QueryResult(query="", note="查询词为空")
        scope = str(scope or "auto").lower()
        if scope in ("codex", "template", "templates", "法典"):
            return QueryResult(
                query=q,
                templates=self.search_codex(q, limit=codex_limit),
                note="" if self._load_codex_lines("sfw") or self._load_codex_lines("nsfw") else "法典不可用",
            )
        chars, tags = self.search_tags(q, limit=limit, scope=scope)
        templates: list[CodexHit] = []
        # 查词默认只出标签；标签完全没命中才用法典兜底。
        if fallback_codex and not chars and not tags:
            templates = self.search_codex(q, limit=codex_limit)
        note = ""
        if not self.available:
            note = "标签库不可用（找不到 idx2.pkl），只返回法典结果"
        elif not chars and not tags and not templates:
            note = "标签库和法典都没命中，换个关键词或查别名"
        return QueryResult(
            query=q,
            characters=chars,
            tags=tags,
            templates=templates,
            note=note,
        )


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="本地提示词融合查询：标签库 + 法典")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--scope", default="auto", choices=["auto", "tag", "character", "codex", "profile"])
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--codex-limit", type=int, default=5)
    ap.add_argument("--tag-root", default="")
    ap.add_argument("--codex-dir", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    lib = PromptLibrary(
        tag_root=args.tag_root or None,
        codex_dir=args.codex_dir or None,
    )
    if args.scope == "profile":
        profile = lib.character_profile(
            " ".join(args.query), limit=args.limit, skin_limit=12
        )
        if not profile:
            print(f"「{' '.join(args.query)}」不是角色 tag，或标签库里没有。")
            return 1
        if args.json:
            print(json.dumps(profile, ensure_ascii=False, indent=2))
            return 0
        print(f"角色：{profile['character']}  {profile['zh']}  {profile['posts']:,} 帖")
        if profile.get("desc"):
            print(f"释义：{profile['desc']}")
        for key, label in (
            ("skins", "皮肤/变体"),
            ("head", "头/脸"),
            ("body", "身体"),
            ("clothes", "衣服"),
            ("other", "其他共现"),
        ):
            rows = profile.get(key) or []
            if not rows:
                continue
            print(f"\n【{label}】")
            for row in rows:
                extra = f"  [{row.get('group')}/{row.get('section')}]" if row.get("group") else ""
                print(f"  {row['posts']:>9,}  {row['tag']}  {row['zh']}{extra}")
        return 0
    if args.scope == "codex":
        result = QueryResult(
            query=" ".join(args.query),
            templates=lib.search_codex(" ".join(args.query), limit=args.codex_limit),
        )
    else:
        result = lib.query(
            " ".join(args.query),
            limit=args.limit,
            codex_limit=args.codex_limit,
            scope=args.scope,
        )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print(f"查询：{result.query}")
    if result.note:
        print(f"提示：{result.note}")
    if result.characters:
        print("\n【角色】")
        for row in result.characters:
            print(" ", row)
    if result.tags:
        print("\n【标签】")
        for row in result.tags:
            print(" ", row)
    if result.templates:
        print("\n【法典】")
        for row in result.templates:
            print(" ", row)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
