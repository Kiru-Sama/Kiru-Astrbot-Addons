# -*- coding: utf-8 -*-
"""回归护栏：按会话类型选知识库（私聊一套、其它一套）。

需求（2026-10-16 确认）：
    · 私聊 → 插件配置里的「私聊知识库」
    · **其它一切**（群聊、OtherMessage 等）→ 「公开知识库」  ← 兜底
    · 知识库内容仍然由 AstrBot 的知识库管理，插件只存"名字"
    · **会话手动绑定过知识库 → 尊重它，不覆盖**
    · 两个配置都留空 = 完全不干预（保持 AstrBot 原行为）
    · 名字对不上 → 回退原逻辑，记 WARNING

实现接缝：包装 astr_main_agent.retrieve_knowledge_base ——
    · kb_mgr.retrieve 的签名里没有 umo，判不了会话类型；
    · 这个模块级函数是 _apply_kb 取知识库的唯一入口，且 _apply_kb 在调用点
      才解析模块全局名，所以换掉模块属性就能生效。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-知识库分流.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""

import asyncio
import pathlib
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402
import astrbot.core.astr_main_agent as ama  # noqa: E402

failures: list[str] = []

PUBLIC_KB = "公开知识库"
PRIVATE_KB = "私聊知识库"

UMO_GROUP = "ncd-app_x:GroupMessage:100000001"
UMO_FRIEND = "ncd-app_x:FriendMessage:100000002"
UMO_OTHER = "ncd-app_x:OtherMessage:100000003"

ORIGINAL_RESULT = "<AstrBot 原逻辑的结果>"


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


# ---------------------------------------------------------------------------
#  假对象
# ---------------------------------------------------------------------------
class FakeKB:
    def __init__(self, name):
        self.kb_name = name
        self.kb_id = f"id-{name}"


class FakeKBManager:
    """只实现插件真正会用到的那两个方法（外加 _format_context）。"""

    def __init__(self, existing: set[str], context_text: str = "<检索到的内容>",
                 hits=None, format_explodes=False):
        self.existing = set(existing)
        self.context_text = context_text
        self.hits = hits if hits is not None else [
            {"kb_name": "库", "doc_name": "doc1", "content": "正文1", "score": 0.9},
        ]
        self.format_explodes = format_explodes
        self.retrieve_calls: list[dict] = []
        self.format_calls: list[list] = []
        self.explode_on_retrieve = False

    async def get_kb_by_name(self, name):
        return FakeKB(name) if name in self.existing else None

    async def retrieve(self, *, query, kb_names, top_k_fusion, top_m_final):
        if self.explode_on_retrieve:
            raise RuntimeError("模拟检索炸了")
        self.retrieve_calls.append(
            {
                "query": query,
                "kb_names": list(kb_names),
                "top_k_fusion": top_k_fusion,
                "top_m_final": top_m_final,
            }
        )
        return {
            "context_text": self.context_text,
            "results": [dict(h) for h in self.hits],
        }

    def _format_context(self, results):
        if self.format_explodes:
            raise RuntimeError("模拟 AstrBot 改了排版函数")
        self.format_calls.append(list(results))
        return "FILTERED::" + "|".join(r.doc_name for r in results)


class FakeContext:
    def __init__(self, kb_mgr, cfg=None, with_kb_manager=True):
        self._cfg = cfg if cfg is not None else {
            "kb_final_top_k": 4,
            "kb_fusion_top_k": 17,
        }
        if with_kb_manager:
            self.kb_manager = kb_mgr

    def get_config(self, umo=None):
        return self._cfg


class FakeSP:
    """冒充 SharedPreferences：只关心 session_get(umo, "kb_config")。"""

    def __init__(self, kb_config=None, explode=False):
        self._kb_config = kb_config
        self._explode = explode
        self.calls: list[str] = []

    async def session_get(self, umo, key=None, default=None):
        self.calls.append(key)
        if self._explode:
            raise RuntimeError("模拟读会话偏好炸了")
        if key == "kb_config" and self._kb_config is not None:
            return self._kb_config
        return default


class FakeSession:
    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


# ---------------------------------------------------------------------------
#  场景跑法：包装 → 调用 → 还原，用例之间不互相污染
# ---------------------------------------------------------------------------
async def fake_original(query, umo, context):
    """冒充 AstrBot 的原实现。"""
    return ORIGINAL_RESULT


def make_plugin(private_kb: str, public_kb: str, threshold=0):
    inst = plugin_main.Main.__new__(plugin_main.Main)
    inst._conf = {
        "kb_private": private_kb,
        "kb_public": public_kb,
        "kb_score_threshold": threshold,
    }
    return inst


def run(plugin, kb_mgr, umo, kb_config=None, cfg=None, with_kb_manager=True,
        original=None, sp_explode=False, query="测试查询", patch_twice=False):
    """返回 (结果, FakeSP)。"""
    fake_sp = FakeSP(kb_config, explode=sp_explode)
    saved_orig = ama.retrieve_knowledge_base
    saved_sp = plugin_main.sp
    saved_wraps = list(plugin_main._WRAPS)

    if "_ncd_monitor_patched" in ama.__dict__:
        delattr(ama, "_ncd_monitor_patched")
    ama.retrieve_knowledge_base = original or fake_original
    plugin_main.sp = fake_sp
    plugin_main._ACTIVE = plugin
    try:
        plugin._patch_kb()
        if patch_twice:
            plugin._patch_kb()
        result = asyncio.run(
            ama.retrieve_knowledge_base(
                query, umo, FakeContext(kb_mgr, cfg, with_kb_manager)
            )
        )
    finally:
        ama.retrieve_knowledge_base = saved_orig
        plugin_main.sp = saved_sp
        plugin_main._WRAPS[:] = saved_wraps
        if "_ncd_monitor_patched" in ama.__dict__:
            delattr(ama, "_ncd_monitor_patched")
    return result, fake_sp


# =====================================================================
#  用例 1：私聊 → 私聊知识库；群聊 / 其它 → 公开知识库（兜底）
# =====================================================================
print("===== 用例 1：按会话类型分流 =====")
for label, umo, expect_kb in (
    ("私聊", UMO_FRIEND, PRIVATE_KB),
    ("群聊", UMO_GROUP, PUBLIC_KB),
    ("其它类型（兜底）", UMO_OTHER, PUBLIC_KB),
):
    mgr = FakeKBManager({PUBLIC_KB, PRIVATE_KB})
    result, _ = run(make_plugin(PRIVATE_KB, PUBLIC_KB), mgr, umo)
    got = mgr.retrieve_calls[0]["kb_names"] if mgr.retrieve_calls else None
    print(f"  {label:16s} → kb_names={got}")
    check("用例1", got == [expect_kb], f"{label}应检索 {expect_kb!r}，实际 {got!r}")
    check("用例1", result != ORIGINAL_RESULT, f"{label}没接管，走了原逻辑")

# =====================================================================
#  用例 2：两个配置都留空 / 只填一个 → 不干预
# =====================================================================
print("\n===== 用例 2：留空 = 不干预 =====")
for label, plugin, umo in (
    ("都留空 + 私聊", make_plugin("", ""), UMO_FRIEND),
    ("都留空 + 群聊", make_plugin("", ""), UMO_GROUP),
    ("只填私聊 + 群聊", make_plugin(PRIVATE_KB, ""), UMO_GROUP),
    ("只填公开 + 私聊", make_plugin("", PUBLIC_KB), UMO_FRIEND),
):
    mgr = FakeKBManager({PUBLIC_KB, PRIVATE_KB})
    result, _ = run(plugin, mgr, umo)
    print(f"  {label:18s} → {result!r}  检索次数={len(mgr.retrieve_calls)}")
    check("用例2", result == ORIGINAL_RESULT, f"{label}不该接管")
    check("用例2", not mgr.retrieve_calls, f"{label}不该真去检索")

# =====================================================================
#  用例 3：会话手动绑定过 → 尊重，不覆盖
# =====================================================================
print("\n===== 用例 3：尊重手动绑定 =====")
for label, kb_config in (
    ("手动绑了别的库", {"kb_ids": ["someone-else"]}),
    ("明确设为不用知识库", {"kb_ids": []}),
):
    mgr = FakeKBManager({PUBLIC_KB, PRIVATE_KB})
    result, _ = run(make_plugin(PRIVATE_KB, PUBLIC_KB), mgr, UMO_FRIEND,
                    kb_config=kb_config)
    print(f"  {label:22s} → {result!r}  检索次数={len(mgr.retrieve_calls)}")
    check("用例3", result == ORIGINAL_RESULT, f"★{label}被插件插手了")
    check("用例3", not mgr.retrieve_calls, f"★{label}不该真去检索")

# =====================================================================
#  用例 4：知识库名对不上 → 回退原逻辑
# =====================================================================
print("\n===== 用例 4：知识库名不存在 =====")
mgr = FakeKBManager({PUBLIC_KB})  # 私聊那个库不存在
result, _ = run(make_plugin("拼错的名字", PUBLIC_KB), mgr, UMO_FRIEND)
print(f"  私聊 + 不存在的库名 → {result!r}  检索次数={len(mgr.retrieve_calls)}")
check("用例4", result == ORIGINAL_RESULT, "库名不存在时该回退原逻辑")
check("用例4", not mgr.retrieve_calls, "库名不存在时不该硬检索")

# =====================================================================
#  用例 5：逗号分隔多个库 + 过滤掉不存在的
# =====================================================================
print("\n===== 用例 5：逗号分隔多个知识库 =====")
mgr = FakeKBManager({"库A", "库C"})
result, _ = run(make_plugin("库A, 不存在的库B ,库C", ""), mgr, UMO_FRIEND)
got = mgr.retrieve_calls[0]["kb_names"] if mgr.retrieve_calls else None
print(f"  配置 '库A, 不存在的库B ,库C' → kb_names={got}")
check("用例5", got == ["库A", "库C"], f"应保留存在的两个并去空白，实际 {got!r}")

# =====================================================================
#  用例 6：检索条数沿用 AstrBot 全局配置，不另起一套
# =====================================================================
print("\n===== 用例 6：检索参数 =====")
mgr = FakeKBManager({PRIVATE_KB})
run(make_plugin(PRIVATE_KB, ""), mgr, UMO_FRIEND,
    cfg={"kb_final_top_k": 9, "kb_fusion_top_k": 33})
call = mgr.retrieve_calls[0]
print(f"  top_m_final={call['top_m_final']} top_k_fusion={call['top_k_fusion']}")
check("用例6", call["top_m_final"] == 9, "top_m_final 应取 kb_final_top_k")
check("用例6", call["top_k_fusion"] == 33, "top_k_fusion 应取 kb_fusion_top_k")
check("用例6", call["query"] == "测试查询", "query 应原样透传")

# 配置缺失时用 AstrBot 的默认值 5 / 20
mgr = FakeKBManager({PRIVATE_KB})
run(make_plugin(PRIVATE_KB, ""), mgr, UMO_FRIEND, cfg={})
call = mgr.retrieve_calls[0]
print(f"  配置为空时 → top_m_final={call['top_m_final']} "
      f"top_k_fusion={call['top_k_fusion']}")
check("用例6", (call["top_m_final"], call["top_k_fusion"]) == (5, 20),
      "缺配置时应回退 AstrBot 的 5 / 20")

# =====================================================================
#  用例 7：没检到内容 → 返回 None（不能把原逻辑的结果放回来）
# =====================================================================
print("\n===== 用例 7：没检到内容 =====")
mgr = FakeKBManager({PRIVATE_KB}, context_text="")
result, _ = run(make_plugin(PRIVATE_KB, ""), mgr, UMO_FRIEND)
print(f"  私有库检索为空 → {result!r}")
check("用例7", result is None, "★检不到时应返回 None，而不是原逻辑的结果")

# =====================================================================
#  用例 8：fail-open —— 插件侧出错不能影响知识库注入结果
# =====================================================================
print("\n===== 用例 8：插件出错时回退原逻辑 =====")
mgr = FakeKBManager({PRIVATE_KB})
result, _ = run(make_plugin(PRIVATE_KB, ""), mgr, UMO_FRIEND, sp_explode=True)
print(f"  读会话偏好抛异常 → {result!r}")
check("用例8", result == ORIGINAL_RESULT, "★读会话偏好出错时应回退原逻辑")

mgr = FakeKBManager({PRIVATE_KB})
mgr.explode_on_retrieve = True
result, _ = run(make_plugin(PRIVATE_KB, ""), mgr, UMO_FRIEND)
print(f"  检索本身抛异常 → {result!r}")
check("用例8", result == ORIGINAL_RESULT, "★检索出错时应回退原逻辑")

mgr = FakeKBManager({PRIVATE_KB})
result, _ = run(make_plugin(PRIVATE_KB, ""), mgr, UMO_FRIEND, with_kb_manager=False)
print(f"  context 上没有 kb_manager → {result!r}")
check("用例8", result == ORIGINAL_RESULT, "没有 kb_manager 时应回退原逻辑")

# =====================================================================
#  用例 9：umo 是对象（MessageSession 之类）也不能崩
# =====================================================================
print("\n===== 用例 9：umo 是对象 =====")
mgr = FakeKBManager({PUBLIC_KB, PRIVATE_KB})
_, _ = run(make_plugin(PRIVATE_KB, PUBLIC_KB), mgr, FakeSession(UMO_GROUP))
got = mgr.retrieve_calls[0]["kb_names"] if mgr.retrieve_calls else None
print(f"  umo 是对象（群聊）→ kb_names={got}")
check("用例9", got == [PUBLIC_KB], "umo 是对象时应仍能判出群聊")

mgr = FakeKBManager({PUBLIC_KB, PRIVATE_KB})
_, _ = run(make_plugin(PRIVATE_KB, PUBLIC_KB), mgr, FakeSession(UMO_FRIEND))
got = mgr.retrieve_calls[0]["kb_names"] if mgr.retrieve_calls else None
print(f"  umo 是对象（私聊）→ kb_names={got}")
check("用例9", got == [PRIVATE_KB], "umo 是对象时应仍能判出私聊")

# =====================================================================
#  用例 10：重复包装只包一次；卸载后模块属性还原
# =====================================================================
print("\n===== 用例 10：幂等 + 可还原 =====")
mgr = FakeKBManager({PRIVATE_KB})
run(make_plugin(PRIVATE_KB, ""), mgr, UMO_FRIEND, patch_twice=True)
# patch_twice=True 时第二次 _patch_kb 应该被标记挡住；检索仍然只发生一次
print(f"  连续包两次 → 检索次数={len(mgr.retrieve_calls)}")
check("用例10", len(mgr.retrieve_calls) == 1, "包两次不该导致重复检索")

# 真正走 _unwrap_all，确认模块属性被还原
saved_orig = ama.retrieve_knowledge_base
saved_sp = plugin_main.sp
saved_wraps = list(plugin_main._WRAPS)
if "_ncd_monitor_patched" in ama.__dict__:
    delattr(ama, "_ncd_monitor_patched")
ama.retrieve_knowledge_base = fake_original
plugin_main.sp = FakeSP()
plugin_main._ACTIVE = make_plugin(PRIVATE_KB, "")
try:
    plugin_main._ACTIVE._patch_kb()
    patched = ama.retrieve_knowledge_base
    n = plugin_main._unwrap_all()
    restored = ama.retrieve_knowledge_base
    marker_left = "_ncd_monitor_patched" in ama.__dict__
    print(f"  包装后与原函数不同: {patched is not fake_original}")
    print(f"  _unwrap_all 还原了 {n} 处；还原后是原函数: {restored is fake_original}")
    print(f"  标记已清掉: {not marker_left}")
    check("用例10", patched is not fake_original, "包装后模块属性应被换掉")
    check("用例10", restored is fake_original, "★_unwrap_all 没把模块属性还原回去")
    check("用例10", not marker_left, "★_unwrap_all 没清掉标记")
finally:
    ama.retrieve_knowledge_base = saved_orig
    plugin_main.sp = saved_sp
    plugin_main._WRAPS[:] = saved_wraps
    if "_ncd_monitor_patched" in ama.__dict__:
        delattr(ama, "_ncd_monitor_patched")

# =====================================================================
#  用例 11-16：相关度阈值
#     背景：融合分 = 0.9 × min-max归一化稠密分 + 0.1 × min-max归一化稀疏分，
#     归一化保证每次的第一名都是满分。实测：
#         '你好啊'               → 0.90 / 0.61 / 0.54
#         '支配方羞辱对方的台词'  → 0.99 / 0.43 / 0.41
#     所以阈值只能砍尾部，砍不掉"第一名本身就是垃圾"。
# =====================================================================
print("\n===== 用例 11：阈值 0（默认）= 不过滤 =====")
HITS_VAGUE = [
    {"kb_name": "库", "doc_name": "docA", "content": "正文A", "score": 0.90},
    {"kb_name": "库", "doc_name": "docB", "content": "正文B", "score": 0.61},
    {"kb_name": "库", "doc_name": "docC", "content": "正文C", "score": 0.54},
]
mgr = FakeKBManager({PRIVATE_KB}, context_text="原样文本", hits=HITS_VAGUE)
result, _ = run(make_plugin(PRIVATE_KB, "", 0), mgr, UMO_FRIEND)
print(f"  阈值0 → {result!r}  重排版次数={len(mgr.format_calls)}")
check("用例11", result == "原样文本", "阈值 0 时应原样用 AstrBot 的文本")
check("用例11", not mgr.format_calls, "阈值 0 时不该重新排版")

print("\n===== 用例 12：阈值 0.65 砍掉弱尾部 =====")
mgr = FakeKBManager({PRIVATE_KB}, context_text="原样文本", hits=HITS_VAGUE)
result, _ = run(make_plugin(PRIVATE_KB, "", 0.65), mgr, UMO_FRIEND)
print(f"  0.90/0.61/0.54 @0.65 → {result!r}")
check("用例12", result == "FILTERED::docA", f"应只留 docA，实际 {result!r}")
check("用例12", len(mgr.format_calls) == 1, "筛掉了就该重排版一次")
check("用例12", len(mgr.format_calls[0]) == 1, "重排版只该拿到保留的块")

print("\n===== 用例 13：阈值 0.6 保留两个 =====")
# ★ 用 0.6 而不是 0.5：0.54 >= 0.50 是成立的，阈值 0.5 时三个都会被保留
#   （第一版这个用例就是写错在这一点上，测试先跑出来才发现）。
mgr = FakeKBManager({PRIVATE_KB}, context_text="原样文本", hits=HITS_VAGUE)
result, _ = run(make_plugin(PRIVATE_KB, "", 0.6), mgr, UMO_FRIEND)
print(f"  0.90/0.61/0.54 @0.60 → {result!r}")
check("用例13", result == "FILTERED::docA|docB", f"应留 A 和 B，实际 {result!r}")

# 阈值正好等于某个分数时，那一块【保留】（>= 而不是 >）
mgr = FakeKBManager({PRIVATE_KB}, context_text="原样文本", hits=HITS_VAGUE)
result, _ = run(make_plugin(PRIVATE_KB, "", 0.61), mgr, UMO_FRIEND)
print(f"  0.90/0.61/0.54 @0.61（边界）→ {result!r}")
check("用例13b", result == "FILTERED::docA|docB",
      f"阈值等于分数时应保留该块（>= 语义），实际 {result!r}")

print("\n===== 用例 14：阈值把所有都筛掉 → 不注入 =====")
mgr = FakeKBManager({PRIVATE_KB}, context_text="原样文本", hits=HITS_VAGUE)
result, _ = run(make_plugin(PRIVATE_KB, "", 0.99), mgr, UMO_FRIEND)
print(f"  全低于 0.99 → {result!r}  重排版次数={len(mgr.format_calls)}")
check("用例14", result is None, "★全被筛掉时应返回 None，而不是退回原样文本")
check("用例14", not mgr.format_calls, "全筛掉时不必重排版")

print("\n===== 用例 15：阈值没筛掉任何东西 → 仍用原文本 =====")
mgr = FakeKBManager({PRIVATE_KB}, context_text="原样文本", hits=HITS_VAGUE)
result, _ = run(make_plugin(PRIVATE_KB, "", 0.5 - 0.5), mgr, UMO_FRIEND)  # 0.0
check("用例15a", result == "原样文本", "阈值 0 与不设等价")
mgr = FakeKBManager({PRIVATE_KB}, context_text="原样文本", hits=HITS_VAGUE)
result, _ = run(make_plugin(PRIVATE_KB, "", 0.1), mgr, UMO_FRIEND)
print(f"  0.10（全都够）→ {result!r}  重排版次数={len(mgr.format_calls)}")
check("用例15b", result == "原样文本", "没筛掉任何块时应原样用 AstrBot 的文本")
check("用例15b", not mgr.format_calls, "没筛掉就不该重新排版（避免格式分叉）")

print("\n===== 用例 16：重排版失败 → 退回不过滤（fail-open）=====")
mgr = FakeKBManager({PRIVATE_KB}, context_text="原样文本", hits=HITS_VAGUE,
                    format_explodes=True)
result, _ = run(make_plugin(PRIVATE_KB, "", 0.65), mgr, UMO_FRIEND)
print(f"  _format_context 抛异常 → {result!r}")
check("用例16", result == "原样文本",
      "★排版函数出问题时应退回不过滤，不能把知识库整个搞没")

print("\n===== 用例 17：阈值配置写歪 → 退回默认 0 =====")
for bad in ("", None, "abc", "高"):
    mgr = FakeKBManager({PRIVATE_KB}, context_text="原样文本", hits=HITS_VAGUE)
    result, _ = run(make_plugin(PRIVATE_KB, "", bad), mgr, UMO_FRIEND)
    check("用例17", result == "原样文本",
          f"阈值配置 {bad!r} 认不出来时应退回 0（不过滤），实际 {result!r}")
print("  ''/None/'abc'/'高' → 全部退回不过滤")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 分流（私聊/群聊/其它兜底）/ 尊重手动 / 留空不干预 / 库名不存在回退 "
      "/ 多库逗号分隔 / 检索参数沿用全局 / 空结果返回 None / fail-open "
      "/ umo 是对象 / 幂等 + 可还原 / 相关度阈值（0=不过滤、砍尾部、边界 >=、"
      "全筛掉=不注入、没筛掉不重排版、排版失败退回不过滤、配置写歪退回 0）全部正确")
