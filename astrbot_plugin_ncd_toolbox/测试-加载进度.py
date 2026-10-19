# -*- coding: utf-8 -*-
"""回归护栏：聊天里的进度汇报（加载期静默 + 去重 + 条数上限）。

为什么要有这个文件：
    2026-10-18 用户反馈「启动中那个别急着报，等个10秒再报，不然都是0%」——
    worker 先起 HTTP 服务、再在后台线程加载模型，最前面几秒查到的是
    `启动中 0%`；旧代码每 5 秒报一次，开局就是两条 0%，看着像卡死。

本测试验证：
  1. 加载期前 10 秒（sd_load_grace）一条都不报
  2. 静默期过后只报【百分比动了】的进度，同一档不重复
  3. 加载最多 2 条；出图最多 2 条，各自独立计数
  4. 就绪但还没开始出图时闭嘴（不提前收工，等它真忙起来）
  5. 出图抛异常时进度任务会被 cancel 掉（以前只在成功路径 cancel）

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-加载进度.py
"""

import asyncio
import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

# 这个测试会故意让出图抛异常，AstrBot 的 logger 会打一大段 traceback 把
# 断言输出淹掉 —— 换个静音壳。
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


def snap(**kw) -> dict:
    """造一次 /progress 快照。"""
    base = {"ok": True, "busy": False, "ready": False, "phase": "启动中",
            "phase_pct": 0, "step": 0, "total": 0, "elapsed": 0.0}
    base.update(kw)
    return base


def make_plugin(**conf):
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = dict({"sd_progress_interval": 5, "sd_load_grace": 10,
                       "image_timeout": 180}, **conf)
    inst._sd = None
    return inst


# =====================================================================
print("===== ① 加载期：前 10 秒一条都不报 =====")
inst = make_plugin()
st = {"max_msgs": 5, "grace": 10.0}
t0 = 1000.0
# 第一次看到"没就绪"只是开始计时，不报
print("  t+0.0s 启动中 0%      →", inst._progress_text(snap(), st, t0))
check("①", inst._progress_text(snap(), st, t0) is None, "第一次快照不该报")
out = [inst._progress_text(snap(), st, t0 + d) for d in (5, 9, 9.9)]
print("  t+5/9/9.9s 启动中 0%  →", out)
check("①", all(o is None for o in out), f"静默期内不该报，实际 {out}")

# 静默期过了，但百分比还是 0 → 依然不报（0% 没有信息量）
print("  t+10.5s 启动中 0%     →", inst._progress_text(snap(), st, t0 + 10.5))
check("①", inst._progress_text(snap(), st, t0 + 10.5) is None, "0% 不该报")

# 百分比动了 → 报
txt = inst._progress_text(snap(phase="读取权重（6.5 GB，首次可能要几十秒）", phase_pct=10),
                          st, t0 + 11)
print(f"  t+11s  读取权重 10%   → {txt}")
check("①", txt == "📦 读取权重（6.5 GB，首次可能要几十秒） 10%", f"应报 10%，实际 {txt!r}")

# =====================================================================
print("\n===== ② 同一档进度不重复刷 =====")
st = {"max_msgs": 5, "grace": 10.0}
inst._progress_text(snap(), st, t0)
inst._progress_text(snap(phase="读取权重", phase_pct=10), st, t0 + 11)
again = [inst._progress_text(snap(phase="读取权重", phase_pct=10), st, t0 + 16 + i) for i in range(3)]
print("  连续三次同一档     →", again)
check("②", all(a is None for a in again), f"同一档不该重复报，实际 {again}")

# =====================================================================
print("===== ③ 整轮最多 5 条（加载和出图共用配额） =====")
st = {"max_msgs": 5, "grace": 10.0}
inst._progress_text(snap(), st, t0)
loads = [
    inst._progress_text(snap(phase="读取权重", phase_pct=10), st, t0 + 11),
    inst._progress_text(snap(phase="装配管道组件", phase_pct=55), st, t0 + 16),
    inst._progress_text(snap(phase="搬运到显存", phase_pct=85), st, t0 + 21),
    inst._progress_text(snap(phase="就绪", phase_pct=100), st, t0 + 26),
]
print("  加载 10/55/85/100% →", loads)
check("③", all(loads), f"5 条配额还没用完，加载进度都该报，实际 {loads}")
check("③", st["sent"] == 4, f"sent 应为 4，实际 {st.get('sent')}")

gens = [
    inst._progress_text(snap(busy=True, ready=True, step=8, total=25), st, t0 + 31),
    inst._progress_text(snap(busy=True, ready=True, step=16, total=25), st, t0 + 39),
    inst._progress_text(snap(busy=True, ready=True, step=24, total=25), st, t0 + 47),
]
print("  出图 8/16/24 步    →", gens)
check("③", gens[0] == "🎨 出图中… 8/25 步", f"步数格式不对：{gens[0]!r}")
check("③", gens[1] is None and gens[2] is None, f"第 5 条之后该闭嘴，实际 {gens}")
check("③", st["sent"] == 5, f"总条数应为 5，实际 {st.get('sent')}")
check("③", st.get("done") is True, "配额用完后应让循环收工")

# 只剩出图、没有加载时，也能报满 5 条
st2 = {"max_msgs": 5, "grace": 10.0}
gen_only = [inst._progress_text(snap(busy=True, ready=True, step=i, total=25), st2, t0 + i)
            for i in range(1, 8)]
print(f"  纯出图 7 次 → 报了 {sum(1 for x in gen_only if x)} 条")
check("③", sum(1 for x in gen_only if x) == 5, "纯出图也该最多 5 条")

# ★ 图生图只跑 steps×strength 步（25×0.7=17），文案要说清楚，不然用户以为少跑了
st3 = {"max_msgs": 5, "grace": 10.0}
img = inst._progress_text(
    snap(busy=True, ready=True, step=9, total=17, mode="img2img", steps_full=25), st3, t0)
print(f"  图生图 9/17 步 → {img}")
check("③", img and "图生图" in img and "17" in img and "25" in img,
      f"★ 图生图消息没解释 17≠25：{img!r}")
check("③", "0.6" in str(img) or "0.7" in str(img) or "0.68" in str(img),
      f"没给出强度：{img!r}")

# 文生图不受影响
txt = inst._progress_text(snap(busy=True, ready=True, step=8, total=25, mode="txt2img",
                               steps_full=25), {"max_msgs": 5, "grace": 10.0}, t0)
print(f"  文生图 8/25 步 → {txt}")
check("③", txt == "🎨 出图中… 8/25 步", f"文生图文案变了：{txt!r}")

# =====================================================================
print("\n===== ④ 就绪但没在出图 → 闭嘴（不提前收工） =====")
st = {"max_msgs": 5, "grace": 10.0}
out = inst._progress_text(snap(busy=False, ready=True, phase="就绪", phase_pct=100), st, t0)
print("  ready=True busy=False →", out, " done =", st.get("done"))
check("④", out is None, f"不该报，实际 {out!r}")
check("④", not st.get("done"), "★不能在这里收工 —— 否则加载快的时候后面一步都报不出来")

# =====================================================================
print("\n===== ⑤ grace=0 也不会冒 0% =====")
inst0 = make_plugin(sd_load_grace=0)
st = {"max_msgs": 5, "grace": 0.0}
inst0._progress_text(snap(), st, t0)
out = [inst0._progress_text(snap(), st, t0 + 3), inst0._progress_text(snap(), st, t0 + 6)]
print("  grace=0，启动中 0%  →", out)
check("⑤", all(o is None for o in out), f"0% 一律不该报，实际 {out}")

# =====================================================================
print("\n===== ⑥ 出图抛异常时，进度任务会被 cancel =====")

clock_state = {"t": 1000.0}
real_sleep = asyncio.sleep


class Clock:
    def time(self):
        return clock_state["t"]


async def fake_sleep(sec, *a, **kw):
    clock_state["t"] += float(sec)
    if float(sec) <= 0:
        await real_sleep(0)      # 让出一次事件循环，模拟真实调度


class BoomClient:
    async def txt2img(self, payload):
        await asyncio.sleep(0)   # 让进度任务先跑起来
        raise RuntimeError("模拟出图炸了")

    async def img2img(self, payload):  # pragma: no cover - 用不到
        raise AssertionError


class FakeSD:
    """假的 StandaloneClient：一直在"加载中"，让进度任务活得久一点。"""

    def __init__(self):
        self.polls = 0

    async def progress(self):
        self.polls += 1
        await real_sleep(0)      # 必须真的让出一次事件循环
        return snap(phase="读取权重", phase_pct=10)


def run_boom() -> tuple[str, list, list]:
    inst = make_plugin()
    inst._sd = FakeSD()
    inst._revealed = {}
    inst._pending_images = {}
    inst._render_lock = asyncio.Lock()
    inst._last_image = None
    inst._lora_list = lambda: []
    inst._sd_client = lambda: BoomClient()

    class FakeCtx:
        async def send_message(self, umo, chain):
            return None

    inst.context = FakeCtx()
    seen: list = []
    original = inst._chat_progress

    async def spy(event, max_msgs=2):
        seen.append(asyncio.current_task())
        return await original(event, max_msgs)

    inst._chat_progress = spy
    event = types.SimpleNamespace(
        unified_msg_origin="t:GroupMessage:1",
        get_messages=lambda: [types.SimpleNamespace()],
        get_sender_id=lambda: "1", get_self_id=lambda: "2",
        get_sender_name=lambda: "x", get_group_id=lambda: "3",
    )

    plugin_main.time = Clock()
    asyncio.sleep = fake_sleep
    try:
        async def driver():
            out = await inst.generate_image(event, "1girl")
            for _ in range(3):
                await real_sleep(0)
            left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            return out, left, list(seen)
        return asyncio.run(driver())
    finally:
        asyncio.sleep = real_sleep
        plugin_main.time = __import__("time")


out, left, seen = run_boom()
tasks = [t for t in seen if t is not None]
print(f"  返回: {out[:40]!r}")
print(f"  进度任务数={len(tasks)}  未结束={[t.get_name() for t in left]}")
check("⑥", out.startswith("画图失败"), f"应返回失败说明，实际 {out[:30]}")
check("⑥", len(tasks) == 1, f"应创建 1 个进度任务，实际 {len(tasks)}")
check("⑥", all(t.done() for t in tasks), "★出图抛异常后进度任务没收掉（会继续往群里报）")
check("⑥", not left, f"还有任务没结束：{[t.get_name() for t in left]}")

# =====================================================================
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 加载静默10秒 / 只报动过的百分比 / 整轮 5 条上限 / 图生图步数标注 / 就绪不提前收工 / 异常时不留任务")


