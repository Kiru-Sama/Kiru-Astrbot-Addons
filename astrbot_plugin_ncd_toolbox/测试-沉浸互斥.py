# -*- coding: utf-8 -*-
"""回归护栏：沉浸模式 / 半沉浸模式必须互斥。

背景：AstrBot 的 _conf_schema 没有下拉框/单选控件（只有
int/float/bool/string/text/list/file/object/template_list/dict），
插件也收不到"用户点了哪个开关"的事件 —— 所以面板上两个开关能同时打开，
看起来自相矛盾（虽然运行时沉浸优先）。

修法：加载时发现两个都开 → 沉浸优先、关掉半沉浸、写回配置。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-沉浸互斥.py
"""

import pathlib
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"{label}: {msg}")


class FakeConf(dict):
    """模拟 AstrBotConfig：dict + save_config。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.saved: list[dict] = []

    def save_config(self, replace=None, **kw):
        self.saved.append(dict(replace or {}))


def make(conf):
    inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    inst._conf = conf
    return inst


# =====================================================================
print("===== ① 两个都开 → 关掉半沉浸并落盘 =====")
c = FakeConf({"rp_mode": True, "rp_mode_half": True})
inst = make(c)
inst._normalize_rp_mode()
print(f"  rp_mode={c['rp_mode']}  rp_mode_half={c['rp_mode_half']}  saved={c.saved}")
check("①", c["rp_mode_half"] is False, "半沉浸没被关掉")
check("①", c["rp_mode"] is True, "不该动沉浸模式")
check("①", c.saved == [{"rp_mode_half": False}], f"没写回配置：{c.saved}")
check("①", inst._immersion_level() == "full", "纠正后档位应为 full")

# =====================================================================
print("\n===== ② 只开半沉浸 → 不动 =====")
c2 = FakeConf({"rp_mode": False, "rp_mode_half": True})
make(c2)._normalize_rp_mode()
print(f"  rp_mode_half={c2['rp_mode_half']}  saved={c2.saved}")
check("②", c2["rp_mode_half"] is True, "★只开半沉浸时不该被关掉")
check("②", c2.saved == [], "不该写配置")
check("②", make(c2)._immersion_level() == "half", "只开半沉浸时档位应为 half")

# =====================================================================
print("\n===== ③ 只开沉浸 / 都不开 → 不动 =====")
c3 = FakeConf({"rp_mode": True, "rp_mode_half": False})
make(c3)._normalize_rp_mode()
check("③", c3.saved == [], "只开沉浸时不该写配置")
check("③", make(c3)._immersion_level() == "full", "档位应为 full")

c4 = FakeConf({"rp_mode": False, "rp_mode_half": False})
make(c4)._normalize_rp_mode()
check("④", c4.saved == [], "都不开时不该写配置")
check("④", make(c4)._immersion_level() == "off", "档位应为 off")

# =====================================================================
print("\n===== ④ 容错：没有 save_config / 没有 _conf 都不能崩 =====")
c5 = {"rp_mode": True, "rp_mode_half": True}  # 普通 dict，没有 save_config
try:
    make(c5)._normalize_rp_mode()
    print(f"  普通 dict → rp_mode_half={c5['rp_mode_half']}（内存已纠正，不需要落盘）")
    check("④", c5["rp_mode_half"] is False, "内存里也该被纠正")
except Exception as exc:  # noqa: BLE001
    check("④", False, f"普通 dict 会崩：{exc}")

try:
    plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)._normalize_rp_mode()
    print("  没有 _conf → 正常返回 ✓")
except Exception as exc:  # noqa: BLE001
    check("④", False, f"没有 _conf 会崩：{exc}")

# =====================================================================
print("\n===== ⑤ 加载时真的会调用它 =====")
src = (PLUGIN / "main.py").read_text(encoding="utf-8")
check("⑤", "self._normalize_rp_mode()" in src, "__init__ 里没调用")
# 调用点必须在 __init__ 内（第一个 def __init__ 之后、下一个 def 之前）
i = src.find("def __init__")
j = src.find("self._normalize_rp_mode()")
nxt = src.find("\n    def ", i + 10)
check("⑤", i < j < (nxt if nxt != -1 else len(src)), "_normalize_rp_mode 的调用不在 __init__ 里")

# =====================================================================
print("\n===== ⑥ schema 里写明了互斥 =====")
import json  # noqa: E402

schema = json.loads((PLUGIN / "_conf_schema.json").read_bytes().decode("utf-8-sig"))
for key in ("rp_mode", "rp_mode_half"):
    hint = schema[key].get("hint") or ""
    ok = "互斥" in hint
    print(f"  {key}: 提到互斥 = {ok}")
    check("⑥", ok, f"{key} 的 hint 没写互斥")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 两个都开→自动关半沉浸并落盘 / 只开半沉浸不动 / 容错 / 加载时调用 / 文档写明")
