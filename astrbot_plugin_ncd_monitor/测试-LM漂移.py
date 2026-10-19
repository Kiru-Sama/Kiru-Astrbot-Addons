# -*- coding: utf-8 -*-
"""回归护栏：LM Studio 的「加载配置漂移」要能被抓到。

背景（2026-10-16 实测踩过）：
    同一个 4B 模型，一会儿是 `4 槽 × 8192`、一会儿是 `1 槽 × 24576`。
    表现成 AstrBot 突然 `Context size has been exceeded`，或者干脆不调工具。
    当时**没有任何地方记录"什么时候变的"**，只能翻 LM Studio 日志猜，查了一整轮。

本测试覆盖两件事：
    ① 原生端点 `/api/v0/models` 的解析（字段按 2026-10-17 抓的真实响应）
    ② 漂移检测：上下文变化 / tool_use 消失 / 模型卸载

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-LM漂移.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""

import pathlib
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


def make_plugin():
    """绕过 __init__：只给 _lm_signature / _lm_drift 留空。"""
    inst = plugin_main.Main.__new__(plugin_main.Main)
    inst._conf = {}
    return inst


# =====================================================================
#  用例 1：真实响应形状能否解析成 loaded 列表
#     字段名照 2026-10-17 对正在跑的 LM Studio 抓的 /api/v0/models 真实响应写，
#     不是凭记忆编的。
# =====================================================================
print("===== 用例 1：解析真实响应形状 =====")

REAL_RESPONSE = {
    "data": [
        {
            "id": "text-embedding-nomic-embed-text-v1.5",
            "object": "model",
            "type": "embeddings",
            "publisher": "nomic-ai",
            "arch": "nomic-bert",
            "compatibility_type": "gguf",
            "quantization": "Q4_K_M",
            "state": "loaded",
            "max_context_length": 2048,
            "loaded_context_length": 2048,
        },
        {
            "id": "qwen3.8-4b-distill-heretic-abliterated",
            "object": "model",
            "type": "vlm",
            "publisher": "Qwen3.8-4BDistill-Heretic-Abliterated-GGUF",
            "arch": "qwen35",
            "compatibility_type": "gguf",
            "quantization": "Q4_K_M",
            "state": "loaded",
            "max_context_length": 262144,
            "loaded_context_length": 24576,
            "capabilities": ["tool_use"],
        },
        {
            "id": "gemma-4-e4b-uncensored-hauhaucs-aggressive",
            "object": "model",
            "type": "vlm",
            "publisher": "Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-IQ4_XS-",
            "arch": "gemma4",
            "compatibility_type": "gguf",
            "quantization": "IQ4_XS",
            "state": "not-loaded",
            "max_context_length": 131072,
            "capabilities": ["tool_use"],
        },
    ]
}

# 这段逻辑在 _lm_studio_probe 里，这里按同样规则复算，确保两边口径一致
loaded = [
    {
        "id": m["id"],
        "type": m.get("type"),
        "context": m.get("loaded_context_length"),
        "max_context": m.get("max_context_length"),
        "tool_use": "tool_use" in (m.get("capabilities") or []),
        "quantization": m.get("quantization"),
    }
    for m in REAL_RESPONSE["data"]
    if m.get("state") == "loaded" and m.get("id")
]
print(f"  loaded 条目 {len(loaded)} 个（not-loaded 应被滤掉）")
for m in loaded:
    print(f"    {m['id'][:40]:42} ctx={m['context']} tool_use={m['tool_use']}")
check("用例1", len(loaded) == 2, f"应只保留 2 个 loaded（实际 {len(loaded)}）")
check("用例1", all(m["context"] for m in loaded), "loaded 的模型都该有 loaded_context_length")

# --- 对真实 LM Studio 的集成断言（没跑就明说跳过，不静默） ---
inst_live = make_plugin()
live = inst_live._lm_native_models("http://127.0.0.1:1234")
if live is None:
    print("  [跳过] LM Studio 没在跑（1234 连不上），集成断言未执行")
else:
    keys = set(live[0].keys()) if live else set()
    print(f"  真实端点返回 {len(live)} 个模型，首条字段 {len(keys)} 个")
    for need in ("id", "state", "loaded_context_length", "max_context_length"):
        check("用例1", need in keys, f"★真实响应里没有 {need} 字段（API 变了？）")

# =====================================================================
#  用例 2：第一次只建基线，不能把"启动"记成"变化"
# =====================================================================
print("\n===== 用例 2：第一次只建基线 =====")
inst = make_plugin()
first = inst._note_lm_drift(loaded)
print(f"  首次返回 {first}（应为空）")
check("用例2", first == [], f"首次不该产生漂移记录，实际 {first}")
check("用例2", len(inst.__dict__["_lm_signature"]) == 2, "基线应记下 2 个模型")

# =====================================================================
#  用例 3：loaded_context_length 变化 → 记一条
# =====================================================================
print("\n===== 用例 3：上下文长度变化 =====")
changed = [dict(m) for m in loaded]
changed[1]["context"] = 8192          # 24576 → 8192，就是当时踩的那个
recs = inst._note_lm_drift(changed)
print(f"  首次变化 → {recs}")
check("用例3", len(recs) == 1, f"时间线里应只有刚这 1 条（实际 {len(recs)}）")
if recs:
    r = recs[0]
    check("用例3", r["field"] == "loaded_context_length", f"字段名不对：{r['field']}")
    check("用例3", r["old"] == 24576 and r["new"] == 8192, f"新旧值不对：{r['old']}→{r['new']}")
    check("用例3", r["model"] == "qwen3.8-4b-distill-heretic-abliterated", "模型名不对")

# 再探一次、没变化 → 不该重复追加（返回的是时间线，所以内容应原样）
again = inst._note_lm_drift(changed)
check("用例3", len(again) == 1, f"没变化时不该重复追加（实际 {len(again)} 条）")
check("用例3", again and again[0] == recs[0], "没变化时应原样返回已有时间线")

# =====================================================================
#  用例 4：tool_use 消失 → 记一条（工具全废的那种）
# =====================================================================
print("\n===== 用例 4：tool_use 消失 =====")
no_tool = [dict(m) for m in changed]
no_tool[1]["tool_use"] = False
recs4 = inst._note_lm_drift(no_tool)
print(f"  {recs4}")
check("用例4", len(recs4) == 2, f"时间线应有 2 条（新的 tool_use + 旧的上下文，实际 {len(recs4)}）")
if recs4:
    check("用例4", recs4[0]["field"] == "tool_use", f"最新一条应是 tool_use，实际 {recs4[0]['field']}")
    check("用例4", recs4[0]["old"] is True and recs4[0]["new"] is False, "true→false 没记对")
    check("用例4", recs4[1]["field"] == "loaded_context_length", "旧记录应还在时间线里")

# =====================================================================
#  用例 5：模型卸载 → 记一条
# =====================================================================
print("\n===== 用例 5：模型卸载 =====")
unloaded = [no_tool[0]]
recs5 = inst._note_lm_drift(unloaded)
print(f"  {recs5}")
check("用例5", len(recs5) == 3, f"时间线应有 3 条（实际 {len(recs5)}）")
if recs5:
    check("用例5", recs5[0]["field"] == "卸载", f"最新一条应是卸载，实际 {recs5[0]['field']}")
    check("用例5", recs5[0]["model"] == "qwen3.8-4b-distill-heretic-abliterated", "卸载的模型名不对")

# =====================================================================
#  用例 6：记录不能无限涨；返回给面板的最多 8 条、新的在前
# =====================================================================
print("\n===== 用例 6：记录上限与顺序 =====")
inst6 = make_plugin()
base = [{"id": "m", "type": "llm", "context": 1000, "max_context": 1000, "tool_use": True}]
inst6._note_lm_drift(base)                       # 建基线
for i in range(30):                              # 造 30 次变化
    inst6._note_lm_drift([dict(base[0], context=1000 + i + 1)])
stored = inst6.__dict__["_lm_drift"]
print(f"  内部存了 {len(stored)} 条（上限应为 20）")
check("用例6", len(stored) == 20, f"内部记录应封顶 20 条（实际 {len(stored)}）")
shown = inst6._note_lm_drift([dict(base[0], context=1031)])
print(f"  返回 {len(shown)} 条，首条 new={shown[0]['new'] if shown else None}")
check("用例6", len(shown) <= 8, f"给面板的最多 8 条（实际 {len(shown)}）")
if shown:
    check("用例6", shown[0]["new"] == 1031, f"最新的一条应排最前（实际 {shown[0]['new']}）")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 真实响应解析 / 首次建基线 / 上下文变化 / tool_use 消失 / 卸载 / 记录上限与顺序 全部正确")
