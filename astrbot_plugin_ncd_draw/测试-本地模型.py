# -*- coding: utf-8 -*-
"""回归护栏：用本地模型时联动禁用绘图。

为什么要有：本地小模型（4B）写 SDXL 提示词明显更差——标签少、爱塞无效质量词、
还会照抄上一次。与其让它画出废图占显存，不如本地模型下直接不给绘图工具，
换回云端模型自动恢复。

本文件钉住：判定逻辑、工具移除、以及"不该禁用的时候别乱禁"。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-本地模型.py
"""

import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeToolSet:
    def __init__(self, names):
        self._d = {n: FakeTool(n) for n in names}

    @property
    def tools(self):
        return list(self._d.values())

    def names(self):
        return list(self._d)

    def remove_tool(self, name):
        if name not in self._d:
            raise KeyError(name)
        self._d.pop(name)


def make(conf, provider_id):
    inst = plugin_main.NcdDrawPlugin.__new__(plugin_main.NcdDrawPlugin)
    inst._conf = dict(conf)

    class Ctx:
        def get_using_provider(self, umo=None):
            if provider_id is None:
                raise RuntimeError("没有可用 provider")
            return types.SimpleNamespace(provider_config={"id": provider_id})

    inst.context = Ctx()
    return inst


ev = types.SimpleNamespace(unified_msg_origin="t:GroupMessage:1")
ALL = ["drawing_tools", "generate_image", "switch_model"]

# =====================================================================
print("===== ① 怎么判定本地模型 =====")
cases = [
    ("lmstudio/qwen3.8-4b-distill-heretic-abliterated", True),
    ("lmstudio/qwen3.8-27b-gsq-rco", True),
    ("ollama/llama3", True),
    ("deepseek/deepseek-flash", False),
    ("openai/gpt-6", False),
]
for pid, want in cases:
    inst = make({}, pid)
    got, seen = inst._local_model_state("t:GroupMessage:1")
    print(f"  {pid:48} -> {got} ({seen})")
    check("①", got is want, f"{pid} 应判为 {want}，实际 {got}")

inst = make({}, None)
got, seen = inst._local_model_state("t:GroupMessage:1")
print(f"  provider 异常 -> {got}（{seen!r}）")
check("①", got is False, "★拿不到 provider 时应按云端处理，不能把绘图锁死")

inst = make({"local_model_keywords": "vllm,lmstudio"}, "vllm/qwen")
print(f"  自定义关键词 -> {inst._local_model_state('x')[0]}")
check("①", inst._local_model_state("x")[0] is True, "自定义关键词没生效")

# =====================================================================
print("\n===== ② 本地模型 → 移除 generate_image =====")
inst = make({}, "lmstudio/qwen3.8-4b")
req = types.SimpleNamespace(func_tool=FakeToolSet(ALL))
inst._hide_image_tool_for_local(req, ev)
left = req.func_tool.names()
print(f"  剩余 {len(left)} 个: {left}")
check("②", "generate_image" not in left, "★本地模型下没移除 generate_image")
check("②", len(left) == len(ALL) - 1, "不该动别的工具")
check("②", "drawing_tools" in left and "switch_model" in left, "别的工具被误删")

# =====================================================================
print("\n===== ③ 云端模型 → 不动 =====")
inst = make({}, "deepseek/deepseek-flash")
req = types.SimpleNamespace(func_tool=FakeToolSet(ALL))
inst._hide_image_tool_for_local(req, ev)
print(f"  剩余 {len(req.func_tool.names())} 个（应 {len(ALL)}）")
check("③", len(req.func_tool.names()) == len(ALL), "★云端模型下不该动工具")

# =====================================================================
print("\n===== ④ 开关关掉 → 本地也保留绘图 =====")
inst = make({"disable_image_on_local": False}, "lmstudio/qwen3.8-4b")
req = types.SimpleNamespace(func_tool=FakeToolSet(ALL))
inst._hide_image_tool_for_local(req, ev)
print(f"  剩余 {len(req.func_tool.names())} 个（应 {len(ALL)}）")
check("④", len(req.func_tool.names()) == len(ALL), "关掉开关后仍被移除")

# =====================================================================
print("\n===== ⑤ 容错：没有 func_tool / 幂等 =====")
inst = make({}, "lmstudio/x")
for bad in (types.SimpleNamespace(func_tool=None), types.SimpleNamespace()):
    try:
        inst._hide_image_tool_for_local(bad, ev)
        print("  没有 func_tool -> 正常返回 ✓")
    except Exception as exc:  # noqa: BLE001
        check("⑤", False, f"容错失败：{exc}")

req = types.SimpleNamespace(func_tool=FakeToolSet(ALL))
inst._hide_image_tool_for_local(req, ev)
inst._hide_image_tool_for_local(req, ev)  # 连调两次
print(f"  连调两次后剩 {len(req.func_tool.names())} 个（应 {len(ALL) - 1}）")
check("⑤", len(req.func_tool.names()) == len(ALL) - 1, "重复调用出错")

# =====================================================================
print("\n===== ⑥ 接线 & 配置 =====")
src = (PLUGIN / "main.py").read_text(encoding="utf-8")
check("⑥", "self._hide_image_tool_for_local(req, event)" in src, "_gate_local_model 里没调用")
conf_schema = (PLUGIN / "_conf_schema.json").read_text(encoding="utf-8")
for k in ("disable_image_on_local", "local_model_keywords"):
    ok = k in conf_schema
    print(f"  schema 里有 {k}: {ok}")
    check("⑥", ok, f"schema 里缺 {k}")

# =====================================================================
print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 本地判定 / 本地移除绘图 / 云端不动 / 开关可关 / 容错幂等 / 接线")

