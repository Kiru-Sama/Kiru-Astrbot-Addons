# -*- coding: utf-8 -*-
"""回归护栏：群聊指令限权补丁（patches/group_admin_commands.py）。

为什么要有这个文件：
    这个补丁要改的是 AstrBot 的 site-packages（内置指令），改错了会让机器人
    起不来、或者把 /help、/sid 一起锁死（群友就永远拿不到自己的 ID）。
    所以它的三段逻辑——识别、打上、撤回——都用假文件先钉一遍，不碰真环境。

本测试覆盖：
  1. 初始状态：/stop、/stats 未限权
  2. 打补丁：各插一行 SHARED_GROUP_ADMIN，且 /help 一行动都不动
  3. 幂等：重复打不会叠加
  4. 撤回：文件与原文逐字节一致
  5. 缺指令时不炸（AstrBot 版本变了的情况）

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-群管补丁.py
"""

import pathlib
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN / "patches"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import group_admin_commands as gac  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


SAMPLE = '''from astrbot.api.event import filter


class Main:
    @filter.command("help")
    async def help(self, event) -> None:
        """Show help message"""
        await self.help_c.help(event)

    @filter.command("reset")
    @filter.permission_type(filter.PermissionType.SHARED_GROUP_ADMIN)
    async def reset(self, message) -> None:
        await self.conversation_c.new_conv(message)

    @filter.command("stop")
    async def stop(self, message) -> None:
        """Stop agent execution"""
        await self.conversation_c.stop(message)

    @filter.command("stats")
    async def stats(self, message) -> None:
        await self.conversation_c.stats(message)
'''

# =====================================================================
print("===== ① 初始识别 =====")
status = gac.analyse(SAMPLE)
print(f"  {status}")
check("①", status == {"stop": False, "stats": False}, f"初始状态识别错了：{status}")

# =====================================================================
print("\n===== ② 打补丁 =====")
patched, added = gac.apply(SAMPLE)
print(f"  新增：{added}")
check("②", set(added) == {"stop", "stats"}, f"该改这两条：{added}")
check("②", patched.count(gac.PERM_LINE) == 3,
      f"补丁后应有 3 行权限装饰器（含 reset 原有 1 行），实际 {patched.count(gac.PERM_LINE)}")
# /help 不能被碰：它前面不该出现权限装饰器
_hi = patched.find('@filter.command("help")')
help_block = patched[max(0, _hi - 60): _hi]
print(f"  /help 前的 60 字符：{help_block!r}")
check("②", "permission_type" not in help_block, "★ /help 被误加权限（群友就看不到帮助了）")
check("②", gac.analyse(patched) == {"stop": True, "stats": True}, "打完后识别仍说未限权")

# =====================================================================
print("\n===== ③ 幂等 =====")
twice, added2 = gac.apply(patched)
print(f"  第二次新增：{added2}")
check("③", added2 == [], "★ 重复打补丁又插了一遍（会叠成两行装饰器）")
check("③", twice == patched, "重复打补丁改变了文件内容")

# =====================================================================
print("\n===== ④ 撤回 =====")
reverted, removed = gac.revert(patched)
print(f"  撤销：{removed}")
check("④", set(removed) == {"stop", "stats"}, f"应撤销这两条：{removed}")
check("④", reverted == SAMPLE, "★ 撤回后没有逐字节还原（留下残余改动）")
check("④", gac.analyse(reverted) == {"stop": False, "stats": False}, "撤回后识别仍说已限权")

# =====================================================================
print("\n===== ⑤ AstrBot 版本变了（缺指令）也不炸 =====")
other = '''class Main:
    @filter.command("help")
    async def help(self, event) -> None:
        pass
'''
try:
    st = gac.analyse(other)
    new_text, added3 = gac.apply(other)
    print(f"  识别={st} 新增={added3} 内容未变={new_text == other}")
    check("⑤", added3 == [] and new_text == other, "缺目标指令时应该原样返回")
except Exception as exc:  # noqa: BLE001
    failures.append(f"⑤ 缺指令时抛异常：{type(exc).__name__}: {exc}")

# =====================================================================
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 识别 / 打补丁（不碰 /help）/ 幂等 / 逐字节撤回 / 缺指令不炸")
