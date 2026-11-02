# -*- coding: utf-8 -*-
"""comfy 纯模块的离线测试（盲审 P2-5）：quota / sizing / tts。

这三块都不依赖 AstrBot，是最适合做 pytest 的；这里用统一风格（PASS/FAIL + 退出码）。
"""

from __future__ import annotations

import sys

COMFY = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy"
sys.path.insert(0, COMFY)

import quota  # noqa: E402
import sizing  # noqa: E402
import tts  # noqa: E402

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


def main() -> int:
    print("=" * 70)
    print("comfy 纯模块离线测试（quota / sizing / tts）")
    print("=" * 70)

    def t_quota_defaults():
        c = quota.QuotaConfig()
        assert c.counted_jobs_limit == 70, c.counted_jobs_limit
        assert c.global_limit == 30, c.global_limit
        assert c.ban_after == 70, c.ban_after
        return f"global_limit={c.global_limit} ban_after={c.ban_after} counted_limit={c.counted_jobs_limit}"

    check("quota 默认值与 schema 一致", t_quota_defaults)

    def t_quota_window():
        # 真实签名：window_start(ts, hour)
        start = quota.window_start(1000000.0, 8)
        assert isinstance(start, int) and start > 0, start
        for bad in (24, -1):
            try:
                quota.window_start(1000000.0, bad)
            except ValueError:
                continue
            raise AssertionError(f"window_start(ts, {bad}) 没有报错")
        return "合法 hour 返回窗口起点，24/-1 抛 ValueError（故配置层要 clamp）"

    check("quota window_start 越界行为", t_quota_window)

    def t_sizing_fit():
        assert sizing.fit_within(100, 100, 1920, 2764800) == (96, 96), sizing.fit_within(100, 100, 1920, 2764800)
        assert sizing.fit_within(4000, 3000, 1920, 2764800) == (1920, 1440)
        w, h = sizing.fit_within(1234, 987, 1920, 2764800)
        assert w % 8 == 0 and h % 8 == 0, (w, h)
        return "100×100→96×96（不放大）· 4000×3000→1920×1440 · 对齐 8"

    check("sizing.fit_within 只缩不放", t_sizing_fit)

    def t_tts_pause():
        out = tts.normalize_pauses("A……B")
        assert "-" in out and out.count("\n") >= 2, out
        assert "……" not in out, out
        return "省略号 → 停顿标记（独占一行）"

    check("tts.normalize_pauses 生成停顿", t_tts_pause)

    def t_tts_blank():
        assert tts.normalize_pauses("A\n\nB") == "A\nB", repr(tts.normalize_pauses("A\n\nB"))
        return "连续空行被压成一行（死正则已修）"

    check("tts 空行压缩", t_tts_blank)

    ok = sum(1 for _, p, _ in results if p)
    bad = [r for r in results if not r[1]]
    print()
    print(f"汇总：{ok}/{len(results)} 通过" + ("" if not bad else f"，{len(bad)} 项失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
