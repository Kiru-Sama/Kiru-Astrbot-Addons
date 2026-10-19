# -*- coding: utf-8 -*-
"""自测：把【当前是群聊还是私聊】明确告诉模型。

背景（2026-10-14）：
    实测人设里写了"群聊不许 NSFW"，但模型照发。查下来 AstrBot 只给了一行
    `Group name: xxx` 藏在 `<system_reminder>` 里（astr_main_agent.py:911-920），
    要模型自己推理出"有群名 = 我在群里 = 那条禁令生效"。而人设是强烈的直接指令，
    间接线索基本必输。

    所以这里【明说】：群聊/私聊 + 群名（私聊没有）+ 昵称和 QQ 号，
    每轮都注入到用户消息后面（跟 AstrBot 自己的 `<system_reminder>` 同一套做法，
    而且监控面板会把注入块单独画出来，能直接看到它有没有生效）。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-会话归属.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""

import asyncio
import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from astrbot.core.agent.tool import ToolSet  # noqa: E402

import main as plugin_main  # noqa: E402

failures: list[str] = []

GROUP_ID = "100000001"
GROUP_NAME = "测试群甲"
NICK = "测试昵称"
QQ = "100000002"


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


class FakeEvent:
    """只实现被用到的那几个方法（都是 AstrMessageEvent 上真实存在的）。"""

    def __init__(self, *, group_id="", group_name=None, nick=NICK, uid=QQ):
        self._group_id = group_id
        self.message_obj = types.SimpleNamespace(
            group_id=group_id,
            group=(
                types.SimpleNamespace(group_name=group_name)
                if group_name is not None
                else None
            ),
            sender=types.SimpleNamespace(nickname=nick, user_id=uid),
        )

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self.message_obj.sender.user_id

    def get_sender_name(self) -> str:
        return self.message_obj.sender.nickname

    def is_private_chat(self) -> bool:
        return not self._group_id


def make_req():
    return types.SimpleNamespace(
        system_prompt="你是测试助手。",
        func_tool=ToolSet(),
        extra_user_content_parts=[],
    )


def make_plugin(on: bool):
    """绕过 __init__，只放配置进去。"""
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = {"rp_mode": False, "session_header": on}
    return inst


def injected_text(req) -> str:
    out = []
    for p in req.extra_user_content_parts:
        t = getattr(p, "text", None)
        if isinstance(t, str):
            out.append(t)
    return "\n".join(out)


# =====================================================================
#  用例 1：群聊 —— 必须出现【群聊中】+ 群名 + 昵称 + QQ
# =====================================================================
print("===== 用例 1：群聊 =====")
req = make_req()
asyncio.run(
    make_plugin(True)._tune_request(
        FakeEvent(group_id=GROUP_ID, group_name=GROUP_NAME), req
    )
)
text = injected_text(req)
print("  注入内容:")
for line in text.splitlines():
    print("    " + line)
check("用例1", "群聊中" in text, "没写明这是群聊")
check("用例1", GROUP_NAME in text, "没带群名")
check("用例1", NICK in text, "没带昵称")
check("用例1", QQ in text, "没带 QQ 号")
check("用例1", text.count("群名") == 1, "群名重复或格式不对")

# =====================================================================
#  用例 2：私聊 —— 说私聊，且【不能】出现群名
# =====================================================================
print("\n===== 用例 2：私聊 =====")
req2 = make_req()
asyncio.run(make_plugin(True)._tune_request(FakeEvent(), req2))
text2 = injected_text(req2)
print("  注入内容:")
for line in text2.splitlines():
    print("    " + line)
check("用例2", "私聊中" in text2, "没写明这是私聊")
check("用例2", "群聊" not in text2, "私聊里出现了「群聊」字样，模型会搞混")
check("用例2", "群名" not in text2, "私聊里不该有群名")
check("用例2", NICK in text2 and QQ in text2, "私聊也要带昵称和 QQ")

# =====================================================================
#  用例 3：开关关掉 → 一个字都不注入
# =====================================================================
print("\n===== 用例 3：开关关掉 =====")
req3 = make_req()
asyncio.run(
    make_plugin(False)._tune_request(
        FakeEvent(group_id=GROUP_ID, group_name=GROUP_NAME), req3
    )
)
print(f"  注入块数量 = {len(req3.extra_user_content_parts)}（应为 0）")
check("用例3", not req3.extra_user_content_parts, "关掉时不该注入任何东西")

# =====================================================================
#  用例 4：拿不到群名（group 对象是 None）也不能崩，而且照样标明群聊
# =====================================================================
print("\n===== 用例 4：群聊但拿不到群名 =====")
req4 = make_req()
asyncio.run(
    make_plugin(True)._tune_request(FakeEvent(group_id=GROUP_ID, group_name=None), req4)
)
text4 = injected_text(req4)
print(f"  注入: {text4!r}")
check("用例4", "群聊中" in text4, "拿不到群名时仍必须说明是群聊")
check("用例4", "群名" not in text4, "群名拿不到就不该硬写一个空的")

# =====================================================================
#  用例 5：不能覆盖别人已经注入的块（AstrBot 自己的 system_reminder 等）
# =====================================================================
print("\n===== 用例 5：追加而不是覆盖 =====")
req5 = make_req()
req5.extra_user_content_parts.append(types.SimpleNamespace(text="别人先注入的块"))
asyncio.run(
    make_plugin(True)._tune_request(
        FakeEvent(group_id=GROUP_ID, group_name=GROUP_NAME), req5
    )
)
texts = [getattr(p, "text", "") for p in req5.extra_user_content_parts]
print(f"  现有注入块: {texts}")
check("用例5", len(req5.extra_user_content_parts) == 2, "应该是追加，不是替换")
check("用例5", texts[0] == "别人先注入的块", "把别人先注入的块挤掉了")

# =====================================================================
#  用例 6：昵称/QQ 缺失时降级，不能注入 "None"
# =====================================================================
print("\n===== 用例 6：拿不到昵称/QQ =====")
req6 = make_req()
asyncio.run(
    make_plugin(True)._tune_request(
        FakeEvent(group_id=GROUP_ID, group_name=GROUP_NAME, nick="", uid=""), req6
    )
)
text6 = injected_text(req6)
print(f"  注入: {text6!r}")
check("用例6", "None" not in text6, "★把 Python 的 None 注给模型了")
check("用例6", "群聊中" in text6 and GROUP_NAME in text6, "缺昵称时群聊信息仍要完整")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 群聊/私聊标注、群名有无、昵称与 QQ、开关、降级、追加语义全部正确")
