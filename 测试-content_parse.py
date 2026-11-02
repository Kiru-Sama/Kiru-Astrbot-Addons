# -*- coding: utf-8 -*-
"""content_parse 的离线测试（monitor 拆分第二批）。"""

from __future__ import annotations

import sys

MON_DIR = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_monitor"
sys.path.insert(0, MON_DIR)

import content_parse as cp  # noqa: E402

results: list[tuple[str, bool, str]] = []
IMG = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy\data\e2e.png"
IMG2 = IMG.replace("e2e.png", "e2e2.png")


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
    print("content_parse 行为测试（monitor 拆分第二批）")
    print("=" * 70)

    def t_empty():
        assert cp.split_content("") == ("", "", [])
        assert cp.extract_images("") == ("", [])
        assert cp.tidy_text("") == ""
        return "空输入返回空三元组"

    check("空输入", t_empty)

    def t_both_images():
        raw = (
            f"看这张 [Image Attachment: path {IMG}]\n"
            f"还有引用 [Image Attachment in quoted message: path {IMG2}]\n"
            "正文在这儿"
        )
        text, images = cp.extract_images(raw)
        assert len(images) == 2, f"应识别 2 张，实际 {len(images)}"
        assert images[0]["q"] is False and images[1]["q"] is True, "q 标记错"
        assert images[0]["n"] == "e2e.png", images[0]["n"]
        assert "[Image Attachment" not in text, "标记没摘干净"
        assert "正文在这儿" in text, "正文被误删"
        return "直接图 + 引用图各 1，标记摘除，正文保留"

    check("extract_images 两种标记", t_both_images)

    def t_dedupe():
        raw = (
            f"[Image Attachment: path {IMG}]\n"
            f"[Image Attachment in quoted message: path {IMG}]"
        )
        _, images = cp.extract_images(raw)
        assert len(images) == 1, f"同路径应去重成 1，实际 {len(images)}"
        assert len(images[0]["k"]) == 16, images[0]["k"]
        return "同路径只登记一次（sha1 前 16 位）"

    check("extract_images 同路径去重", t_dedupe)

    def t_split():
        raw = (
            "[图片]\n"
            f"[Image Attachment: path {IMG}]\n"
            "<system_reminder>User ID: 100, Nickname: 某人</system_reminder>\n"
            "你好呀\n\n\n\n世界"
        )
        text, reminder, images = cp.split_content(raw)
        assert len(images) == 1, images
        assert reminder == "User ID: 100, Nickname: 某人", repr(reminder)
        assert "[图片]" not in text and "system_reminder" not in text
        assert "你好呀" in text and "世界" in text
        assert "\n\n\n" not in text, "连续空行没压掉"
        return f"正文={len(text)}字 提醒={reminder[:14]}… 图=1"

    check("split_content 三段分离", t_split)

    def t_blank():
        raw = "[图片]\n[Image Attachment: path /tmp/a.png]\n   \n\n"
        text, _, images = cp.split_content(raw)
        assert text == "", f"纯空白应清成空串，实际 {text!r}"
        assert len(images) == 1
        return "只有图/空白时正文为空串"

    check("split_content 空白正文", t_blank)

    def t_tidy():
        raw = "\n\n<system_reminder>提醒内容</system_reminder>\n\n\n正文"
        out = cp.tidy_text(raw)
        assert "📋 提醒内容" in out, out
        assert not out.startswith("\n"), "开头空白没压掉"
        assert "\n\n\n" not in out
        return "提醒转 📋 行 + 压空白"

    check("tidy_text", t_tidy)

    ok = sum(1 for _, p, _ in results if p)
    bad = [r for r in results if not r[1]]
    print()
    print(f"汇总：{ok}/{len(results)} 通过" + ("" if not bad else f"，{len(bad)} 项失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
