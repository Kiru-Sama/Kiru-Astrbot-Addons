# -*- coding: utf-8 -*-
"""构建「最小 ComfyUI 便携版」：只复制跑起来必需的东西，模型一个字节都不搬。

用法（Windows，任意 Python 3.8+ 都行，不需要装依赖）：

    python 构建最小版.py --source "F:\\ComfyUI_portable\\ComfyUI_windows_portable-G314" \\
                         --target "F:\\ComfyUI_min"

    # 模型库不在 <source>\\..\\Mods In It 时显式指定：
    python 构建最小版.py --source ... --target ... --models "D:\\models"

做三件事：
  1. 复制 python_embeded（去掉 __pycache__ / *.pyc / *.lib / *.exp，省 400 MB 左右）
  2. 复制 ComfyUI 核心代码（不含 .git / 自定义节点 / 模型 / 输出 / 用户数据）
  3. 建空骨架目录 + extra_model_paths.yaml（模型外挂）+ run_min.bat

为什么值得单独做一份：
  · 完整便携版里的 custom_nodes / output / .git 经常上 GB，出图根本用不到
  · 自定义节点装多了会互相干扰；最小版只有官方节点，行为可预测
  · 模型仍然共用一个目录 —— 不复制、不占额外空间、换模型两边都生效
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SKELETON_DIRS = [
    "custom_nodes",
    "input",
    "output",
    "temp",
    "models/checkpoints",
    "models/vae",
    "models/clip",
    "models/clip_vision",
    "models/loras",
    "models/unet",
    "models/diffusion_models",
    "models/controlnet",
    "models/upscale_models",
    "models/embeddings",
    "models/style_models",
    "models/ipadapter",
    "models/gligen",
    "models/photomaker",
    "models/hypernetworks",
]

CORE_EXCLUDES = [
    ".git", ".github", ".ci",
    "custom_nodes", "input", "output", "temp", "user",
    "models", "tests", "tests-unit", "web", "__pycache__", "script_examples",
]

YAML_TEMPLATE = """\
# 最小 ComfyUI 便携版的模型来源（一个字节都不复制，全部外挂）
# base_path 改成你自己的模型库目录即可；子项对应 ComfyUI 的模型类别。
mods:
    base_path: {base}
    checkpoints: checkpoints
    diffusion_models: diffusion_models
    text_encoders: text_encoders
    clip: clip
    clip_vision: clip_vision
    vae: vae
    vae_approx: vae_approx
    loras: loras
    style_models: style_models
    embeddings: embeddings
    upscale_models: upscale_models
    latent_upscale_models: latent_upscale_models
    ipadapter: ipadapter
    controlnet: controlnet
    detection: detection
    yolo: yolo
    ultralytics: ultralytics
    sams: sams
    nlf: nlf
"""

BAT_TEMPLATE = """\
@echo off
cd /d "%~dp0"
python_embeded\\python.exe -s ComfyUI\\main.py --port {port} --listen 127.0.0.1 --disable-auto-launch
pause
"""


def run_robocopy(src: Path, dst: Path, extra: list[str]) -> int:
    cmd = ["robocopy", str(src), str(dst), "/E", *extra,
           "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/MT:24"]
    print("  " + " ".join(cmd))
    proc = subprocess.run(cmd, check=False)  # noqa: S603
    # robocopy: 0-7 成功（1 = 复制了文件，2/4/6 是"多余/不匹配"之类，都不算失败）
    return proc.returncode


def dir_size(path: Path) -> tuple[float, int]:
    total = 0
    count = 0
    if not path.exists():
        return 0.0, 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
                count += 1
        except OSError:
            continue
    return total / 1024**3, count


def main() -> int:
    ap = argparse.ArgumentParser(description="构建最小 ComfyUI 便携版")
    ap.add_argument("--source", required=True, help="完整便携版根目录（含 python_embeded）")
    ap.add_argument("--target", required=True, help="输出目录，例如 F:\\ComfyUI_min")
    ap.add_argument("--models", default="", help="模型库根目录（默认 <source>\\..\\Mods In It）")
    ap.add_argument("--port", type=int, default=8199, help="run_min.bat 里用的端口")
    args = ap.parse_args()

    src = Path(args.source).expanduser().resolve()
    dst = Path(args.target).expanduser().resolve()
    if not (src / "python_embeded").is_dir():
        print(f"❌ {src} 里没有 python_embeded —— 这不是 ComfyUI 便携版根目录")
        return 1
    if not (src / "ComfyUI" / "main.py").is_file():
        print(f"❌ {src}\\ComfyUI\\main.py 不存在")
        return 1
    models = Path(args.models).expanduser().resolve() if args.models else (src.parent / "Mods In It")
    if not models.is_dir():
        print(f"⚠ 模型目录不存在：{models}（先建好，或改 extra_model_paths.yaml）")

    dst.mkdir(parents=True, exist_ok=True)

    print("[1/3] 复制 python_embeded ...")
    rc = run_robocopy(
        src / "python_embeded",
        dst / "python_embeded",
        ["/XD", "__pycache__", "/XF", "*.pyc", "*.lib", "*.exp"],
    )
    if rc >= 8:
        print(f"❌ 复制 python_embeded 失败，robocopy exit={rc}")
        return 1

    print("[2/3] 复制 ComfyUI 核心 ...")
    # ★ 排除项必须用【绝对路径】：robocopy 的 /XD 只写名字时会匹配任意层级的同名目录，
    #   于是 comfy\ldm\models、comfy_api\input 这种核心目录会被一起排掉 ——
    #   启动时报 `No module named 'comfy.ldm.models'`（2026-10-21 实测踩过）。
    core_excludes = [
        str(src / "ComfyUI" / name) for name in CORE_EXCLUDES if name != "__pycache__"
    ] + ["__pycache__"]
    rc = run_robocopy(
        src / "ComfyUI",
        dst / "ComfyUI",
        ["/XD", *core_excludes, "/XF", "*.pyc", "*.bak", "*.bak-*", "AGENTS.md", "openapi.yaml"],
    )
    if rc >= 8:
        print(f"❌ 复制 ComfyUI 核心失败，robocopy exit={rc}")
        return 1

    print("[3/3] 建骨架目录 / 写配置 ...")
    for rel in SKELETON_DIRS:
        (dst / "ComfyUI" / rel).mkdir(parents=True, exist_ok=True)
    (dst / "ComfyUI" / "extra_model_paths.yaml").write_text(
        YAML_TEMPLATE.format(base=models), encoding="utf-8"
    )
    (dst / "run_min.bat").write_text(BAT_TEMPLATE.format(port=args.port), encoding="ascii")

    py_gb, py_n = dir_size(dst / "python_embeded")
    core_gb, core_n = dir_size(dst / "ComfyUI")
    print("\n===== 汇总 =====")
    print(f"python_embeded: {py_gb:.2f} GB / {py_n} 个文件")
    print(f"ComfyUI:        {core_gb * 1024:.0f} MB / {core_n} 个文件")
    print(f"合计:           {py_gb + core_gb:.2f} GB")
    print(f"模型来源:       {models}（不复制）")
    print(f"\n验证：{dst}\\run_min.bat  然后浏览器开 http://127.0.0.1:{args.port}")
    print("（插件 astrbot_plugin_ncd_comfy 的 comfy_dir 填这个目录即可）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
