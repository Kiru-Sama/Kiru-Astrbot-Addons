# -*- coding: utf-8 -*-
"""回归护栏：采样器（欧拉+普通 / DPM++ 2M+Karras）与画幅解析。

为什么要有这个文件：
    2026-10-18 加"采样器调度器多一个选项：欧拉和普通（默认）"，同时让 AI 能自己选画幅。
    这两件事都容易"看起来接上了、其实没生效"：
      · 采样器的名字写错 → 悄悄回落成默认，画出来还是老样子
      · 画幅写错 → 静默用默认尺寸，模型以为自己指定了
    所以这里把【纯逻辑】都钉住，真机出图差异另由 _swap_verify 一类的脚本验证。

本测试覆盖：
  1. normalize_sampler：各种写法收敛，认不出回落 euler
  2. make_scheduler：两种档位造出来的类/关键参数正确（Karras 不能泄漏给 euler）
  3. parse_size：832x1216 / 竖横方 / 8 倍数对齐 / 越界与乱写要报错
  4. 配置默认值：sd_sampler 默认 euler；插件 payload 里带 sampler
  5. 换采样器只换对象、不重新加载权重（源码级：load_pipeline 之外没有 from_single_file）

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-采样器.py
"""

import json
import pathlib
import re
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sd_worker  # noqa: E402
import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


# =====================================================================
print("===== ① 采样器名字收敛 =====")
cases = {
    "euler": "euler",
    "EULER": "euler",
    "欧拉": "euler",
    "euler_normal": "euler",
    "dpmpp2m_karras": "dpmpp2m_karras",
    "dpmpp_2m": "dpmpp2m_karras",
    "DPM++2M": "dpmpp2m_karras",
    "karras": "dpmpp2m_karras",
    "": "euler",
    "胡说八道": "euler",
}
for raw, expect in cases.items():
    got = sd_worker.normalize_sampler(raw)
    print(f"  {raw!r:18} → {got}")
    check("①", got == expect, f"{raw!r} 应归一成 {expect}，实际 {got}")

# =====================================================================
print("\n===== ② 两种档位的调度器（在 SD 的 python 里真造一遍）=====")
# ★ AstrBot 的 venv 里没有 torch/diffusers（那是 SD 独立环境的活），
#   所以这一段不能在本进程里 import diffusers —— 用配置里的 sd_python 去跑。
#   这也顺带验证了"插件用的那个 python 里，这两种调度器真的造得出来"。
import subprocess  # noqa: E402

ASTRBOT_ROOT = PLUGIN.parents[2]
CONFIG = ASTRBOT_ROOT / "data" / "config" / "astrbot_plugin_ncd_toolbox_config.json"
conf = json.loads(CONFIG.read_bytes().decode("utf-8-sig")) if CONFIG.exists() else {}
SD_PYTHON = str(conf.get("sd_python") or "").strip()

SNIPPET = r"""
import json, sys
sys.path.insert(0, r"%s")
import sd_worker as w
base = {"num_train_timesteps":1000,"beta_start":0.00085,"beta_end":0.012,
        "beta_schedule":"scaled_linear","steps_offset":1,"prediction_type":"epsilon",
        "timestep_spacing":"linspace","use_karras_sigmas":True,
        "algorithm_type":"dpmsolver++","solver_order":2}
out = {}
for key in ("euler", "dpmpp2m_karras"):
    s = w.make_scheduler(key, base)
    s.set_timesteps(20)
    out[key] = {"cls": type(s).__name__, "karras": s.config.get("use_karras_sigmas"),
                "beta": s.config.get("beta_schedule"),
                "ts": [round(float(x), 2) for x in s.timesteps[:4]]}
print(json.dumps(out))
""" % (PLUGIN,)

if not SD_PYTHON or not pathlib.Path(SD_PYTHON).exists():
    print(f"  （跳过：配置里没有可用的 sd_python：{SD_PYTHON!r}）")
else:
    try:
        proc = subprocess.run(
            [SD_PYTHON, "-c", SNIPPET], capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            print("  sd_python 执行失败：")
            print((proc.stderr or "")[-600:])
            failures.append("② 在 sd_python 里造调度器失败（见上面的 stderr）")
        else:
            got = json.loads((proc.stdout or "").strip().splitlines()[-1])
            print(f"  euler          → {got['euler']['cls']}  karras={got['euler']['karras']}"
                  f"  beta={got['euler']['beta']}  前4步={got['euler']['ts']}")
            print(f"  dpmpp2m_karras → {got['dpmpp2m_karras']['cls']}"
                  f"  karras={got['dpmpp2m_karras']['karras']}"
                  f"  前4步={got['dpmpp2m_karras']['ts']}")
            check("②", got["euler"]["cls"] == "EulerDiscreteScheduler",
                  f"euler 造出来的是 {got['euler']['cls']}")
            check("②", got["dpmpp2m_karras"]["cls"] == "DPMSolverMultistepScheduler",
                  f"karras 造出来的是 {got['dpmpp2m_karras']['cls']}")
            check("②", got["euler"]["karras"] is False,
                  "★ euler 被 DPMSolver 的 use_karras_sigmas=True 污染了（那就不是“普通”了）")
            check("②", got["dpmpp2m_karras"]["karras"] is True, "karras 档没打开 karras sigma")
            check("②", str(got["euler"]["beta"]) == "scaled_linear",
                  f"euler 丢了底模的 beta 曲线：{got['euler']['beta']}")
            check("②", got["euler"]["ts"] != got["dpmpp2m_karras"]["ts"],
                  "★ 两档时间步完全一样 —— 等于没加选项")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"② 真机验证异常：{type(exc).__name__}: {exc}")

# =====================================================================
print("\n===== ③ 画幅解析 =====")
ok_cases = {
    "832x1216": (832, 1216),
    "1216*832": (1216, 832),
    "1024×1024": (1024, 1024),
    "竖": (832, 1216),
    "横": (1216, 832),
    "方": (1024, 1024),
    "720x1024": (720, 1024),
    "1440x1440": (1440, 1440),   # 警告线整，单边合法、面积也没超
    "1920x1440": (1920, 1440),   # 用户说的最大画布
    "1440x1920": (1440, 1920),
    "": None,
    "   ": None,
    "684x1205": (680, 1200),   # 自动对齐到 8 的倍数
}
for raw, expect in ok_cases.items():
    got = plugin_main.parse_size(raw)
    print(f"  {raw!r:14} → {got}")
    check("③", got == expect, f"{raw!r} 应解析成 {expect}，实际 {got}")

# ★ 1920×1920 = 3.69 MP > 1920×1440，实测会把 12 GB 显存逼爆，必须拒
bad_cases = ["100x100", "1920x1920", "2000x1440", "2560x1440", "3000x3000", "abc", "832 by 1216"]
for raw in bad_cases:
    try:
        got = plugin_main.parse_size(raw)
        failures.append(f"③ {raw!r} 本该报错，实际返回 {got}")
        print(f"  {raw!r:14} → 没报错（不应该）")
    except ValueError as exc:
        print(f"  {raw!r:14} → ValueError: {str(exc)[:40]}…")

# =====================================================================
print("\n===== ④ 配置与 payload 里的 sampler =====")
ASTRBOT_ROOT = PLUGIN.parents[2]
CONFIG = ASTRBOT_ROOT / "data" / "config" / "astrbot_plugin_ncd_toolbox_config.json"
schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
print(f"  schema.sd_sampler 默认 = {schema.get('sd_sampler', {}).get('default')!r}")
check("④", schema.get("sd_sampler", {}).get("default") == "euler", "配置默认不是 euler")
if CONFIG.exists():
    conf = json.loads(CONFIG.read_bytes().decode("utf-8-sig"))
    print(f"  本机配置 sd_sampler = {conf.get('sd_sampler')!r}")
    # ★ 不要求"必须是 euler"：别人可能选了 Karras。只要求它是个认识的值 ——
    #   否则这个测试换台机器/换个设置就假红。
    check("④", conf.get("sd_sampler") in (None, "", "euler", "dpmpp2m_karras"),
          f"sd_sampler 是认不出的值：{conf.get('sd_sampler')!r}")

src = (PLUGIN / "main.py").read_text(encoding="utf-8")
render = src[src.find("async def _render_standalone("):src.find("async def _render_standalone_img2img")]
check("④", '"sampler"' in render, "★ 出图 payload 里没带 sampler（配置改了也不生效）")
check("④", "sd_sampler" in render, "payload 的 sampler 没从配置取")

# =====================================================================
print("\n===== ⑤ 换采样器不重新加载权重 =====")
worker_src = (PLUGIN / "sd_worker.py").read_text(encoding="utf-8")
loads = len(re.findall(r"from_single_file\(", worker_src))
swap = worker_src[worker_src.find("def apply_sampler("):worker_src.find("def apply_loras(")]
print(f"  from_single_file 出现次数 = {loads}（只应在 load_pipeline 里）")
print(f"  apply_sampler 里有没有 from_single_file = {'from_single_file' in swap}")
check("⑤", loads == 1, "★ 采样器切换又去加载模型了（应该只换调度器对象）")
check("⑤", "from_single_file" not in swap, "★ apply_sampler 里重新加载了权重")
check("⑤", "make_scheduler" in swap, "apply_sampler 没有真的换调度器")

# =====================================================================
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 采样器名收敛 / 两档调度器真的不同 / 画幅解析与报错 / 配置与接线 / 不重载权重")
