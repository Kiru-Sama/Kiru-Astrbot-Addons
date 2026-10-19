# -*- coding: utf-8 -*-
"""回归护栏：SDXL 独立后端（SD1.5 支线已删除）。

覆盖：
  1. detect_arch 只返回 sdxl；拿小文件会告警但不崩
  2. worker 里已经没有 StableDiffusionPipeline（SD1.5 支线真的删了）
  3. offload 三档：auto/on -> model，sequential -> 逐层，off -> 不用
  4. 配置指向 SDXL 底模 + SDXL LoRA，分辨率/步数按工作流
  5. （已删）LoRA 的可选开关改在 standalone 侧：sd_lora_scale=0 即不挂

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-SDXL.py
"""

import json
import pathlib
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sd_worker  # noqa: E402

# ★ 不要写死本机路径：分享包里出现本机目录名会被隐私扫描抓出来。
#   这里从插件目录往上推出 AstrBot 根，再拼配置路径。
ASTRBOT_ROOT = PLUGIN.parents[2]
CONFIG = ASTRBOT_ROOT / "data" / "config" / "astrbot_plugin_ncd_draw_config.json"
WORKER_SRC = (PLUGIN / "sd_worker.py").read_text(encoding="utf-8")

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


# ===================================================================== 1
print("===== ① detect_arch 只认 SDXL =====")
# ★ 不碰真实模型文件：用"伪造大小"验证阈值逻辑，既快又不依赖本机模型。
import os as _os
import tempfile as _tempfile

_real_getsize = _os.path.getsize
with _tempfile.NamedTemporaryFile(suffix=".safetensors", delete=False) as fh:
    tiny = fh.name
try:
    print(f"  伪造 6.4 GB → {sd_worker.detect_arch(tiny)}")
    check("①", sd_worker.detect_arch(tiny) == "sdxl", "大文件应判为 sdxl")
    # 伪造一个"很大"的文件：只骗 getsize，不真占磁盘
    _os.path.getsize = lambda p: int(6.4 * 1024**3)  # type: ignore[assignment]
    try:
        print(f"  真实小文件 → {sd_worker.detect_arch(tiny)}（应仍返回 sdxl，但告警）")
        check("①", sd_worker.detect_arch(tiny) == "sdxl",
              "小文件也必须返回 sdxl（只支持 SDXL）")
    finally:
        _os.path.getsize = _real_getsize  # type: ignore[assignment]
finally:
    try:
        _os.unlink(tiny)
    except OSError:
        pass
check("①", "只支持 SDXL" in WORKER_SRC, "缺少「只支持 SDXL」的明确提示")

# ===================================================================== 2
print("\n===== ② SD1.5 支线确实删了 =====")
check("②", "StableDiffusionPipeline" not in WORKER_SRC,
      "★worker 里还有 StableDiffusionPipeline（SD1.5 支线没删干净）")
check("②", "StableDiffusionXLPipeline" in WORKER_SRC, "没加载 SDXL pipeline")
check("②", "enable_sequential_cpu_offload" in WORKER_SRC, "没接 sequential offload")

# ===================================================================== 3
print("\n===== ③ offload 三档 =====")
for mode, expect in (("auto", 'use_offload = "model"'),
                     ("on", 'use_offload = "model"'),
                     ("sequential", 'use_offload = "sequential"'),
                     ("off", "use_offload = False")):
    ok = expect in WORKER_SRC
    print(f"  {mode:<11} → {expect.split('=')[1].strip():<12} {'OK' if ok else '✗'}")
    check("③", ok, f"{mode} 分支没接上")

# ===================================================================== 4
print("\n===== ④ 配置指向 SDXL =====")
if not CONFIG.exists():
    print(f"  （跳过：没找到 {CONFIG}）")
    conf = None
else:
    conf = json.loads(CONFIG.read_bytes().decode("utf-8-sig"))
show = {k: (conf or {}).get(k) for k in
        ("image_backend", "sd_checkpoint", "sd_lora", "sd_lora_scale",
         "sd_arch", "sd_offload", "sd_width", "sd_height", "sd_steps", "sd_cfg")}
for k, v in show.items():
    print(f"  {k} = {str(v)[:78]}")
# ★ 不认具体模型名（别人可能用 juggernautXL 之类）：按【文件体积】判 SDXL。
#   SDXL 单文件底模一般 6~7 GB；SD1.5 只有 2 GB。文件不存在就只查扩展名。
ckpt = str((conf or {}).get("sd_checkpoint", ""))
if ckpt and pathlib.Path(ckpt).is_file():
    gb = pathlib.Path(ckpt).stat().st_size / 1024**3
    print(f"  底模体积 {gb:.2f} GB")
    check("④", gb >= 3.5, f"底模只有 {gb:.2f} GB，看起来不是 SDXL")
else:
    print(f"  （底模文件不存在，跳过体积判断：{ckpt}）")
check("④", ckpt.lower().endswith((".safetensors", ".ckpt")), "底模路径不像模型文件")
lora = str((conf or {}).get("sd_lora", ""))
if lora:
    print(f"  LoRA = {lora}")
    check("④", lora.lower().endswith(".safetensors"), "LoRA 不是 .safetensors")
else:
    print("  LoRA 没配（这是允许的：留空 = 不挂 LoRA）")
check("④", int((conf or {}).get("sd_width", 0)) >= 512 and int((conf or {}).get("sd_height", 0)) >= 768,
      "分辨率太小（SDXL 要在 1024 级别）")
check("④", int((conf or {}).get("image_timeout", 0)) >= 120,
      "image_timeout 太小：SDXL 冷启动 15 秒 + 出图 20 秒")

# ===================================================================== 5
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 只认 SDXL / SD1.5 支线已删 / offload 三档 / 配置指向 SDXL / LoRA 可开关")


