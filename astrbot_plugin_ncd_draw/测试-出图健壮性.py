# -*- coding: utf-8 -*-
"""回归护栏：另一份代码审查里挑出来的 5 个真问题。

来源：外部 AI 对项目的检查（2026-10-18），逐条核实后确认成立的：
  ① 空闲看门狗不看 busy → 出图超过 idle_timeout 会在渲染中途自杀
  ② 默认负面词以 `color, colored` 开头 → 所有出图系统性偏灰
  ⑤ txt2img 不校验 8 的倍数 / 没有上限 → 填 500 报错、填 2048 OOM
  ⑦ LoRA 静默降级，页面上看不到到底挂没挂上
  ⑨ 出图文件名固定 comfy_ 前缀 → 独立后端的图也叫 comfy_xxx，排查时误导

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-出图健壮性.py
"""

import pathlib
import sys
import types

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402
import sd_worker  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


WORKER_SRC = (PLUGIN / "sd_worker.py").read_text(encoding="utf-8")
MAIN_SRC = (PLUGIN / "main.py").read_text(encoding="utf-8")
PAGE = (PLUGIN / "pages" / "imggen" / "index.html").read_text(encoding="utf-8")

# =====================================================================
#  ① 看门狗必须避开 busy
# =====================================================================
print("===== ① 看门狗不杀正在出图的自己 =====")
# ★ 不能用 find('def _idle_watchdog')：Select-String/find 都可能先命中
#   threading.Thread(target=_idle_watchdog...) 那行引用。用正则锁死函数定义。
import re as _re  # noqa: E402

m = _re.search(r"def _idle_watchdog\(\)[^\n]*\n(.*?)(?=\n\ndef |\Z)", WORKER_SRC, _re.S)
watch = m.group(0) if m else ""
print("  看门狗片段：")
for line in watch.splitlines()[:14]:
    print("   " + line)
check("①", "def _idle_watchdog" in watch, "正则没抓到看门狗函数")
check("①", 'if _state["busy"]' in watch,
      "★看门狗没判 busy —— 出图超过 idle_timeout 会把自己杀掉")
# ★ 只认"行首缩进的真实调用"：注释里提到 `os._exit(0)` 是在解释这个坑，
#   拿裸字符串 index() 会被注释误判（这条断言自己就踩过）。
_call = _re.search(r"^\s+os\._exit\(0\)", watch, _re.M)
check("①", _call is not None, "没找到 os._exit(0) 调用")
if _call is not None:
    check("①", watch.index('if _state["busy"]') < _call.start(),
          "busy 判断必须在 os._exit 调用之前")

# =====================================================================
#  ② 默认负面词不能再压颜色
# =====================================================================
print("\n===== ② 默认负面词 =====")
neg = sd_worker.DEFAULT_NEGATIVE
print(f"  {neg[:90]}…")
check("②", "color" not in neg, f"★默认负面词里还有 color（会压掉颜色）：{neg}")
check("②", "lowres" in neg and "watermark" in neg, "常规负面词被删光了")
check("②", "sd_negative" in MAIN_SRC, "插件没有读 sd_negative 配置")
# ★ 2026-10-19：负面词现在优先用 AI 传的 opts['negative']，没有再回落到配置
check("②", "_negative_with_extra(" in MAIN_SRC and "sd_negative" in MAIN_SRC,
      "★没把负面词传给 worker（配了也不生效）")

# =====================================================================
#  ⑤txt2img 宽高对齐与夹取
# =====================================================================
print("\n===== ⑤ 宽高对齐 8 的倍数 =====")
cases = {500: 496, 720: 720, 960: 960, 2048: 1920, 60: 64, 0: 512, -5: 512, "x": 512}
for raw, want in cases.items():
    got = sd_worker._align_side(raw)
    print(f"  {raw!r:8} → {got}")
    check("⑤", got == want, f"_align_side({raw!r}) 应为 {want}，实际 {got}")
    check("⑤", got % 8 == 0, f"{got} 不是 8 的倍数")
check("⑤", "_align_side(payload.get(\"width\")" in WORKER_SRC,
      "txt2img 没走对齐函数（还是会直接抛 divisible by 8）")

# =====================================================================
#  ⑦/⑨ 插件侧：LoRA 可见、文件名前缀按后端
# =====================================================================
print("\n===== ⑦ LoRA 状态可见 / ⑨ 文件名前缀 =====")
check("⑦", 'info["lora"]' in MAIN_SRC, "status 没把 lora 透出去")
check("⑦", "s.lora" in PAGE, "生图页没显示 LoRA")
check("⑨", '"sd",' in MAIN_SRC, "文件名前缀没固定成 sd")
check("⑨", MAIN_SRC.count('"sd",') >= 2, "两处出图调用都要带上前缀参数")
# ★ 2026-10-18 实测踩过：用 PowerShell here-string 生成代码时，引号被吃掉了，
#   代码变成裸标识符 —— 语法合法，运行时 NameError，出图正好挂在这一行。
#   编译检查抓不到，只有这种断言能抓。
check("⑨", "_image_backend" not in MAIN_SRC,
      "★ComfyUI 后端已拆掉，不该再有 _image_backend")
check("⑨", "ComfyUIError" not in MAIN_SRC, "★还引用着已删除的 ComfyUIError")
check("⑨", 'f"{tag}_{uuid.uuid4().hex[:8]}_{safe}"' in MAIN_SRC,
      "文件名没用 tag 参数")
check("⑨", "comfy_{uuid" not in MAIN_SRC,
      "★还有写死的 comfy_ 前缀（独立后端的图会被叫成 comfy_xxx）")

# =====================================================================
#  ④ 超时后把 worker 判为脏
# =====================================================================
print("\n===== ④ 超时后丢弃 worker =====")
check("④", "_discard_worker" in MAIN_SRC, "没有 _discard_worker")
check("④", "self._discard_worker()" in MAIN_SRC, "超时分支没调用 _discard_worker")
check("④", hasattr(plugin_main.NcdDrawPlugin, "_discard_worker"),
      "方法没挂到类上")


def discard_test() -> bool:
    inst = plugin_main.NcdDrawPlugin.__new__(plugin_main.NcdDrawPlugin)
    stopped = {"n": 0}

    class FakeClient:
        def stop(self):
            stopped["n"] += 1

    inst._sd = FakeClient()
    inst._discard_worker()
    return stopped["n"] == 1 and inst._sd is None


check("④", discard_test(), "调用后应 stop() 一次并把 _sd 置 None")
print("  stop() 一次 + _sd 置 None ✓")

print("\n===== ⑥ _save_generated 不能碰 self（曾导致出图全挂）=====")
import inspect as _inspect  # noqa: E402

raw_attr = _inspect.getattr_static(plugin_main.NcdDrawPlugin, "_save_generated")
check("⑥", isinstance(raw_attr, staticmethod), "_save_generated 应该是 staticmethod")
save_src = _inspect.getsource(plugin_main.NcdDrawPlugin._save_generated)
print("  签名:", _inspect.signature(plugin_main.NcdDrawPlugin._save_generated))
check("⑥", "self." not in save_src,
      "★staticmethod 里出现 self. —— 会在运行时 NameError，整条出图挂掉")
check("⑥", "tag" in _inspect.signature(plugin_main.NcdDrawPlugin._save_generated).parameters,
      "前缀参数 tag 不在签名里")

print("\n===== ⑦ 聊天进度汇报 =====")
import asyncio as _asyncio  # noqa: E402


def progress_test(busy: bool, max_msgs: int) -> list[str]:
    sent: list[str] = []

    class FakeSd:
        async def progress(self):
            return {"busy": busy, "step": 3, "total": 20}

    inst = plugin_main.NcdDrawPlugin.__new__(plugin_main.NcdDrawPlugin)
    inst._conf = {"sd_progress_interval": 3}
    inst._sd = FakeSd()

    class FakeCtx:
        async def send_message(self, umo, chain):
            sent.append(umo)

    inst.context = FakeCtx()
    ev = types.SimpleNamespace(unified_msg_origin="x:GroupMessage:1")
    try:
        _asyncio.run(_asyncio.wait_for(inst._chat_progress(ev, max_msgs), timeout=15))
    except _asyncio.TimeoutError:
        pass
    return sent


_sent_busy = progress_test(True, 1)
print(f"  busy=True, max_msgs=1 → 发了 {len(_sent_busy)} 条")
check("⑦", len(_sent_busy) == 1, f"busy 时应发 1 条，实际 {len(_sent_busy)}")

_sent_idle = progress_test(False, 2)
print(f"  busy=False, max_msgs=2 → 发了 {len(_sent_idle)} 条（应为 0）")
check("⑦", len(_sent_idle) == 0, "没在出图时不该发进度")

check("⑦", "sd_progress_in_chat" in (PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"),
      "schema 里没有 sd_progress_in_chat")
check("⑦", "_chat_progress(event)" in MAIN_SRC, "generate_image 没起进度任务")
check("⑦", "progress_task.cancel()" in MAIN_SRC, "出图结束后没取消进度任务（会一直发）")

print("\n===== ⑧ 画图规范要明确禁止无效质量词 =====")
# ★ 2026-10-18 改：画图规范从 generate_image 的说明搬到了 draw_tools（二级目录），
#   否则一大段规则每轮都进提示词。这里改查 draw_tools 的源码。
import inspect as _insp2  # noqa: E402

doc = _insp2.getdoc(plugin_main.NcdDrawPlugin.generate_image) or ""
draw_src = MAIN_SRC[MAIN_SRC.find("async def drawing_tools("):MAIN_SRC.find("async def generate_image(")]
print(f"  generate_image 说明 {len(doc)} 字（应显著变短），drawing_tools 段 {len(draw_src)} 字")
check("⑧", "drawing_tools" in doc, "generate_image 的说明没指向 drawing_tools")
check("⑧", len(doc) < 2200, f"generate_image 说明又变长了（{len(doc)} 字）—— 规范该放 drawing_tools")
for token in ("四条硬规则", "masterpiece", "best quality", "8k", "ultra detailed", "77 token"):
    ok = token in draw_src
    print(f"  {'OK ' if ok else '✗  '}{token}")
    check("⑧", ok, f"drawing_tools 的规范里缺「{token}」")
check("⑧", "光线" in draw_src or "lighting" in draw_src,
      "只说「别写」不够，还要指出替代写法（光线/镜头/质感）")

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 看门狗避 busy / 负面词不压色 / 宽高对齐 / LoRA 可见 / 文件名前缀 / 超时丢弃 worker")





