# -*- coding: utf-8 -*-
"""回归护栏：LoRA 要能一路传到 worker 的命令行上。

链路：配置 sd_lora / sd_lora_scale → StandaloneClient → 子进程参数 --lora / --lora-scale
断在任何一环，表现都是"配置了但没效果"，而且日志里看不出来。

本测试只测【参数拼接】（快）；真实加载/出图另由 e2e 脚本验证。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-LoRA.py
"""

import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sd_local as sd  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


class FakeProc:
    returncode = None

    def poll(self):
        return None

    def terminate(self):
        pass


def capture_cmd(**kwargs) -> list[str]:
    """拦下 subprocess.Popen，把命令行捞出来。"""
    captured: list[list[str]] = []
    orig = sd.subprocess.Popen

    def fake_popen(cmd, **kw):  # noqa: ANN001, ANN003
        captured.append(list(cmd))
        return FakeProc()

    sd.subprocess.Popen = fake_popen
    try:
        client = sd.StandaloneClient(
            python=kwargs["python"],
            checkpoint=kwargs["checkpoint"],
            worker_script=kwargs.get("worker_script"),
            **{k: v for k, v in kwargs.items() if k not in ("python", "checkpoint", "worker_script")},
        )
        client._spawn()
    finally:
        sd.subprocess.Popen = orig
    return captured[0] if captured else []


PY = r"C:\example\env\Scripts\python.exe"
CKPT = r"C:\example\models\anything.safetensors"
LORA = r"C:\example\models\loras\my_style.safetensors"
# ★ 这里用明显的示例路径：本测试只验【命令行拼接】，不碰文件系统。
#   写真实本机路径会把个人目录带进分享包（实测被抓出来 3 处）。

# =====================================================================
#  用例 1：配了 LoRA → 命令行要带 --lora 和 --lora-scale
# =====================================================================
print("===== 用例 1：有 LoRA 时拼参数 =====")
cmd = capture_cmd(python=PY, checkpoint=CKPT, lora=LORA, lora_scale=0.75)
print("  " + " ".join(part.replace("\\", "/").split("/")[-1] for part in cmd[-6:]))
check("用例1", "--lora" in cmd, "★命令行里没有 --lora")
if "--lora" in cmd:
    idx = cmd.index("--lora")
    check("用例1", cmd[idx + 1] == LORA, f"--lora 后面的路径不对：{cmd[idx + 1]}")
check("用例1", "--lora-scale" in cmd, "★命令行里没有 --lora-scale")
if "--lora-scale" in cmd:
    idx = cmd.index("--lora-scale")
    check("用例1", cmd[idx + 1] == "0.75", f"--lora-scale 值不对：{cmd[idx + 1]}")

# =====================================================================
#  用例 2：没配 LoRA → 一个多余人参都不能带（否则 worker 会报错）
# =====================================================================
print("\n===== 用例 2：没 LoRA 时不带参数 =====")
cmd2 = capture_cmd(python=PY, checkpoint=CKPT, lora="", lora_scale=0.8)
print(f"  参数个数 {len(cmd2)}，含 --lora: {'--lora' in cmd2}")
check("用例2", "--lora" not in cmd2, "★没配 LoRA 却仍传了 --lora（worker 会拿到空路径）")
check("用例2", "--lora-scale" not in cmd2, "★没配 LoRA 却仍传了 --lora-scale")

# =====================================================================
#  用例 3：客户端属性要能被读到（方便排查"到底传了什么"）
# =====================================================================
print("\n===== 用例 3：客户端属性 =====")
c = sd.StandaloneClient(python=PY, checkpoint=CKPT, lora="  " + LORA + "  ", lora_scale="0.6")
print(f"  lora={c.lora!r}  lora_scale={c.lora_scale}")
check("用例3", c.lora == LORA, "首尾空白没去掉")
check("用例3", c.lora_scale == 0.6, f"强度没转成 float：{c.lora_scale!r}")

empty = sd.StandaloneClient(python=PY, checkpoint=CKPT)
check("用例3", empty.lora == "", "不传时应为空串")
check("用例3", empty.lora_scale == 0.8, f"默认强度应为 0.8，实际 {empty.lora_scale}")

# =====================================================================
#  用例 4：worker 的 argparse 必须认得这两个参数
# =====================================================================
print("\n===== 用例 4：worker 参数定义 =====")
worker = (PLUGIN / "sd_worker.py").read_text(encoding="utf-8")
check("用例4", '"--lora"' in worker, "★sd_worker.py 里没有 --lora 参数")
check("用例4", '"--lora-scale"' in worker, "★sd_worker.py 里没有 --lora-scale 参数")
check("用例4", "load_lora(" in worker, "★没有调用 load_lora（加载逻辑没接上）")
check("用例4", "_lora_kwargs()" in worker,
      "★推理时没传 cross_attention_kwargs（LoRA 加载了也不会生效）")
n_kwargs = worker.count("**_lora_kwargs()")
print(f"  推理调用点接了 {n_kwargs} 处（文生图 + 图生图，应为 2）")
check("用例4", n_kwargs == 2, f"★应有 2 处（txt2img + img2img），实际 {n_kwargs}")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 有 LoRA 时拼参数 / 没 LoRA 不带参数 / 属性归一化 / worker 参数与调用点 全部正确")

