"""自测：全部工具是否注册成功、参数 schema 与描述是否齐全。

不联网、不启动 AstrBot，只导入模块并检查注册表。
用法：<AstrBot>\\.venv\\Scripts\\python.exe 测试-工具注册.py
"""

import json
import pathlib
import re
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

EXPECTED = {
    "web_search_bing": {"query": "string", "count": "number"},
    "fetch_page": {"url": "string"},
    "list_stickers": {"page": "number"},
    "preview_sticker": {"index": "number"},
    "send_sticker": {"index": "number"},
    "generate_image": {"prompt": "string", "style": "string", "reference": "string",
                       "size": "string", "force": "boolean"},
    # 换底模：只有一个 model，留空=只列清单（AI 想先看有什么可选时用）
    "switch_model": {"model": "string"},
    # 画图二级菜单：刻意不带参数（它只是"展开目录"）
    "draw_tools": {},
    # 头像：只有一个 who，留空=当前说话的人
    "view_avatar": {"who": "string"},
    "recall_history": {"count": "number"},
    # 菜单入口：故意【不带参数】—— 小模型多一个要填的字段就多一次填错的机会
    "ncd_tools": {},
}

failures: list[str] = []

import main as plugin_main  # noqa: E402
from astrbot.core.provider.register import llm_tools  # noqa: E402

print(f"导入成功，插件类 = {plugin_main.NcdToolboxPlugin.__name__}")

found = {t.name: t for t in llm_tools.func_list if t.name in EXPECTED}
print(f"注册表共 {len(llm_tools.func_list)} 个工具，命中 {len(found)} 个\n")

for name, params in EXPECTED.items():
    tool = found.get(name)
    if tool is None:
        failures.append(f"工具 {name} 没有注册上")
        print(f"✗ {name} 未注册")
        continue

    props = (tool.parameters or {}).get("properties") or {}
    got = {k: v.get("type") for k, v in props.items()}
    print(f"● {name}  参数={json.dumps(got, ensure_ascii=False)}")
    print(f"    {(tool.description or '')[:60]}…")

    if got != params:
        failures.append(f"{name} 参数不匹配：期望 {params}，实际 {got}")
    if not tool.description:
        failures.append(f"{name} 没有描述")
    for key, spec in props.items():
        if not spec.get("description"):
            failures.append(f"{name}.{key} 缺少参数描述")
    if getattr(tool, "handler", None) is None:
        failures.append(f"{name} 没有 handler")

schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
print(f"\n配置项 {len(schema)} 个: {', '.join(schema)}")
for key, meta in schema.items():
    if meta.get("type") not in ("string", "int", "float", "bool", "list", "object"):
        failures.append(f"配置 {key} 的类型 {meta.get('type')!r} 可能不被支持")
    if not meta.get("description"):
        failures.append(f"配置 {key} 缺少 description")

if not (PLUGIN / "metadata.yaml").is_file():
    failures.append("缺 metadata.yaml")

# ---------------------------------------------------------------------------
#  ★ 关键护栏：源码里 @filter.llm_tool 声明的名字，必须和 TOOL_NAMES 完全一致
#    沉浸/半沉浸模式会把不在 TOOL_NAMES 里的工具从 req.func_tool 移除，
#    漏加一个的表现是「机器人死活不调这个工具」，而且日志里看不出来。
# ---------------------------------------------------------------------------
src = (PLUGIN / "main.py").read_text(encoding="utf-8")
decorated = set(re.findall(r'@filter\.llm_tool\(name="([^"]+)"\)', src))
known = set(plugin_main.TOOL_NAMES)
print(f"\n源码里的 @filter.llm_tool: {sorted(decorated)}")
print(f"TOOL_NAMES            : {sorted(known)}")
missing = decorated - known
extra = known - decorated
if missing:
    failures.append(
        "★ 这些工具在 TOOL_NAMES 里查不到，沉浸模式会把它们【静默移除】: "
        + ", ".join(sorted(missing))
    )
if extra:
    failures.append("TOOL_NAMES 里有源码中不存在的工具: " + ", ".join(sorted(extra)))
if decorated != set(EXPECTED):
    failures.append(
        f"EXPECTED 和源码里的工具不一致：源码 {sorted(decorated)}，EXPECTED {sorted(EXPECTED)}"
    )

# ---------------------------------------------------------------------------
#  中文 prompt 拦截 + 出图总超时
#    两件都只有跑起来才知道：
#      · 中文 prompt 不拦 → 白等十几秒出一张废图（SD1.5 只认英文）
#      · 超时不生效 → 画不出来就一直占着并发槽，机器人整个卡住
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402

print("\n--- 中文 prompt 判定 ---")
cases = {
    "a cat sitting on a wooden chair": False,
    "cyberpunk street at night": False,
    "一只猫在木椅上": True,
    "猫": True,
    "a cat，soft light": True,      # 全角逗号也算中文输入痕迹
    "a cat, soft light": False,
    "": False,
}
for text, expect in cases.items():
    got = plugin_main.looks_chinese(text)
    print(f"  {text!r:38} → {got}（应 {expect}）")
    if got != expect:
        failures.append(f"looks_chinese({text!r}) 应为 {expect}，实际 {got}")

print("\n--- 出图总超时 ---")


async def _slow(prompt, opts=None):
    await asyncio.sleep(5)
    return b"x", "x.png"


inst = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
inst._conf = {"image_backend": "standalone", "image_timeout": 1}
inst._render_standalone = _slow          # 实例属性直接顶掉方法

t0 = __import__("time").time()
try:
    asyncio.run(inst._render_image("a cat"))
    failures.append("★ 超时没生效：1 秒的配置下竟然正常返回了")
    print("  !! 超时没生效")
except plugin_main.SDBackendError as exc:
    cost = __import__("time").time() - t0
    print(f"  {cost:.1f} 秒后中止：{str(exc)[:70]}…")
    if "超时" not in str(exc):
        failures.append(f"超时错误信息里没有「超时」字样：{exc}")
    if cost > 3:
        failures.append(f"超时用了 {cost:.1f} 秒才生效，应该在 1 秒左右")
except Exception as exc:  # noqa: BLE001
    failures.append(f"超时抛的是 {type(exc).__name__}，应为 SDBackendError")

# 默认值也要对：不配 image_timeout 时应落到 70
inst2 = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
inst2._conf = {"image_backend": "standalone"}
print(f"  不配 image_timeout 时的默认值：{inst2._conf_int('image_timeout', 70)}")
if inst2._conf_int("image_timeout", 70) != 70:
    failures.append("image_timeout 默认值不是 70")

if failures:
    print("\n失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)

print(f"\nPASS: {len(EXPECTED)} 个工具全部注册成功，参数与描述齐全，TOOL_NAMES 与源码声明一致，"
      "中文 prompt 能识别，出图超时能中止")

