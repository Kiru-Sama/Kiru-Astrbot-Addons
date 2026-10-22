# -*- coding: utf-8 -*-
"""模型目录解析：从 ComfyUI 的 extra_model_paths.yaml 读出每类模型在哪个盘。

为什么要读这个文件而不是写死目录：
  · 最小版和完整版共用同一份模型库，靠的就是这个 yaml
  · 你换模型库只改 yaml 一处，插件这边跟着变
  · 插件里一条本机路径都不写死，换个机器照样能用

yaml 的规则（ComfyUI 自己的约定）：
    mods:
        base_path: F:\\模型库        # 绝对路径，或相对于这个 yaml 所在目录
        checkpoints: checkpoints     # 相对 base_path
        loras: loras
"""

from __future__ import annotations

import os
from pathlib import Path

#: 插件关心的类别 → 同义写法（ComfyUI 里 unet 和 diffusion_models 都能放扩散模型）
CATEGORY_ALIASES = {
    "checkpoints": ("checkpoints",),
    "diffusion_models": ("diffusion_models", "unet", "diffusionmodels"),
    "loras": ("loras", "lora"),
    "text_encoders": ("text_encoders", "clip"),
    "vae": ("vae",),
}

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf")


def parse_yaml(path: Path) -> dict[str, list[Path]]:
    """把 extra_model_paths.yaml 解析成 {类别: [目录...]}。

    ★ 只认「段里有 base_path + 某个类别名」这种结构，够用且不依赖 PyYAML。
      解析不了的段直接跳过 —— 别因为一个写错的段让整个模型列表变空。
    """
    out: dict[str, list[Path]] = {}
    if not path or not path.exists():
        return out
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out

    section: list[tuple[str, str]] = []
    # 把 "缩进 键: 值" 收集成段，遇到 indent=0 就结算上一段
    for raw in lines:
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        key, sep, val = line.strip().partition(":")
        if not sep:
            continue
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if indent == 0:
            _flush(section, path, out)
            section = []
        else:
            # ★ base_path 也要进 section —— 之前只在局部变量里存了一下，
            #   _flush 读不到它，整段就被当成"没有 base_path"丢掉，
            #   结果模型清单永远是空的（2026-10-21 踩过）。
            section.append((key, val))
    _flush(section, path, out)
    return out


def _flush(section: list[tuple[str, str]], yaml_path: Path, out: dict[str, list[Path]]) -> None:
    """结算一个段：把里面的类别名映射成绝对目录。"""
    if not section:
        return
    base = ""
    pairs = []
    for key, val in section:
        if key == "base_path":
            base = val
        else:
            pairs.append((key, val))
    if not base:
        return
    base_path = Path(base)
    if not base_path.is_absolute():
        base_path = yaml_path.parent / base_path
    base_path = base_path.resolve()
    for key, val in pairs:
        canon = _canonical(key)
        if not canon:
            continue
        target = (base_path / val).resolve() if val else base_path
        bucket = out.setdefault(canon, [])
        if target not in bucket:
            bucket.append(target)


def _canonical(name: str) -> str:
    low = name.strip().lower()
    for canon, aliases in CATEGORY_ALIASES.items():
        if low in aliases:
            return canon
    return ""


def scan(categories: list[str], min_mb: float = 0.0) -> list[dict]:
    """扫这些类别的目录，返回 [{name, size_mb, path}]。

    name 是【相对于类别目录】的路径（反斜杠）—— 这正是 ComfyUI 里显示的 ckpt_name / lora_name。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for root in categories:
        if not root or not Path(root).is_dir():
            continue
        root_path = Path(root)
        for dirpath, _dirnames, filenames in os.walk(root_path):
            for fn in filenames:
                if not fn.lower().endswith(MODEL_EXTS):
                    continue
                full = Path(dirpath) / fn
                try:
                    size_mb = full.stat().st_size / 1024 / 1024
                except OSError:
                    continue
                if size_mb < min_mb:
                    continue
                try:
                    rel = full.relative_to(root_path)
                except ValueError:
                    continue
                name = str(rel).replace("/", "\\")
                if name in seen:
                    continue
                seen.add(name)
                out.append({"name": name, "size_mb": round(size_mb, 1), "path": str(full)})
    out.sort(key=lambda m: m["name"].lower())
    return out


def default_yaml(comfy_dir: str) -> Path:
    """最小版里的 extra_model_paths.yaml 位置（找不到返回一个不存在的路径）。"""
    base = Path(comfy_dir) if comfy_dir else Path("")
    for cand in (base / "ComfyUI" / "extra_model_paths.yaml", base / "extra_model_paths.yaml"):
        if cand.exists():
            return cand
    return base / "ComfyUI" / "extra_model_paths.yaml"
