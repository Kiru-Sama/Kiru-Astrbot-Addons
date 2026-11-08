"""电脑端：把 QCE 导出的聊天记录转成「更自然的对话」插件可用的数据包。

用法：
    python build_natural_pack.py <聊天记录.json> [--ta 名字] [--me 名字] [--out 输出目录] [--id 包名]

输出（默认到 ./packs/）：
    natural_pack_<id>.json   风格档案 + 原话对 + 元信息（插件直接加载）
    persona_<id>.txt         静态人格提示词（放进「更自然的对话beta」人格用）

依赖：同目录的 chatlog.py（从 qq-clone 项目复用，不改逻辑）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "_probe" / "qq-clone-src"))
sys.path.insert(0, str(HERE))

import chatlog  # noqa: E402


def _persona_prompt(persona: chatlog.Persona) -> str:
    """静态人格：模板去掉「示例」段，示例由插件每轮动态追加。"""
    profile = "\n".join(persona.profile_lines())
    return chatlog.SYSTEM_TEMPLATE.replace(
        "\n# 你以前真实发过的话（照这个感觉说，别照抄）\n{examples}\n", ""
    ).format(
        name=persona.name,
        profile=profile,
        examples="",
        notes="别主动结束话题，也别一直追问，自然点。",
    ).strip()


def build(src: Path, ta: str | None, me: str | None, out_dir: Path, pack_id: str) -> tuple[Path, Path]:
    persona = chatlog.build_persona(src, ta=ta, me=me)
    out_dir.mkdir(parents=True, exist_ok=True)

    pack = {
        "schema": 1,
        "generated_at": int(time.time()),
        "id": pack_id,
        "name": persona.name,
        "me": persona.me,
        "total_messages": persona.total,
        "profile": persona.profile,
        "pairs": persona.pairs,
        "source": src.name,
    }
    pack_path = out_dir / f"natural_pack_{pack_id}.json"
    pack_path.write_text(
        json.dumps(pack, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    prompt_path = out_dir / f"persona_{pack_id}.txt"
    prompt_path.write_text(_persona_prompt(persona), encoding="utf-8")

    return pack_path, prompt_path


def main() -> int:
    ap = argparse.ArgumentParser(description="QCE 聊天记录 → 更自然的对话数据包")
    ap.add_argument("src", help="QCE 导出的 json 路径")
    ap.add_argument("--ta", default=None, help="要学的人的名字（默认自动判断）")
    ap.add_argument("--me", default=None, help="『我』的名字（默认自动判断）")
    ap.add_argument("--out", default=str(HERE / "packs"), help="输出目录")
    ap.add_argument("--id", default=None, help="包 id（默认用 TA 名字）")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_file():
        print(f"✗ 文件不存在: {src}")
        return 1

    pack_path, prompt_path = build(
        src, args.ta, args.me, Path(args.out), args.id or src.stem
    )
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    size_kb = pack_path.stat().st_size / 1024
    print(f"✓ 数据包: {pack_path}  ({size_kb:.0f} KB)")
    print(f"  对象: {pack['name']} · 消息 {pack['total_messages']} 条 · 原话对 {len(pack['pairs'])} 组")
    print(f"✓ 人格提示词: {prompt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
