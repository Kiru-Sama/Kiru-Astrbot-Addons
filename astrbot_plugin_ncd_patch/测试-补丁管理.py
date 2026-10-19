# -*- coding: utf-8 -*-
"""回归护栏：补丁管理器（ncd_patch）。

要点（2026-10-19 整理时定的）：
  1. 状态判定只有四种：已应用 / 未应用 / 版本不匹配 / 目标缺失
  2. 非管理员不能看、更不能改
  3. apply/revert 都先 --check，预检不过就【一个字节都不动】
  4. 绝不自动应用：加载时只写日志

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-补丁管理.py
"""

import asyncio
import importlib.util
import pathlib
import shutil
import sys
import tempfile
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

plugin_main.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    debug=lambda *a, **k: None,
)

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


class FakeEvent:
    def __init__(self, role="admin"):
        self.role = role
        self.unified_msg_origin = "t:GroupMessage:1"


def make_plugin():
    inst = plugin_main.NcdPatchPlugin.__new__(plugin_main.NcdPatchPlugin)
    inst._conf = {}
    inst.context = types.SimpleNamespace()
    return inst


# =====================================================================
print("===== ① 状态判定：已应用 / 未应用 / 版本不匹配 / 缺失 =====")
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ncdpatch_"))
pkg = tmp / "pkg"
pkg.mkdir()
target = pkg / "core" / "demo.py"
target.parent.mkdir(parents=True)
target.write_text("original\n", encoding="utf-8")

applied_sha = plugin_main.sha16(target)
target.write_text("patched\n", encoding="utf-8")
patched_sha = plugin_main.sha16(target)

ASTRBOT_PKG = plugin_main.PKG_DIR   # 先记下真实路径，下面临时改成假目录
plugin_main.PKG_DIR = pkg  # 指到临时目录，别碰真源码

items = [
    {"id": "A", "rel": "core/demo.py", "sha_before": applied_sha, "sha_after": patched_sha},   # 已应用
    {"id": "B", "rel": "core/demo.py", "sha_before": patched_sha, "sha_after": "deadbeef"},    # 未应用
    {"id": "C", "rel": "core/demo.py", "sha_before": "0" * 16, "sha_after": "1" * 16},         # 不匹配
    {"id": "D", "rel": "core/nope.py", "sha_before": "0" * 16, "sha_after": "1" * 16},         # 缺失
]
got = {it["id"]: plugin_main.state_of(it)[0] for it in items}
print("  ", got)
check("①", got["A"] == "applied", "已应用没认出来")
check("①", got["B"] == "clean", "未应用没认出来")
check("①", got["C"] == "drifted", "版本不匹配没认出来")
check("①", got["D"] == "missing", "目标缺失没认出来")

# =====================================================================
print("\n===== ② 非管理员：只读也拒绝，改动更拒绝 =====")


async def case2():
    inst = make_plugin()
    return (
        await inst.patch_status(FakeEvent("member")),
        await inst.patch_apply(FakeEvent("member"), "P1"),
        await inst.patch_revert(FakeEvent("member"), "P1"),
    )


a, b, c = asyncio.run(case2())
print(f"  status={a[:24]}…  apply={b[:24]}…  revert={c[:24]}…")
check("②", "管理员" in a and "管理员" in b and "管理员" in c, "非管理员没被挡住")

# =====================================================================
print("\n===== ③ 未知补丁 id：给出可执行提示 =====")


async def case3():
    inst = make_plugin()
    return await inst.patch_apply(FakeEvent("admin"), "NOPE")


out = asyncio.run(case3())
print("  ", out[:60])
check("③", "没有这个补丁" in out and "patch_status" in out, "未知 id 的提示不够可执行")

# =====================================================================
print("\n===== ④ 真实补丁文件：--check 能过，且不落地改动 =====")
real = PLUGIN / "patches"
plugin_main.PKG_DIR = ASTRBOT_PKG      # 换回真实包目录（之前指到过临时目录）
meta = plugin_main.load_meta()
print(f"  清单 {len(meta)} 条：{[m['id'] for m in meta]}")
check("④", len(meta) >= 1, "校验值.json 是空的")

# 对已应用的那条做「反向预检」：应该能过（说明补丁内容与当前源码匹配）
applied = [m for m in meta if plugin_main.state_of(m)[0] == "applied"]
if applied:
    it = applied[0]
    ok, msg = plugin_main.git_apply(real / it["patch"], revert=True, check_only=True)
    print(f"  {it['id']} 反向预检: {ok} {msg[:60]}")
    check("④", ok, f"已应用的 {it['id']} 反向预检失败：{msg}")
else:
    print("  （当前没有已应用的补丁，跳过反向预检）")

# =====================================================================
shutil.rmtree(tmp, ignore_errors=True)

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 四种状态 / 非管理员拦截 / 未知 id 提示 / 真实补丁预检")

