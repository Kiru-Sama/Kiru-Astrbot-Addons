# -*- coding: utf-8 -*-
"""给 AstrBot 内置指令补上「群聊需管理员」的限制。

为什么需要这个补丁：
    AstrBot 的 /reset、/new 自带 `@filter.permission_type(SHARED_GROUP_ADMIN)`
    —— 群聊里要管理员、私聊不限制。但 /stop、/stats 没有任何权限过滤：
    群里任何人敲一下就能停掉别人的生成、或者看到本会话的 token 统计。
    这个补丁只给这两个指令补上同样的过滤器，其余指令不动
    （/help、/sid 必须留给所有人，否则群友拿不到自己的 ID 去加白名单）。

用法：
    python group_admin_commands.py --check      # 只看有没有打上（不改文件）
    python group_admin_commands.py              # 打补丁（幂等，重复跑不会叠加）
    python group_admin_commands.py --revert     # 撤掉补丁

    python group_admin_commands.py --root "F:\\NapCatDesk\\ASTRBOT"
        # 显式指定 AstrBot 根目录（默认自动往上找 site-packages/astrbot）

★ AstrBot 升级会覆盖 site-packages，升级后重跑一次即可；`--check` 适合放进启动脚本。
"""

from __future__ import annotations

import argparse
import difflib
import pathlib
import sys

TARGETS = ("stop", "stats")
PERM_LINE = "    @filter.permission_type(filter.PermissionType.SHARED_GROUP_ADMIN)\n"


def find_main(root: pathlib.Path) -> pathlib.Path:
    """找到 builtin_commands/main.py。"""
    candidates = [
        root / ".venv" / "Lib" / "site-packages" / "astrbot" / "builtin_stars"
        / "builtin_commands" / "main.py",
        root / "Lib" / "site-packages" / "astrbot" / "builtin_stars"
        / "builtin_commands" / "main.py",
    ]
    for p in root.glob("**/astrbot/builtin_stars/builtin_commands/main.py"):
        candidates.append(p)
    for p in candidates:
        if p.is_file():
            return p
    raise SystemExit(f"找不到 builtin_commands/main.py（root={root}）")


def analyse(text: str) -> dict[str, bool]:
    """每条目标指令当前是否已经有限权。"""
    lines = text.splitlines()
    status: dict[str, bool] = {}
    for name in TARGETS:
        marker = f'@filter.command("{name}")'
        idx = next((i for i, ln in enumerate(lines) if ln.strip() == marker), None)
        if idx is None:
            status[name] = False  # 找不到就算未打（下面 apply 时也不会动它）
            continue
        window = "\n".join(lines[max(0, idx - 3): idx + 1])
        status[name] = "permission_type" in window
    return status


def apply(text: str) -> tuple[str, list[str]]:
    """在目标指令的 @filter.command 上方插入权限过滤器。"""
    lines = text.splitlines(keepends=True)
    added: list[str] = []
    for name in TARGETS:
        marker = f'@filter.command("{name}")'
        idx = next(
            (i for i, ln in enumerate(lines) if ln.strip() == marker), None
        )
        if idx is None:
            print(f"  · 没找到指令 /{name}，跳过（AstrBot 版本可能变了）")
            continue
        if "permission_type" in "".join(lines[max(0, idx - 3): idx + 1]):
            continue
        lines.insert(idx, PERM_LINE)
        added.append(name)
    return "".join(lines), added


def revert(text: str) -> tuple[str, list[str]]:
    """只删掉我们自己插入的那行（紧挨着目标指令、且是 SHARED_GROUP_ADMIN）。"""
    lines = text.splitlines(keepends=True)
    removed: list[str] = []
    for name in TARGETS:
        marker = f'@filter.command("{name}")'
        idx = next((i for i, ln in enumerate(lines) if ln.strip() == marker), None)
        if idx is None or idx == 0:
            continue
        if lines[idx - 1] == PERM_LINE:
            del lines[idx - 1]
            removed.append(name)
    return "".join(lines), removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="", help="AstrBot 根目录（默认从脚本位置往上找）")
    ap.add_argument("--check", action="store_true", help="只检查，不改文件")
    ap.add_argument("--revert", action="store_true", help="撤掉补丁")
    ap.add_argument("--diff", action="store_true", help="打印 unified diff")
    args = ap.parse_args()

    root = pathlib.Path(args.root) if args.root else _guess_root()
    target = find_main(root)
    print(f"目标文件：{target}")
    text = target.read_text(encoding="utf-8")
    before = analyse(text)
    print("当前状态：" + "、".join(f"/{k}={'已限权' if v else '未限权'}" for k, v in before.items()))

    if args.check:
        ok = all(before.values())
        print("结论：" + ("全部已限权" if ok else "还有指令没限权（跑一次本脚本即可）"))
        return 0 if ok else 1

    if args.revert:
        new_text, removed = revert(text)
        if not removed:
            print("没有需要撤的（本来就未打补丁）")
            return 0
        target.write_text(new_text, encoding="utf-8")
        print("已撤销：" + "、".join("/" + n for n in removed))
        return 0

    new_text, added = apply(text)
    if not added:
        print("无需改动（补丁已在位，或找不到目标指令）")
        return 0
    if args.diff:
        print(
            "".join(
                difflib.unified_diff(
                    text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile="main.py(原)",
                    tofile="main.py(补丁后)",
                )
            )
        )
    target.write_text(new_text, encoding="utf-8")
    print("已给这些指令加上群聊管理员限制：" + "、".join("/" + n for n in added))
    print("★ 需要重启 AstrBot 才生效（Python 模块已加载在内存里）。")
    return 0


def _guess_root() -> pathlib.Path:
    """脚本放在 <AstrBot>/data/plugins/xxx/patches/ 时，往上找 AstrBot 根。"""
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "data" / "cmd_config.json").is_file():
            return parent
    return pathlib.Path.cwd()


if __name__ == "__main__":
    sys.exit(main())
