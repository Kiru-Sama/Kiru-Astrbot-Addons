# -*- coding: utf-8 -*-
"""把 AstrBot 的源码补丁 P1~P4 导出成标准 unified diff（.patch）。

★ 这个脚本只能在【做过补丁的那台机器】上跑 —— 它依赖 AstrBot 原地留的
  `.bak-before-xxx` 备份文件作为"修改前"的那一侧。别人拿到的是 .patch，
  不需要这个脚本。

为什么要生成 .patch 而不是让人照着文档手改：
    手改要"找到这段、把这几行换成那几行"，改错了不报错、只是行为悄悄不对。
    .patch 是**可校验**的：`git apply --check` 能提前告诉你打不上，
    打完还能用哈希比对确认结果一致。

用法：
    <AstrBot>\\.venv\\Scripts\\python.exe 生成源码补丁.py [输出目录]
    默认输出到：..\\astrbot_plugin_ncd_patch\\patches\\（2026-10-19 补丁拆成独立插件）
"""
import difflib
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# AstrBot 包根目录：插件在 <AstrBot>\data\plugins\<插件名>\，
# 所以从插件目录往上三层到 <AstrBot>\data，再跟上 .venv\Lib\site-packages\astrbot
ASTRBOT_DATA = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ASTRBOT_PKG = os.path.join(
    ASTRBOT_DATA, ".venv", "Lib", "site-packages", "astrbot"
)

# 一个补丁一项。rel 是相对 AstrBot 包根的路径（.patch 里就用它）。
SPECS = [
    {
        "id": "P1",
        "rel": "core/tools/cron_tools.py",
        "bak": "core/tools/cron_tools.py.bak-before-action-fix",
        "title": "定时任务参数兜底",
        "why": (
            "小模型（4B 级）调 future_task 时会漏传必填的 action 参数，"
            "于是它嘴上说'建好了'、实际什么都没建，日志里报 "
            "`action must be one of create, edit, delete, or list.`。"
            "补丁在 action 为空但给了 note/cron_expression/run_at 时按 create 处理。"
        ),
    },
    {
        "id": "P2",
        "rel": "builtin_stars/astrbot/main.py",
        "bak": "builtin_stars/astrbot/main.py.bak-before-poke-094824",
        "title": "戳一戳时按人设回应",
        "why": (
            "别人戳（QQ 的戳一戳）机器人时，事件是「空文本 + 一个 Poke 组件」："
            "唤醒路径要求 @ 或前缀（文本是空的），主动回复路径要求 "
            "Plain/Image/Json（Poke 不在列表里），两条都不通，所以毫无反应。"
            "补丁把「戳机器人自己」当成等价于「空 @」，并给它单独的提示词。"
        ),
    },
    {
        "id": "P3",
        "rel": "core/utils/media_utils.py",
        "bak": "core/utils/media_utils.py.bak-before-montage",
        "title": "动图取 3 帧（原版是 9 宫格）",
        "why": (
            "原版把动图拆成 3x3 共 9 帧拼成一张图，视觉 token 开销大且信息冗余。"
            "补丁改成 3 列 1 行 = 只取前 3 帧。"
        ),
    },
    {
        "id": "P4",
        "rel": "core/astr_main_agent.py",
        "bak": "core/astr_main_agent.py.bak-before-toolprompt-1011-1403",
        "title": "模型不支持工具时，不再往系统提示里塞工具提示",
        "why": (
            "原判据只看「有没有工具」，而 req.func_tool 非空是常态（人设未设工具、"
            "定时任务工具、SendMessageToUserTool 三条路都会喂饱它），跟模型能力无关。"
            "于是每轮都往人设末尾追加一段英文工具使用提示，纯 token 开销。"
            "补丁加上 `_provider_supports_modality(provider, 'tool_use')` 判据；"
            "modalities 为空列表视为「未配置」→ 保持原行为，不误伤老配置。"
        ),
    },
]


def sha16(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def make_patch(spec: dict) -> tuple[str, dict]:
    before_path = os.path.join(ASTRBOT_PKG, spec["bak"].replace("/", os.sep))
    after_path = os.path.join(ASTRBOT_PKG, spec["rel"].replace("/", os.sep))

    for p in (before_path, after_path):
        if not os.path.exists(p):
            raise FileNotFoundError(p)

    with open(before_path, encoding="utf-8") as f:
        before = f.readlines()
    with open(after_path, encoding="utf-8") as f:
        after = f.readlines()

    rel = spec["rel"]
    body = list(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=3,
        )
    )
    if not body:
        raise ValueError(f"{spec['id']}: 这个文件和备份一模一样，没有差异？")

    # ★ .patch 必须是【严格合法的补丁】—— 不能在 `diff --git` 和 `---` 之间
    #   塞注释，git apply 会直接拒收。所以标题/原因/校验值全部放到
    #   配套的「补丁说明.md」里，这里只留补丁本身。
    text = f"diff --git a/{rel} b/{rel}\n" + "".join(body)

    meta = {
        "id": spec["id"],
        "rel": rel,
        "title": spec["title"],
        "why": spec["why"],
        "fname": f"{spec['id']}-{os.path.basename(rel)}.patch",
        "sha_before": sha16(before_path),
        "sha_after": sha16(after_path),
        "added": sum(1 for l in body if l.startswith("+") and not l.startswith("+++")),
        "removed": sum(1 for l in body if l.startswith("-") and not l.startswith("---")),
        "bytes_before": os.path.getsize(before_path),
        "bytes_after": os.path.getsize(after_path),
    }
    return text, meta


def write_notes(out_dir: str, metas: list[dict]) -> str:
    """把「为什么改、改了哪、打完该是什么哈希」写成一份人看的说明。"""
    L = [
        "# 源码补丁说明（P1 ~ P4）",
        "",
        "这四个补丁改的是 **AstrBot 自己的源码文件**（不是配置、不是插件）。",
        "**AstrBot 一升级就会被覆盖，升级后要照这份重打一遍。**",
        "",
        "## 怎么打",
        "",
        "```powershell",
        "# 进到 AstrBot 的包目录（下面这个路径按自己的安装位置改）",
        "cd <AstrBot>\\\\.venv\\\\Lib\\\\site-packages\\\\astrbot",
        "",
        "# 先干跑一遍，确认都能打上（--check 不会改文件）",
        "git apply --check -p1 <.patch 所在目录>\\\\P1-cron_tools.py.patch",
        "git apply --check -p1 <.patch 所在目录>\\\\P2-main.py.patch",
        "git apply --check -p1 <.patch 所在目录>\\\\P3-media_utils.py.patch",
        "git apply --check -p1 <.patch 所在目录>\\\\P4-astr_main_agent.py.patch",
        "",
        "# 没问题再去掉 --check 真打",
        "git apply -p1 <.patch 所在目录>\\\\P4-astr_main_agent.py.patch",
        "```",
        "",
        "> 没有 git 也行，用 `patch -p1 < xxx.patch`。",
        "> **或者干脆照着下面每节的说明手改** —— 每个补丁都只有几行。",
        "> 打完必须**重启 AstrBot**（改的是 .py 源码，配置那种热重载不适用）。",
        "",
        "## 清单",
        "",
        "| 补丁 | 文件 | 作用 | 增/删 | 打完的 SHA256 前 16 位 |",
        "|---|---|---|---|---|",
    ]
    for m in metas:
        L.append(
            f"| {m['id']} | `{m['rel']}` | {m['title']} | "
            f"+{m['added']}/-{m['removed']} | `{m['sha_after']}` |"
        )

    L += [
        "",
        "> **打完怎么自查**：对每个文件算 SHA256 取前 16 位，应等于上表最后一列。",
        "> 对不上就是没打全（或者 AstrBot 升级后又把你覆盖了）。",
        "> 一行命令：",
        "> ```powershell",
        "> Get-FileHash <文件> -Algorithm SHA256 | % { $_.Hash.Substring(0,16).ToLower() }",
        "> ```",
        "",
    ]

    for m in metas:
        L += [
            f"## {m['id']}　{m['title']}",
            "",
            f"- **文件**：`{m['rel']}`",
            f"- **补丁**：`{m['fname']}`",
            f"- **改动量**：+{m['added']} / -{m['removed']} 行"
            f"（{m['bytes_before']} → {m['bytes_after']} 字节）",
            f"- **校验值**：打之前 `{m['sha_before']}` → 打之后 `{m['sha_after']}`",
            "",
            f"**为什么改**",
            "",
            m["why"],
            "",
        ]

    path = os.path.join(out_dir, "补丁说明.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "astrbot_plugin_ncd_patch", "patches")
    os.makedirs(out_dir, exist_ok=True)

    if not os.path.isdir(ASTRBOT_PKG):
        print(f"找不到 AstrBot 包目录：{ASTRBOT_PKG}")
        print("（这个脚本必须在做过补丁的那台机器上跑）")
        return 1

    metas = []
    for spec in SPECS:
        try:
            text, meta = make_patch(spec)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {spec['id']} {spec['title']}: {type(exc).__name__}: {exc}")
            return 1
        # 文件名用 ASCII，避免不同系统/终端下乱码
        with open(os.path.join(out_dir, meta["fname"]), "w", encoding="utf-8", newline="") as f:
            f.write(text)
        metas.append(meta)
        print(
            f"[OK]   {meta['id']}  {meta['fname']:28s} "
            f"+{meta['added']}/-{meta['removed']} 行"
            f"  {meta['bytes_before']} -> {meta['bytes_after']} 字节"
        )

    notes = write_notes(out_dir, metas)

    # 机器可读的校验值 —— 给「应用补丁.ps1」用来判断
    # "还没打 / 已经打过 / 状态不对"，不用在脚本里再抄一份表。
    import json as _json

    with open(os.path.join(out_dir, "校验值.json"), "w", encoding="utf-8") as f:
        _json.dump(
            [
                {
                    "id": m["id"],
                    "patch": m["fname"],
                    "rel": m["rel"],
                    "sha_before": m["sha_before"],
                    "sha_after": m["sha_after"],
                    "title": m["title"],
                }
                for m in metas
            ],
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\n输出目录：{out_dir}")
    print(f"说明文档：{notes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
