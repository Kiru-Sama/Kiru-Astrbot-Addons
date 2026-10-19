# -*- coding: utf-8 -*-
"""回归护栏：按会话类型选人格（私聊一套、其它一套）。

需求（2026-10-14 确认）：
    · 私聊 → 插件配置里的「私密人格」
    · **其它一切**（群聊、OtherMessage 等）→ 「公开人格」  ← 兜底
    · 人格内容仍然在 AstrBot 的人格库里维护，插件只存"名字"
    · **会话手动指定过人格 → 尊重它，不覆盖**
    · 两个配置都留空 = 完全不干预（保持 AstrBot 原行为）

实现接缝：包装 PersonaManager.resolve_selected_persona —— 它是 AstrBot 解析人格的
唯一入口，而且签名里带 umo（`平台:MessageType:会话`），所以**不需要改提示词**。

★ 为什么不在 on_llm_request 里改：那个钩子跑在人格【已经拼进 system_prompt】之后，
  那时只能做字符串替换，脆得很。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-人格分流.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""

import asyncio
import pathlib
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402
from astrbot.core.persona_mgr import PersonaManager  # noqa: E402

failures: list[str] = []

PUBLIC = "普通人格"
PRIVATE = "私密人格"
FALLBACK = "全局默认人格"

UMO_GROUP = "ncd-app_x:GroupMessage:100000001"
UMO_FRIEND = "ncd-app_x:FriendMessage:100000002"
UMO_OTHER = "ncd-app_x:OtherMessage:100000003"


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


# ---------------------------------------------------------------------------
#  假 PersonaManager：只带 get_persona_v3_by_id 需要的那点状态
# ---------------------------------------------------------------------------
class FakeMgr:
    def __init__(self):
        self.personas_v3 = [
            {"name": PUBLIC},
            {"name": PRIVATE},
            {"name": FALLBACK},
        ]

    def get_persona_v3_by_id(self, persona_id):
        if not persona_id:
            return None
        return next(
            (p for p in self.personas_v3 if p["name"] == persona_id), None
        )


async def fake_original(
    self_, *, umo, conversation_persona_id, platform_name, provider_settings=None
):
    """冒充"真正的 AstrBot 解析"：一律返回全局默认人格。

    真实实现里 conversation_persona_id=None 时就是回退到全局默认 ——
    这里简化成固定返回，好让断言看得清"到底是插件选的还是 AstrBot 选的"。
    """
    return (FALLBACK, {"name": FALLBACK}, None, False)


async def fake_original_forced(
    self_, *, umo, conversation_persona_id, platform_name, provider_settings=None
):
    """会话被显式指定过人格（含 "[%None]"）/ 被 session rule 强制。"""
    return (conversation_persona_id, {"name": conversation_persona_id}, None, False)


def make_plugin(private_name: str, public_name: str):
    inst = plugin_main.Main.__new__(plugin_main.Main)
    inst._conf = {"persona_private": private_name, "persona_public": public_name}
    return inst


async def call(mgr, umo, conversation_persona_id=None, platform_name="aiocqhttp"):
    return await PersonaManager.resolve_selected_persona(
        mgr,
        umo=umo,
        conversation_persona_id=conversation_persona_id,
        platform_name=platform_name,
    )


async def scenario(original, plugin, umo, conversation_persona_id=None):
    """在"包装 → 调用 → 还原"之间跑一次，避免用例之间互相污染。"""
    cls = PersonaManager
    cls.__dict__.get("_ncd_monitor_patched") and delattr(cls, "_ncd_monitor_patched")
    cls.resolve_selected_persona = original
    plugin_main._ACTIVE = plugin
    plugin._patch_persona()
    try:
        return await call(FakeMgr(), umo, conversation_persona_id)
    finally:
        # 还原：把类方法恢复成原始实现（同一个进程里后面还要用）
        if "_ncd_monitor_patched" in cls.__dict__:
            delattr(cls, "_ncd_monitor_patched")


def name_of(result) -> str:
    return result[0]


# =====================================================================
#  用例 1：私聊 → 私密人格；群聊 / 其它 → 公开人格（兜底）
# =====================================================================
print("===== 用例 1：按会话类型分流 =====")
for label, umo, expect in (
    ("私聊", UMO_FRIEND, PRIVATE),
    ("群聊", UMO_GROUP, PUBLIC),
    ("其它类型（兜底）", UMO_OTHER, PUBLIC),
):
    r = asyncio.run(scenario(fake_original, make_plugin(PRIVATE, PUBLIC), umo))
    print(f"  {label:16s} umo={umo.split(':', 1)[1]:28s} → {name_of(r)!r}")
    check("用例1", name_of(r) == expect, f"{label}应选 {expect!r}，实际 {name_of(r)!r}")
    # 返回的对象必须是真实人格对象，不能只有名字
    check("用例1", (r[1] or {}).get("name") == expect, f"{label}没返回人格对象")

# =====================================================================
#  用例 2：会话手动指定过 → 尊重，不覆盖
# =====================================================================
print("\n===== 用例 2：尊重手动指定 =====")
for label, cpid in (
    ("指定了具体人格", "手动选的人格"),
    ("明确设为不设人格", "[%None]"),
):
    r = asyncio.run(
        scenario(fake_original_forced, make_plugin(PRIVATE, PUBLIC), UMO_GROUP, cpid)
    )
    print(f"  {label:16s} conversation_persona_id={cpid!r} → {name_of(r)!r}")
    check("用例2", name_of(r) == cpid, f"★{label}被插件覆盖了：实际 {name_of(r)!r}")
    check("用例2", name_of(r) != PUBLIC, f"★{label}被换成了公开人格")

# =====================================================================
#  用例 3：两个配置都留空 → 完全不干预
# =====================================================================
print("\n===== 用例 3：留空 = 不干预 =====")
for umo in (UMO_FRIEND, UMO_GROUP):
    r = asyncio.run(scenario(fake_original, make_plugin("", ""), umo))
    print(f"  umo={umo.split(':', 1)[1]:28s} → {name_of(r)!r}")
    check("用例3", name_of(r) == FALLBACK, f"留空时不该改人格，实际 {name_of(r)!r}")

# 只填一个：另一个留空的那些会话保持原样
r = asyncio.run(scenario(fake_original, make_plugin(PRIVATE, ""), UMO_GROUP))
print(f"  只填了私密人格，群聊 → {name_of(r)!r}（应保持默认）")
check("用例3", name_of(r) == FALLBACK, "只填了一个时，另一个大类应保持原行为")
r = asyncio.run(scenario(fake_original, make_plugin("", PUBLIC), UMO_FRIEND))
print(f"  只填了公开人格，私聊 → {name_of(r)!r}（应保持默认）")
check("用例3", name_of(r) == FALLBACK, "只填了一个时，另一个大类应保持原行为")

# =====================================================================
#  用例 4：人格名写错（库里没有）→ 回退原行为，不能把对话搞没
# =====================================================================
print("\n===== 用例 4：人格名不存在 =====")
r = asyncio.run(scenario(fake_original, make_plugin("不存在的人格", PUBLIC), UMO_FRIEND))
print(f"  私聊 + 不存在的人格名 → {name_of(r)!r}")
check("用例4", name_of(r) == FALLBACK, "人格名不存在时该回退到 AstrBot 原结果")

# =====================================================================
#  用例 5：fail-open —— 插件自己的判断出错，也不能改变人格解析结果
# =====================================================================
print("\n===== 用例 5：插件出错时不能影响人格解析 =====")


class ExplodingMgr:
    """读取人格列表就炸 —— 模拟插件侧自己出错。

    ★ 不继承 FakeMgr：那个 __init__ 要给 personas_v3 赋值，而这里它是会抛异常的
      只读 property，继承会先炸在构造上（这个坑我在思考剥离那个测试里刚踩过一次）。
    """

    @property
    def personas_v3(self):
        raise RuntimeError("模拟插件侧读取人格列表就炸")

    def get_persona_v3_by_id(self, persona_id):
        raise RuntimeError("模拟插件侧读取人格列表就炸")


async def scenario_exploding(plugin, umo):
    cls = PersonaManager
    if "_ncd_monitor_patched" in cls.__dict__:
        delattr(cls, "_ncd_monitor_patched")
    cls.resolve_selected_persona = fake_original
    plugin_main._ACTIVE = plugin
    plugin._patch_persona()
    try:
        return await PersonaManager.resolve_selected_persona(
            ExplodingMgr(),
            umo=umo,
            conversation_persona_id=None,
            platform_name="aiocqhttp",
        )
    finally:
        if "_ncd_monitor_patched" in cls.__dict__:
            delattr(cls, "_ncd_monitor_patched")


r = asyncio.run(scenario_exploding(make_plugin(PRIVATE, PUBLIC), UMO_FRIEND))
print(f"  插件侧抛异常时 → {name_of(r)!r}")
check("用例5", name_of(r) == FALLBACK, "★插件出错把人格解析结果改掉了")

# =====================================================================
#  用例 6：umo 不是字符串（MessageSession 之类）也不能崩
# =====================================================================
print("\n===== 用例 6：umo 是对象 =====")


class FakeSession:
    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


r = asyncio.run(
    scenario(fake_original, make_plugin(PRIVATE, PUBLIC), FakeSession(UMO_GROUP))
)
print(f"  umo 是对象（群聊）→ {name_of(r)!r}")
check("用例6", name_of(r) == PUBLIC, "umo 是对象时应仍能判出群聊")

r = asyncio.run(
    scenario(fake_original, make_plugin(PRIVATE, PUBLIC), FakeSession(UMO_FRIEND))
)
print(f"  umo 是对象（私聊）→ {name_of(r)!r}")
check("用例6", name_of(r) == PRIVATE, "umo 是对象时应仍能判出私聊")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 分流（私聊/群聊/其它兜底）/ 尊重手动 / 留空不干预 / 人格名不存在回退 "
      "/ fail-open / umo 是对象 全部正确")
