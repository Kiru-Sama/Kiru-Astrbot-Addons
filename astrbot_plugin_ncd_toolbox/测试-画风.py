# -*- coding: utf-8 -*-
"""回归护栏：画风 LoRA 目录 + 格式探测。

命名约定：<SFW|NSFW>_<画风>.safetensors

三条必须钉住的规则：
  1. 没评价前缀的文件不收（老的 SD1.5 LoRA 就是这样被挡在外面的）
  2. 格式不支持的不收，而且要给出原因 —— 实测 guizhencao 是 LoKr，diffusers 加载直接报错
  3. 底模不对的不收 —— SD1.5 LoRA 挂 SDXL 会 shape mismatch（768 vs 2048）

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-画风.py
"""

import json
import pathlib
import struct
import sys
import tempfile

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import lora_catalog as lc  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


def write_st(path: pathlib.Path, keys: list[str], meta: dict | None = None) -> None:
    """造一个"头部合法"的 safetensors（只有头，没有张量数据）。

    探测只读头，所以这样就够测；不用真的造几百 MB。
    """
    head: dict = {k: {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]} for k in keys}
    if meta:
        head["__metadata__"] = meta
    blob = json.dumps(head).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(blob)) + blob + b"\0\0")


XL_META = {"ss_network_module": "networks.lora",
           "modelspec.architecture": "stable-diffusion-xl-v1-base/lora"}

d = pathlib.Path(tempfile.mkdtemp())
write_st(d / "SFW_cunnyfunky.safetensors", ["lora_te1_x.lora_down.weight"], XL_META)
write_st(d / "SFW_guizhencao_chenbin.safetensors",
         ["lora_unet_a.lokr_w1", "lora_unet_a.lokr_w2"],
         {"ss_network_module": "lycoris.kohya",
          "modelspec.architecture": "stable-diffusion-xl-v1-base/lora"})
write_st(d / "NSFW_St_Louis.safetensors", ["lora_te1_y.lora_up.weight"], XL_META)
# SD1.5 的 LoRA：有前缀也该被底模规则挡住
write_st(d / "SFW_old15.safetensors", ["lora_te_text_model_encoder_layers_0.lora_down.weight"],
         {"ss_network_module": "networks.lora"})
# 没前缀 / 后缀不对
write_st(d / "style_GZC.safetensors", ["lora_te_text_model_encoder_layers_0.lora_down.weight"], {})
(d / "SFW_broken.txt").write_bytes(b"not a model")

loras = lc.scan(d)

# =====================================================================
print("===== ① 扫描：只收「有前缀 + 格式支持 + SDXL」 =====")
for lo in loras:
    print(f"  {lo.rating:5} {lo.name:32} {lo.size_mb} MB")
print("\n  被跳过：")
for name, why in lc.SKIPPED:
    print(f"    {name:42} {why}")

names = {lo.name for lo in loras}
check("①", names == {"cunnyfunky", "St_Louis"}, f"应收 2 个，实际 {names}")
check("①", "guizhencao_chenbin" not in names, "★LoKr 被收进目录了（加载会报错）")
check("①", "old15" not in names, "★SD1.5 的 LoRA 被收进目录了")
check("①", "style_GZC" not in names, "★没评价前缀的被收了")
check("①", "broken" not in names, "非 safetensors 被收了")
check("①", any("LoKr" in why for _, why in lc.SKIPPED), "LoKr 被跳过时没给原因")
check("①", any("SD1.5" in why for _, why in lc.SKIPPED), "SD1.5 被跳过时没给原因")

# =====================================================================
print("\n===== ② probe 单独测 =====")
for f, want in (
    ("SFW_cunnyfunky.safetensors", ("lora", "sdxl")),
    ("SFW_guizhencao_chenbin.safetensors", ("lokr", "sdxl")),
    ("SFW_old15.safetensors", ("lora", "sd15")),
):
    got = lc.probe(d / f)
    print(f"  {f:42} -> {got}")
    check("②", got == want, f"{f} 应为 {want}，实际 {got}")
check("②", lc.probe(d / "SFW_broken.txt") == ("unknown", "unknown"), "垃圾文件应判 unknown")

# =====================================================================
print("\n===== ③ 清单只列可用的 =====")
txt = lc.catalog_text(loras)
print(txt)
check("③", "cunnyfunky" in txt and "St_Louis" in txt, "清单漏了可用画风")
check("③", "guizhencao" not in txt, "★不可用的画风出现在清单里")
check("③", "old15" not in txt, "★SD1.5 的画风出现在清单里")

# =====================================================================
print("\n===== ④ 名字解析 =====")
for want, n in (("", 0), ("none", 0), ("cunnyfunky", 1),
                ("CUNNYFUNKY", 1), ("cunnyfunky, st_louis:0.6", 2)):
    got = lc.resolve(want, loras)
    print(f"  {want!r:26} -> {n} 个  {lc.summarize(got)}")
    check("④", len(got) == n, f"{want!r} 应为 {n} 个，实际 {len(got)}")

check("④", lc.resolve("cunnyfunky", loras)[0]["scale"] == lc.DEFAULT_SCALE,
      f"默认强度应为 {lc.DEFAULT_SCALE}")

for bad, why in (("guizhencao", "LoKr 不在目录里"), ("old15", "SD1.5 不在目录里"),
                 ("abc", "太短/不存在")):
    try:
        lc.resolve(bad, loras)
        check("④", False, f"★{why}（{bad!r}）竟然能解析成功")
    except lc.LoraError as exc:
        print(f"  {bad!r:14} -> {str(exc)[:56]}")

try:
    lc.resolve("St_Louis", loras, allow_nsfw=False)
    check("④", False, "★allow_nsfw=False 时仍选了 NSFW")
except lc.LoraError:
    print("  关掉 NSFW 后选它 -> 报错 ✓")

# =====================================================================
print("\n===== ④-2 中文画风名 =====")
# 2026-10-19：实测 diffusers 接受中文 adapter_name、也接受中文文件名，
# 所以目录里可以放 SFW_像素风.safetensors 这种名字。这里钉住解析行为。
write_st(d / "SFW_像素风.safetensors", ["lora_te1_p.lora_down.weight"], XL_META)
write_st(d / "SFW_3D渲染风.safetensors", ["lora_te1_q.lora_down.weight"], XL_META)
cn = lc.scan(d)
for want, expect in (("像素风", "像素风"), ("像素", "像素风"), ("3D", "3D渲染风"),
                     ("渲染", "3D渲染风"), ("3d渲染", "3D渲染风")):
    got = lc.resolve(want, cn)
    print(f"  {want!r:10} → {got[0]['name']}  （{got[0]['scale']}）")
    check("④-2", got[0]["name"] == expect, f"{want!r} 解析成了 {got[0]['name']}")
check("④-2", "像素风" in lc.catalog_text(cn), "中文画风没出现在清单里")
mixed = lc.resolve("像素风:0.6,3D渲染风:0.5", cn)
print(f"  中文叠加 → {lc.summarize(mixed)}")
check("④-2", [m["name"] for m in mixed] == ["像素风", "3D渲染风"], "中文叠加顺序/名字不对")
check("④-2", [m["scale"] for m in mixed] == [0.6, 0.5], "中文叠加的强度没解析对")

# =====================================================================
print("\n===== ⑤ 真实目录 =====")
# ★ 不写死本机路径：从插件目录往上推出 AstrBot 根，再找独立环境的 loras。
#   （分享包里出现本机目录名会被隐私扫描抓出来）
import os
# ★ 真实目录用环境变量传（不写死本机路径，否则分享包会泄露目录名）
_LORAS = pathlib.Path(os.environ.get("NCD_LORA_DIR") or "")
real = lc.scan(_LORAS) if _LORAS.is_dir() else []
print(f"  可用 {len(real)} 个：" + "、".join(f"{lo.name}({lo.rating})" for lo in real))
for name, why in lc.SKIPPED:
    print(f"    跳过 {name}: {why}")
if real:
    check("⑤", all(lo.rating in ("SFW", "NSFW") for lo in real), "评价解析失败")
else:
    print("  （跳过：没设置 NCD_LORA_DIR，无法核对真实目录）")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 前缀过滤 / 格式探测 / 底模过滤 / 原因可查 / 清单 / 解析 / 真实目录")
