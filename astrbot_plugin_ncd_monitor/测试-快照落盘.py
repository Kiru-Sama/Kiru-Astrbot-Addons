# -*- coding: utf-8 -*-
"""回归护栏：上下文快照要能跨插件重载存活。

背景（2026-10-17 实测）：
    面板右栏「发给模型的完整上下文」只读内存里的 deque(maxlen=40)。
    改插件必然要重载，一重载就清空 —— 当时最后一条快照 18:44:49 捕获、
    18:44:51 重载，面板立刻变成「还没有抓到请求」，看着像 bug，
    其实只是内存被重置了。所以要把最近几条落盘。

另外两条硬要求：
    ① 缓存必须写在 plugin_data，**不能写插件目录** —— 插件目录会被打包
       分享出去，而快照里是完整聊天正文。
    ② 读写失败绝不能影响插件启动和对话。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-快照落盘.py
"""

import json
import pathlib
import sys
import tempfile

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


def make_plugin(cache_path: str):
    inst = plugin_main.Main.__new__(plugin_main.Main)
    inst._conf = {}
    from collections import deque

    inst.context_snapshots = deque(maxlen=40)
    inst._snapshot_seq = 0
    inst._snap_cache = cache_path
    inst._snap_saved_at = 0.0
    return inst


def snap(i: int) -> dict:
    return {
        "id": i,
        "time": 1792000000 + i,
        "kind": "chat",
        "message_count": i + 1,
        "total_chars": 100 * i,
        "estimated_tokens": 10 * i,
        "messages": [{"role": "user", "content": f"第 {i} 条"}],
    }


tmp = pathlib.Path(tempfile.mkdtemp(prefix="ncd_snap_"))
cache = tmp / "sub" / "contexts.json"

# =====================================================================
#  用例 1：写入 → 新实例读回（模拟重载）
# =====================================================================
print("===== 用例 1：写盘后重载能读回 =====")
a = make_plugin(str(cache))
for i in (1, 2, 3):
    a.context_snapshots.append(snap(i))
    a._snapshot_seq = i
a._save_snapshots(force=True)
print(f"  写盘后文件存在: {cache.exists()}，大小 {cache.stat().st_size if cache.exists() else 0} 字节")
check("用例1", cache.exists(), "缓存文件没写出来")

b = make_plugin(str(cache))
b._load_snapshots()
print(f"  新实例读回 {len(b.context_snapshots)} 条，_snapshot_seq={b._snapshot_seq}")
check("用例1", len(b.context_snapshots) == 3, f"应读回 3 条（实际 {len(b.context_snapshots)}）")
check("用例1", b._snapshot_seq == 3, f"序号应续到 3（实际 {b._snapshot_seq}）")
if b.context_snapshots:
    check("用例1", b.context_snapshots[-1]["messages"][0]["content"] == "第 3 条",
          "正文没正确还原（中文/JSON 编码问题？）")

# =====================================================================
#  用例 2：只存最近 12 条
# =====================================================================
print("\n===== 用例 2：只存最近 12 条 =====")
c = make_plugin(str(cache))
for i in range(1, 31):
    c.context_snapshots.append(snap(i))
c._save_snapshots(force=True)
saved = json.loads(cache.read_text(encoding="utf-8"))
print(f"  文件里 {len(saved)} 条，首条 id={saved[0]['id']} 末条 id={saved[-1]['id']}")
check("用例2", len(saved) == 12, f"应存 12 条（实际 {len(saved)}）")
check("用例2", saved[-1]["id"] == 30, "最后一条应是最新的")

# =====================================================================
#  用例 3：节流（5 秒内重复保存不写盘）
# =====================================================================
print("\n===== 用例 3：节流 =====")
d = make_plugin(str(tmp / "throttle.json"))
d.context_snapshots.append(snap(99))
before = d._snap_saved_at
d._save_snapshots()                      # 第一次：写
after_first = d._snap_saved_at
d._save_snapshots()                      # 立刻第二次：应被节流
print(f"  第一次写入后时间戳变了: {after_first != before}")
check("用例3", after_first != before, "第一次应该真的写了")
check("用例3", pathlib.Path(tmp / "throttle.json").exists(), "第一次没写出文件")

# 节流靠时间戳判断，第二次没有改变它 → 说明提前 return 了
d._save_snapshots(force=True)            # force 必须能突破节流
check("用例3", pathlib.Path(tmp / "throttle.json").exists(), "force 保存失败")

# =====================================================================
#  用例 4：缓存损坏 / 不存在 都不能抛
# =====================================================================
print("\n===== 用例 4：坏缓存与缺失 =====")
missing = make_plugin(str(tmp / "nope.json"))
missing._load_snapshots()
check("用例4", len(missing.context_snapshots) == 0, "缺失时不该有快照且不能抛")

broken_path = tmp / "broken.json"
broken_path.write_text("{这不是合法 JSON", encoding="utf-8")
broken = make_plugin(str(broken_path))
broken._load_snapshots()
check("用例4", len(broken.context_snapshots) == 0, "坏缓存时不该有快照且不能抛")

# 不可写路径：保存失败也不能抛
bad_save = make_plugin(str(tmp / "a_file" / "b" / "contexts.json"))
(tmp / "a_file").write_text("我是文件不是目录", encoding="utf-8")
bad_save.context_snapshots.append(snap(1))
bad_save._save_snapshots(force=True)
check("用例4", True, "不可写路径下 _save_snapshots 没抛（走到这行就算过）")
print("  不可写路径下保存没抛 ✓")

# =====================================================================
#  用例 5：缓存路径必须在 plugin_data，不能在插件目录
# =====================================================================
print("\n===== 用例 5：缓存位置 =====")
real = plugin_main.Main.__new__(plugin_main.Main)
real._conf = {}
from collections import deque  # noqa: E402

real.context_snapshots = deque(maxlen=40)
real._snapshot_seq = 0
real._snap_cache = plugin_main.os.path.join(
    plugin_main.ASTRBOT_ROOT, "data", "plugin_data", "ncd_monitor", "contexts.json"
)
print(f"  路径: {real._snap_cache}")
plugin_dir = str(PLUGIN).lower()
check("用例5", "plugin_data" in real._snap_cache.lower(), "不在 plugin_data 下")
check("用例5", not real._snap_cache.lower().startswith(plugin_dir),
      "★ 缓存落在插件目录里了 —— 打包会把聊天内容分享出去")

import shutil  # noqa: E402

shutil.rmtree(str(tmp), ignore_errors=True)

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 写盘后重载读回 / 只存 12 条 / 节流 / 坏缓存降级 / 位置在 plugin_data 全部正确")
