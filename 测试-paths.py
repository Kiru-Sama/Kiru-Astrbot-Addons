# -*- coding: utf-8 -*-
"""paths 的离线测试（monitor 拆分第三批）：在 tmp 假树上验证探测逻辑。"""

from __future__ import annotations

import os
import sys
import tempfile
import time

MON_DIR = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_monitor"
sys.path.insert(0, MON_DIR)

import paths  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, fn):
    try:
        detail = fn() or ""
        results.append((name, True, detail))
        print(f"  PASS  {name}  — {detail}")
    except AssertionError as exc:
        results.append((name, False, str(exc)))
        print(f"  FAIL  {name}\n        └─ {exc}")
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"  ERROR {name}\n        └─ {type(exc).__name__}: {exc}")


def touch(path: str, mtime: float | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def main() -> int:
    print("=" * 70)
    print("paths 行为测试（tmp 假树）")
    print("=" * 70)

    def t_detect():
        with tempfile.TemporaryDirectory() as tmp:
            astrbot = os.path.join(tmp, "ASTRBOT")
            os.makedirs(astrbot, exist_ok=True)
            assert paths.detect_snowluma_root(astrbot) == "", "不该凭空找到"
            sl = os.path.join(tmp, "DATA", "components", "SnowLuma")
            os.makedirs(sl, exist_ok=True)
            assert paths.detect_snowluma_root(astrbot) == sl, "没找到同级 DATA 布局"
        return "无 → 空串；造 DATA/components/SnowLuma → 命中"

    check("detect_snowluma_root 候选布局", t_detect)

    def t_identity_newest():
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "data", "111", "snowluma_identity.db")
            new = os.path.join(tmp, "data", "222", "snowluma_identity.db")
            now = time.time()
            touch(old, now - 3600)
            touch(new, now)
            got = paths.find_identity_db(tmp)
            assert got == new, f"应挑最新的 222，实际 {got}"
        return "两个库挑 mtime 更新的那个"

    check("find_identity_db 挑最新（换号不复用旧号）", t_identity_newest)

    def t_onebot_newest():
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "config", "onebot_111.json")
            b = os.path.join(tmp, "config", "onebot_222.json")
            now = time.time()
            touch(a, now)
            touch(b, now - 3600)
            got = paths.find_onebot_config(tmp)
            assert got == a, f"应挑最新的 111，实际 {got}"
        return "两个 onebot 配置挑 mtime 更新的那个"

    check("find_onebot_config 挑最新", t_onebot_newest)

    def t_first_match():
        with tempfile.TemporaryDirectory() as tmp:
            assert paths.first_match(tmp, "*.json") == "", "空目录应返回空串"
            p = os.path.join(tmp, "b.json")
            touch(p)
            assert paths.first_match(tmp, "*.json") == p
        return "空目录空串 / 有文件返回路径"

    check("first_match", t_first_match)

    def t_lm():
        assert paths.is_lm_unavailable("No available chat models are unavailable") is True
        assert paths.is_lm_unavailable("正常回复") is False
        assert paths.is_lm_unavailable("") is False
        assert paths.is_lm_unavailable(None) is False
        return "认得出兜底文案，普通文本/空值判 False"

    check("is_lm_unavailable", t_lm)

    ok = sum(1 for _, p, _ in results if p)
    bad = [r for r in results if not r[1]]
    print()
    print(f"汇总：{ok}/{len(results)} 通过" + ("" if not bad else f"，{len(bad)} 项失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
