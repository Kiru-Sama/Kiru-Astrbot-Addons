# -*- coding: utf-8 -*-
"""NCD 源码补丁管理：把「改 AstrBot 源码」这件事收进一个插件里统一管。

为什么要单独一个插件（2026-10-19 整理）：
  · 补丁以前散在监控插件里，谁都不知道到底打了没打、打完是什么状态；
  · AstrBot 一升级，补丁就静默失效，出问题很难归因；
  · 这里只做【状态自检 + 手动应用/回滚】，绝不自动打补丁 ——
    自动打会把升级后的新文件改坏。

工具（都只给管理员）：
  patch_status()        列出每个补丁：未应用 / 已应用 / 版本不匹配
  patch_apply(id)       打上（git apply，先 --check）
  patch_revert(id)      回滚（git apply -R，先 --check）
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

PLUGIN_DIR = Path(__file__).resolve().parent
PATCH_DIR = PLUGIN_DIR / "patches"
META_FILE = PATCH_DIR / "校验值.json"

# AstrBot 包根目录：<AstrBot>\data\plugins\<本插件>\main.py 往上三层
ASTRBOT_ROOT = PLUGIN_DIR.parent.parent.parent


def _find_pkg_dir(root: Path) -> Path:
    """兼容 Windows/Linux/macOS 的 venv 目录布局。"""
    windows = root / ".venv" / "Lib" / "site-packages" / "astrbot"
    candidates = [windows]
    lib = root / ".venv" / "lib"
    if lib.is_dir():
        candidates.extend(sorted(lib.glob("python*/site-packages/astrbot")))
    for path in candidates:
        if path.is_dir():
            return path
    return windows


PKG_DIR = _find_pkg_dir(ASTRBOT_ROOT)


def sha16(path: Path) -> str:
    """前 16 位 SHA256，和校验表里的写法一致。"""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_meta() -> list[dict[str, Any]]:
    if not META_FILE.is_file():
        return []
    try:
        data = json.loads(META_FILE.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[patch] 读校验值.json 失败: {exc}")
        return []


def state_of(item: dict[str, Any]) -> tuple[str, str]:
    """返回 (状态, 说明)。状态：applied / clean / drifted / missing"""
    rel = str(item.get("rel") or "")
    target = PKG_DIR / rel
    if not target.is_file():
        return "missing", f"目标文件不存在：{rel}"
    cur = sha16(target)
    if cur == item.get("sha_after"):
        return "applied", "已应用"
    if cur == item.get("sha_before"):
        return "clean", "未应用"
    return "drifted", f"版本不匹配（当前 {cur}）——AstrBot 可能升级过，别硬打"


def git_apply(patch: Path, revert: bool, check_only: bool = False) -> tuple[bool, str]:
    """在 site-packages\astrbot 下执行 git apply（-p1）。"""
    cmd = ["git", "apply", "-p1"]
    if revert:
        cmd.append("-R")
    if check_only:
        cmd.append("--check")
    cmd.append(str(patch))
    try:
        r = subprocess.run(
            cmd, cwd=str(PKG_DIR), capture_output=True, text=True, timeout=60
        )
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out.strip()
    except FileNotFoundError:
        return False, "找不到 git（请确认 PATH 里有 git）"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


class NcdPatchPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self._conf = config or {}
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        """启动时只做只读自检并写日志，绝不自动应用补丁。"""
        items = load_meta()
        if not items:
            logger.info("[patch] 没有补丁清单（patches/校验值.json）")
            return
        counts: dict[str, int] = {}
        for it in items:
            st, _ = state_of(it)
            counts[st] = counts.get(st, 0) + 1
        logger.info(
            f"[patch] 补丁状态自检：共 {len(items)} 个 · "
            f"已应用 {counts.get('applied', 0)} · 未应用 {counts.get('clean', 0)} · "
            f"版本不匹配 {counts.get('drifted', 0)} · 目标缺失 {counts.get('missing', 0)}"
        )

    @staticmethod
    def _admin(event: AstrMessageEvent) -> bool:
        return str(getattr(event, "role", "")) == "admin"

    @filter.llm_tool(name="patch_status")
    async def patch_status(self, event: AstrMessageEvent) -> str:
        """列出源码补丁的状态（只读）。"""
        if not self._admin(event):
            return "只有管理员能查看补丁状态。"
        items = load_meta()
        if not items:
            return "没有补丁清单：patches/校验值.json 不存在或为空。"
        lines = [f"补丁目录：{PATCH_DIR}", f"目标包：{PKG_DIR}", ""]
        for it in items:
            st, why = state_of(it)
            mark = {"applied": "✅ 已应用", "clean": "⬜ 未应用",
                    "drifted": "⚠️ 版本不匹配", "missing": "❌ 目标缺失"}[st]
            lines.append(f"· {it.get('id')} {it.get('title')} —— {mark}（{why}）")
        lines.append("")
        lines.append("要打上：patch_apply(id)；要回滚：patch_revert(id)。")
        return "\n".join(lines)

    def _find(self, pid: str) -> dict[str, Any] | None:
        for it in load_meta():
            if str(it.get("id")).lower() == str(pid).strip().lower():
                return it
        return None

    @filter.llm_tool(name="patch_apply")
    async def patch_apply(self, event: AstrMessageEvent, id: str = "") -> str:
        """打上一个源码补丁（管理员；会先做 --check）。"""
        if not self._admin(event):
            return "只有管理员能改源码补丁。"
        item = self._find(id)
        if not item:
            return f"没有这个补丁：{id}。先调 patch_status 看有哪些。"
        st, why = state_of(item)
        if st == "applied":
            return f"{item['id']} 已经是应用状态，不用再打。"
        if st == "drifted":
            return f"{item['id']} 不能打：{why}。请先确认 AstrBot 版本，重新生成补丁。"
        patch = PATCH_DIR / str(item.get("patch"))
        if not patch.is_file():
            return f"补丁文件不存在：{patch}"
        ok, out = await asyncio.to_thread(
            git_apply, patch, revert=False, check_only=True
        )
        if not ok:
            return f"{item['id']} 预检失败（没有改动任何文件）：{out}"
        ok, out = await asyncio.to_thread(git_apply, patch, revert=False)
        after = state_of({**item, "sha_before": ""})[0]
        return (
            f"{item['id']} 已应用。" if ok else f"{item['id']} 应用失败：{out}"
        ) + (f"\n状态：{after}" if ok else "")

    @filter.llm_tool(name="patch_revert")
    async def patch_revert(self, event: AstrMessageEvent, id: str = "") -> str:
        """回滚一个源码补丁（管理员；会先做 --check）。"""
        if not self._admin(event):
            return "只有管理员能改源码补丁。"
        item = self._find(id)
        if not item:
            return f"没有这个补丁：{id}。先调 patch_status 看有哪些。"
        st, why = state_of(item)
        if st == "clean":
            return f"{item['id']} 本来就是未应用状态，不用回滚。"
        if st != "applied":
            return f"{item['id']} 不能回滚：{why}。"
        patch = PATCH_DIR / str(item.get("patch"))
        if not patch.is_file():
            return f"补丁文件不存在：{patch}"
        ok, out = await asyncio.to_thread(
            git_apply, patch, revert=True, check_only=True
        )
        if not ok:
            return f"{item['id']} 预检失败（没有改动任何文件）：{out}"
        ok, out = await asyncio.to_thread(git_apply, patch, revert=True)
        return f"{item['id']} 已回滚。" if ok else f"{item['id']} 回滚失败：{out}"
