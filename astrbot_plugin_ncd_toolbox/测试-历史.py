# -*- coding: utf-8 -*-
"""回归护栏：上下文超感知（recall_history）。

要钉死：
  1. 档位归一化：只认 30/50/70/90，其它就近折算，垃圾输入回落 30
  2. 转写格式：角色名 / 图片占位 / 单条截断 / 空消息跳过 / 顺序从旧到新
  3. 总量封顶：宁可丢最早的，也不能把最近的话截掉（模型靠最近的话判断语境）
  4. 工具接线：注册名、参数、TOOL_NAMES、菜单清单

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-历史.py
"""

import asyncio
import json
import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import history as h  # noqa: E402
import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


# =====================================================================
print("===== ① 档位归一化 =====")
cases = {30: 30, 40: 30, 50: 50, 60: 50, 80: 70, 90: 90, 200: 90, 0: 30, -5: 30,
         None: 30, "x": 30, "70": 70}
for raw, want in cases.items():
    got = h.snap_count(raw)
    print(f"  {raw!r:6} -> {got}")
    check("①", got == want, f"snap_count({raw!r}) 应为 {want}，实际 {got}")
check("①", h.ALLOWED_COUNTS == (30, 50, 70, 90), f"档位定义变了：{h.ALLOWED_COUNTS}")

# =====================================================================
print("\n===== ② 转写格式 =====")
msgs = [
    {"role": "user", "content": "第一句"},
    {"role": "assistant", "content": "第二句"},
    {"role": "user", "content": [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": "x"}},
    ]},
    {"role": "tool", "content": json.dumps({"ok": True}, ensure_ascii=False)},
    {"role": "assistant", "content": "很长的" * 80},
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": ""},
    {"role": "assistant", "content": "最后一句"},
]
out = h.format_transcript(msgs, limit=30)
for line in out.splitlines():
    print("  " + line[:100])

# ★ 头部写的是【实际保留下来的条数】（空消息会被跳过），
#   所以别硬编码 8 —— 上面那条 content="" 会被过滤掉，实际是 7 条。
kept = len([l for l in out.splitlines() if l.startswith("[")])
check("②", f"最近的 {kept} 条" in out,
      f"头部条数和实际不符（实际 {kept} 条）：{out.splitlines()[0]}")
check("②", "对方: 第一句" in out, "用户角色没转成「对方」")
check("②", "我: 第二句" in out, "助手角色没转成「我」")
check("②", "[图片]" in out, "多模态里的图片没占位")
check("②", "（截断）" in out, "超长消息没截断")
check("②", "最后一句" in out, "最后一条丢了")
check("②", "系统提示" in out, "系统消息没显示")
check("②", out.index("第一句") < out.index("最后一句"), "顺序不是从旧到新")
nums = [l.split("]")[0][1:] for l in out.splitlines() if l.startswith("[")]
check("②", nums == [str(i) for i in range(1, len(nums) + 1)],
      f"编号不连续（跳号会让人以为漏了消息）：{nums}")
check("②", all("* 80" not in l for l in out.splitlines()), "超长消息没真正截断")

# =====================================================================
print("\n===== ③ 总量封顶：保留最新的 =====")
big = [{"role": "user", "content": f"第{i}条" + "x" * 200} for i in range(1, 91)]
capped = h.format_transcript(big, limit=90)
print(f"  90 条超长消息 -> 输出 {len(capped)} 字")
check("③", len(capped) <= h.TOTAL_CHARS + 400, f"没封顶：{len(capped)} 字")
check("③", "第90条" in capped, "★最新的那条被挤掉了")
check("③", "第1条" not in capped, "封顶后不该还留最早的")
check("③", "因为太长没放进来" in capped, "被截掉的部分没有说明")

# =====================================================================
print("\n===== ③b AstrBot 的内部标记要剥掉 =====")
# 实测真实历史里 user 消息长这样：
#   "你好啊 <system_reminder>User ID: ..., Nickname: ...</system_reminder>"
# 这些标记在别处已经可见，转写时再抄一遍纯烧 token。
noisy = [{"role": "user",
          "content": "你好啊 <system_reminder>User ID: 1, Nickname: x</system_reminder> 在吗"}]
txt = h.format_transcript(noisy, limit=30)
print("  " + txt.splitlines()[-1][:88])
check("③b", "system_reminder" not in txt,
      "★<system_reminder> 没被剥掉，白烧 token")
check("③b", "在吗" in txt and "你好啊" in txt, "剥噪音时把正文也弄丢了")

# =====================================================================
print("\n===== ④ 边界输入 =====")
for bad in ([], None, [{"role": "user", "content": ""}]):
    txt = h.format_transcript(bad, limit=30)
    print(f"  {str(bad)[:30]!r} -> {txt[:40]!r}")
    check("④", "没有可读" in txt or "还没有可读" in txt, f"边界输入没给出人话：{txt[:40]}")

# =====================================================================
print("\n===== ⑤ 工具接线 =====")
check("⑤", "recall_history" in plugin_main.TOOL_NAMES, "TOOL_NAMES 里没有")
check("⑤", hasattr(plugin_main.NcdToolboxPlugin, "recall_history"), "类上没有这个方法")
check("⑤", "recall_history" in getattr(plugin_main, "TOOL_SUMMARY", {}), "TOOL_SUMMARY 缺说明")


def menu() -> str:
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._revealed = {}
    inst._conf = {}
    ev = types.SimpleNamespace(unified_msg_origin="t:GroupMessage:1")
    return asyncio.run(inst.ncd_tools(ev))


text = menu()
check("⑤", "recall_history" in text, "★菜单清单里没有 recall_history")
check("⑤", "新增工具" not in text.split("recall_history")[1][:12],
      "菜单里只显示了兜底文案，说明 TOOL_SUMMARY 没覆盖到")

# =====================================================================
print("\n===== ⑥ 拿不到历史时给人话，不抛异常 =====")


class FakeCm:
    def __init__(self, cid, messages=None, boom=False, field="history"):
        self.cid = cid
        self.boom = boom
        self.field = field
        # ★ 真实对象里是 history=json.dumps(list)，是【字符串】不是列表 ——
        #   一开始我读的是 content，在生产上会永远拿到 None。
        if field == "history":
            self.history = json.dumps(messages or [], ensure_ascii=False)
        else:
            self.content = messages or []

    async def get_curr_conversation_id(self, umo):
        if self.boom:
            raise RuntimeError("db down")
        return self.cid

    async def get_conversation(self, umo, cid):
        ns = types.SimpleNamespace()
        if hasattr(self, "history"):
            ns.history = self.history
        if hasattr(self, "content"):
            ns.content = self.content
        return ns


def run_tool(cm):
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = {}
    inst._revealed = {}
    inst.context = types.SimpleNamespace(conversation_manager=cm)
    ev = types.SimpleNamespace(unified_msg_origin="t:GroupMessage:1")
    return asyncio.run(inst.recall_history(ev, 30))


print("  没有会话 id ->", run_tool(FakeCm(None))[:40])
check("⑥", "还没有对话记录" in run_tool(FakeCm(None)), "没有会话时提示不对")

print("  有历史 ->", run_tool(FakeCm("c1", [{"role": "user", "content": "你好"}])).splitlines()[0][:40])
# ★ 兼容老字段名（content 里放列表）
ok_content = run_tool(FakeCm("c1", [{"role": "user", "content": "老字段"}], field="content"))
print("  content 字段 ->", ok_content.splitlines()[0][:40])
check("⑥", "老字段" in ok_content, "兼容 content 字段失败")
check("⑥", "你好" in run_tool(FakeCm("c1", [{"role": "user", "content": "你好"}])), "正常历史没读出来")

print("  数据库炸了 ->", run_tool(FakeCm("c1", boom=True))[:40])
check("⑥", "读历史失败" in run_tool(FakeCm("c1", boom=True)), "异常没转成人话")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 档位归一 / 转写格式 / 总量封顶保住最新 / 边界 / 接线 / 异常兜底")
