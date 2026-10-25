# -*- coding: utf-8 -*-
"""端到端：真的拉起 ComfyUI、用你给的预设工作流出图、取回图片。

前置：用 --comfy-dir 或环境变量 COMFY_DIR 指向 ComfyUI 便携版根目录，
     它的 extra_model_paths.yaml 能查到四个工作流用到的底模。

跑：
    python 测试-出图.py --preset all --fast        # 四个预设各出一张（步数压到 6）
    python 测试-出图.py --preset moss              # 只跑 MOSS，用工作流原参数
    python 测试-出图.py --preset sdxl --free-test  # 额外验证"卸载模型"接口

★ 脚本自己拉起 ComfyUI、跑完自己收掉，不弹界面窗口。
  第一次冷启动要 import torch，20~60 秒很正常。
"""

import argparse
import asyncio
import os
import pathlib
import struct
import sys
import time

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from comfy_backend import ComfyClient, ComfyError  # noqa: E402
import presets  # noqa: E402
import sizing  # noqa: E402


def png_size(data: bytes) -> tuple[int, int] | None:
    """从 PNG 的 IHDR 里读宽高（不装 Pillow 也能验）。"""
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


async def one_preset(client, name: str, args) -> bool:
    graph = presets.load_graph(name)
    info = presets.summarize(graph)
    meta = presets.PRESETS[name]
    width, height = sizing.parse_size(args.size, 1920, 1920 * 1440) or (
        info["width"], info["height"]
    )
    steps = 6 if args.fast else info["steps"]
    print(f"\n--- {name}（{meta['label']}）底模={info['model']}")
    print(f"    {width}×{height} · {steps} 步 · CFG {info['cfg']:g} · {info['sampler_name']}")

    final = presets.apply_params(
        graph,
        prompt="masterpiece, best quality, 1girl, solo, gentle smile, "
               "upper body, looking at viewer, soft lighting",
        negative="lowres, bad anatomy, watermark, signature, text",
        width=width,
        height=height,
        steps=steps,
        cfg=info["cfg"],
        seed=20261021,
        model=None,
        loras=None,
    )
    t0 = time.time()
    try:
        result = await client.generate(final)
        image = result["images"][0]
        data = await client.fetch_image(image)
    except ComfyError as exc:
        print(f"    ❌ 出图失败：{exc}")
        return False
    took = time.time() - t0
    size = png_size(data)
    out = PLUGIN / "data" / f"e2e_{name}_{int(time.time())}.png"
    out.write_bytes(data)
    ok = True
    print(f"    拿到 {image.get('filename')} · {len(data):,} 字节 · {took:.1f} 秒 · 存 {out.name}")
    if size is None:
        print("    ❌ 不是合法 PNG")
        ok = False
    elif size != (width, height):
        print(f"    ❌ 尺寸不对：{size[0]}×{size[1]}，应为 {width}×{height}")
        ok = False
    if len(data) < 5000:
        print("    ❌ 图片太小，多半是噪声")
        ok = False

    if args.img2img:
        print("    -- 图生图：拿这张当参考图，denoise 0.5")
        try:
            ref_name = await client.upload_image(data, f"ref_{name}.png")
            g2 = presets.apply_params(
                graph,
                prompt="same girl, gentle smile, looking at viewer, soft lighting",
                negative="lowres, bad anatomy, watermark",
                width=width,
                height=height,
                steps=steps,
                cfg=info["cfg"],
                seed=12345,
                source_image=ref_name,
                denoise=0.5,
            )
            r2 = await client.generate(g2)
            d2 = await client.fetch_image(r2["images"][0])
        except ComfyError as exc:
            print(f"    ❌ 图生图失败：{exc}")
            return False
        size2 = png_size(d2)
        out2 = PLUGIN / "data" / f"e2e_{name}_img2img_{int(time.time())}.png"
        out2.write_bytes(d2)
        print(f"    图生图 {len(d2):,} 字节 · {size2[0]}×{size2[1]} · 存 {out2.name}")
        if size2 != (width, height):
            print(f"    ❌ 图生图尺寸不对：{size2}")
            ok = False
    print("    ✅ 通过" if ok else "    ❌ 不通过")
    return ok


async def run(args) -> int:
    log = PLUGIN / "data" / "comfy_e2e.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    client = ComfyClient(
        comfy_dir=args.comfy_dir,
        port=args.port,
        startup_timeout=args.startup_timeout,
        gen_timeout=args.gen_timeout,
        idle_timeout=1800,
        unload_timeout=300,
        log_path=log,
    )
    print(f"== 便携版：{client.comfy_dir}")
    names = list(presets.PRESETS) if args.preset == "all" else [args.preset]
    ok_all = True
    try:
        t0 = time.time()
        await client.ensure_server()
        print(f"== ComfyUI 就绪，用时 {time.time() - t0:.1f} 秒")
        for name in names:
            ok_all = await one_preset(client, name, args) and ok_all
        if args.free_test:
            before = client._health_sync()  # 只为读显存数字
            await client.free_models()
            await asyncio.sleep(2)
            after = client._health_sync()
            def free_gb(stats):
                devs = (stats or {}).get("devices") or [{}]
                return float(devs[0].get("vram_free") or 0) / 1024**3
            print(f"\n== 卸载模型：显存空闲 {free_gb(before):.1f} GB → {free_gb(after):.1f} GB "
                  f"（模型标记 loaded={client.model_loaded()}）")
            if free_gb(after) <= free_gb(before):
                print("   ⚠ 显存没有明显变化（可能模型本来就已释放）")
        if args.idle_test:
            print(f"\n== 空闲自退：等进程自己退出（idle_timeout 由调用方设小）")
            client.idle_timeout = args.idle_timeout
            deadline = time.time() + 90
            while time.time() < deadline and client.running():
                await asyncio.sleep(2)
            if client.running():
                print("   ❌ 超过 90 秒进程还在")
                ok_all = False
            else:
                print("   ✅ 进程已自己退出")
    except ComfyError as exc:
        print(f"❌ {exc}")
        ok_all = False
    finally:
        client.stop()
        print("\n== 已收掉 ComfyUI 进程")
    print("✅ 端到端通过" if ok_all else "❌ 端到端不通过")
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comfy-dir", default=os.environ.get("COMFY_DIR", ""))
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--preset", default="sdxl", help="sdxl/krea2/anima/moss，或 all")
    ap.add_argument("--size", default="", help="留空 = 用工作流里的画幅")
    ap.add_argument("--fast", action="store_true", help="步数压到 6，只验链路")
    ap.add_argument("--startup-timeout", type=float, default=300)
    ap.add_argument("--gen-timeout", type=float, default=300)
    ap.add_argument("--free-test", action="store_true", help="额外验证卸载模型接口")
    ap.add_argument("--img2img", action="store_true", help="再用刚出的图跑一次图生图")
    ap.add_argument("--idle-test", action="store_true")
    ap.add_argument("--idle-timeout", type=float, default=5)
    args = ap.parse_args()
    if not args.comfy_dir:
        ap.error("请用 --comfy-dir 或环境变量 COMFY_DIR 指定 ComfyUI 便携版根目录")
    if args.preset != "all" and args.preset not in presets.PRESETS:
        print(f"没有这个预设：{args.preset}；可选 {'、'.join(presets.PRESETS)} 或 all")
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
