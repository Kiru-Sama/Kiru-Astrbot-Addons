# -*- coding: utf-8 -*-
"""校验新打出来的 zip：10 个插件、无垃圾、页面完整且安全。"""

import json
import re
import zipfile
from pathlib import Path

ZIP = Path(r"F:\Deepseek Harness WorkTable\AstrBot插件分享包.zip")
PLUGINS = [
    "astrbot_plugin_ncd_monitor",
    "astrbot_plugin_ncd_toolbox",
    "astrbot_plugin_ncd_patch",
    "astrbot_plugin_ncd_extra",
    "astrbot_plugin_ncd_comfy",
    "astrbot_plugin_ncd_session",
    "astrbot_plugin_ncd_catchup",
    "astrbot_plugin_ncd_inject",
    "astrbot_plugin_ncd_awake",
    "astrbot_plugin_ncd_memory",
    "astrbot_plugin_ncd_welcome",
    "astrbot_plugin_ncd_feedback",
]

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)

LIVE = Path(r"F:\NapCatDesk\ASTRBOT\data\plugins")


def _version_of(text: str) -> str:
    m = re.search(r"(?m)^version:\s*(\S+)", text or "")
    return m.group(1) if m else ""


def check_version(plugin: str, label: str) -> None:
    """zip 内版本必须存在，且与 live 源码一致（抓「改了没装」）。"""
    try:
        in_zip = _version_of(z.read(f"插件/{plugin}/metadata.yaml").decode("utf-8"))
    except KeyError:
        check(f"{label} 版本", False, "zip 里没有 metadata.yaml")
        return
    mf = LIVE / plugin / "metadata.yaml"
    live = _version_of(mf.read_text(encoding="utf-8-sig")) if mf.is_file() else ""
    check(f"{label} 版本 {in_zip}", bool(in_zip) and in_zip == live,
          f"zip={in_zip} live={live}")




z = zipfile.ZipFile(ZIP)
names = z.namelist()
print("entries:", len(names), "bytes:", ZIP.stat().st_size)

for p in PLUGINS:
    check(f"插件存在 {p}", f"插件/{p}/main.py" in names)

for needle in ["__pycache__", ".pyc", "thumbcache", "ncd_draw", "codex", "plugin_data", "测试-", ".bak"]:
    hit = [n for n in names if needle in n]
    check(f"不含 {needle}", not hit, str(hit[:3]))

awake = [n for n in names if n.startswith("插件/astrbot_plugin_ncd_awake/")]
print("awake files:", len(awake))
for rel in ["main.py", "metadata.yaml", "_conf_schema.json", "README.md", "pages/awake/index.html"]:
    check(f"awake 含 {rel}", f"插件/astrbot_plugin_ncd_awake/{rel}" in names)

page = z.read("插件/astrbot_plugin_ncd_awake/pages/awake/index.html").decode("utf-8")
inline = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
check("awake 页面内联脚本无 src=/href=", re.search(r"(?:src|href)=", inline) is None)
check("awake 页面有开关", "toggleBtn" in inline)
check("页面带 bridge", "/api/plugin/page/bridge-sdk.js" in page)

ver = z.read("版本.txt").decode("utf-8-sig")
for p in PLUGINS:
    check(f"版本.txt 列出 {p}", p in ver)
check("版本.txt 含 awake 版本", ("v" + _version_of(
    z.read("插件/astrbot_plugin_ncd_awake/metadata.yaml").decode("utf-8"))) in ver)

meta = z.read("插件/astrbot_plugin_ncd_awake/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_awake", "awake metadata")
schema = json.loads(z.read("插件/astrbot_plugin_ncd_awake/_conf_schema.json").decode("utf-8"))
check("awake schema 默认关闭", schema["enabled"]["default"] is False)

# ---- 链路记忆 ----
check("插件清单 12 个", len(PLUGINS) == 12)
mem_meta = z.read("插件/astrbot_plugin_ncd_memory/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_memory", "memory metadata")
mem_main = z.read("插件/astrbot_plugin_ncd_memory/main.py").decode("utf-8")
mem_schema = json.loads(
    z.read("插件/astrbot_plugin_ncd_memory/_conf_schema.json").decode("utf-8")
)
check(
    "memory 三工具齐全",
    all(
        f'name="{name}"' in mem_main
        for name in ("memory_write", "memory_search", "memory_forget")
    ),
)
check(
    "memory 默认上限/摘要/新记忆提示",
    mem_schema["max_entries"]["default"] == 100
    and mem_schema["summary_capture"]["default"] is True
    and mem_schema["hint_enabled"]["default"] is True,
)
check(
    "memory 新记忆待发标记",
    "hint_pending" in mem_main and "hint_sent" not in mem_main.split("hint_pending")[0],
)
check(
    "memory 中文检索二元组",
    "SEARCH_MIN_SCORE" in mem_main
    and "def _bigrams" in mem_main
    and "def _score_memory" in mem_main,
)
check(
    "memory 摘要扫描不包 compressor",
    "_capture_summaries" in mem_main
    and "SUMMARY_PREFIXES" in mem_main
    and "LLMSummaryCompressor" not in mem_main,
)
check(
    "memory 路径脱敏与群删权限",
    "_sha1(umo)" in mem_main
    and "GroupMessage" in mem_main
    and "is_admin" in mem_main,
)
mem_page = "插件/astrbot_plugin_ncd_memory/pages/memory/index.html"
check("memory 面板存在", mem_page in names)
if mem_page in names:
    mem_html = z.read(mem_page).decode("utf-8")
    mem_js = "\n".join(re.findall(r"<script>(.*?)</script>", mem_html, re.S))
    check("memory 面板带 bridge", "/api/plugin/page/bridge-sdk.js" in mem_html)
    check(
        "memory 面板可改可删",
        "/ncd-memory/update" in mem_js and "/ncd-memory/delete" in mem_js,
    )
    check(
        "memory 条目不再拼 query",
        'apiPost("/ncd-memory/entries"' in mem_js
        and "/ncd-memory/entries?umo=" not in mem_js,
    )
    check(
        "memory 面板显示对端头像",
        "data-av" in mem_html and "bindAvatars" in mem_js,
    )

# ---- 监控页：这次修掉的 data 缩略图 ----
mon = z.read("插件/astrbot_plugin_ncd_monitor/pages/monitor/index.html").decode("utf-8")
mon_js = "\n".join(re.findall(r"<script>(.*?)</script>", mon, re.S))
mon_main = z.read("插件/astrbot_plugin_ncd_monitor/main.py").decode("utf-8")
check("监控页内联脚本无 src=/href=", re.search(r"(?:src|href)=", mon_js) is None)
check("监控页用 data-thumb", 'data-thumb="' in mon_js)
check("监控页插入后赋 src", 'img[data-thumb]' in mon_js and "img.src = value" in mon_js)
mon_meta = z.read("插件/astrbot_plugin_ncd_monitor/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_monitor", "监控")
mon_schema = json.loads(z.read("插件/astrbot_plugin_ncd_monitor/_conf_schema.json").decode("utf-8"))
check("监控并发默认 0", mon_schema["max_concurrent_replies"]["default"] == 0)
check("监控页有缓存诊断卡", "缓存诊断" in mon and "first_diff" in mon_js)
check("监控页有首条列与断因", "首条" in mon_js and "diag-cause" in mon_js and "_cache_cause" in mon_main)
check(
    "监控有引用守卫",
    "_patch_quote_guard" in mon_main
    and "quote_guard_after" in mon_schema
    and "_strip_broken_quote" in mon_main,
)
check("监控页有会话缓存命中", "cache_hit_rate" in mon_js and "缓存" in mon_js)
check("监控页会话卡用 6 列网格", "ses-head" in mon and "grid-template-columns" in mon)
check("监控页有外观抽屉", "uiDrawer" in mon and "ncd-monitor-appearance" in mon)
check("监控页有 16 个可隐藏卡片", mon.count("data-card=") >= 16)
check("监控页有文生图/图生图标签", "图生图" in mon_js and "文生图" in mon_js and "st.mode" in mon_js)
check(
    "监控有每日 Token",
    "usage_daily" in z.read("插件/astrbot_plugin_ncd_monitor/main.py").decode("utf-8")
    and "usageDailyTotal" in mon_js
    and "drawUsageDailyChart" in mon_js
    and "setUsageMode" in mon_js
    and "function safe(" in mon_js
    and "_token_total_stats" in z.read("插件/astrbot_plugin_ncd_monitor/main.py").decode("utf-8")
    and "usageTotalCum" in mon_js
    and "token_daily" in z.read("插件/astrbot_plugin_ncd_monitor/main.py").decode("utf-8"),
)
check("监控页读实际 ComfyUI 队列", "_attach_comfy_queue" in z.read("插件/astrbot_plugin_ncd_monitor/main.py").decode("utf-8") and "队列（实际）" in mon_js)

# ---- 本轮：异步出图 / 历史 / 搜索提示 ----
comfy_meta = z.read("插件/astrbot_plugin_ncd_comfy/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_comfy", "comfy")
comfy_presets = z.read("插件/astrbot_plugin_ncd_comfy/presets.py").decode("utf-8")
comfy_main = z.read("插件/astrbot_plugin_ncd_comfy/main.py").decode("utf-8")
comfy_quota = z.read("插件/astrbot_plugin_ncd_comfy/quota.py").decode("utf-8")
comfy_page = z.read("插件/astrbot_plugin_ncd_comfy/pages/quota/index.html").decode("utf-8")
for name in ("quota.py", "quota_store.py", "pages/quota/index.html"):
    check(
        f"comfy 包含 {name}",
        f"插件/astrbot_plugin_ncd_comfy/{name}" in z.namelist(),
    )
check("comfy 有额度命令", '"排名"' in comfy_main and "quota_rank_cmd" in comfy_main)
check(
    "comfy 有全部排行命令（管理员）",
    '"全部排行"' in comfy_main and "quota_all_rank_cmd" in comfy_main,
)
check(
    "monitor 有设备状态命令（所有人）",
    '"设备状态"' in mon_main and "device_status_cmd" in mon_main,
)
check(
    "包含安装说明",
    "安装说明.txt" in z.namelist()
    and "data\\plugins" in z.read("安装说明.txt").decode("utf-8"),
)
check("comfy 有额度面板 API", "ncd-comfy/quota" in comfy_main)
check(
    "comfy 有四榜数据",
    "ranking_total" in comfy_quota
    and "speech_ranking" in comfy_quota
    and '"rankings"' in comfy_quota,
)
check(
    "comfy 有软门禁",
    "_lookup_gate" in comfy_main
    and "_lookup_seen" in comfy_main
    and "comfy_tag_lookup" in comfy_main,
)
check(
    "toolbox 保留 comfy_tag_lookup",
    "comfy_tag_lookup" in z.read(
        "插件/astrbot_plugin_ncd_toolbox/main.py"
    ).decode("utf-8"),
)
check(
    "comfy 面板四榜",
    all(x in comfy_page for x in ("生图总榜", "发言日榜", "发言总榜", "renderBoard")),
)
check(
    "comfy 有额度数值控制",
    "api_quota_config" in comfy_main
    and "QUOTA_CONFIG_SPEC" in comfy_main
    and "saveConfig" in comfy_page,
)
check(
    "comfy 有赞助者覆盖",
    "sponsor_overrides" in comfy_quota
    and "set_override" in comfy_quota
    and "sponsorFields" in comfy_page,
)
check(
    "comfy 有额度计数",
    "_quota_on_success" in comfy_main and "record_success" in comfy_main,
)
check("comfy 有 70 禁生图入口拦截", "_quota_block_message" in comfy_main)
check("comfy 有 20 步降质", "_quota_effective_steps" in comfy_main)
check("comfy 已启用限流执行", "QUOTA_ENFORCE = True" in comfy_main)
check(
    "comfy 有队列上限",
    "_quota_enqueue_error" in comfy_main and "_quota_pending_count" in comfy_main,
)
check(
    "comfy 有间隔出队",
    "_quota_job_wait" in comfy_main and "_quota_worker_wait" in comfy_main,
)
check("comfy 有画幅钳制", "_quota_clamp_size" in comfy_main)
check("comfy 有结束时间", "_quota_record_end" in comfy_main)
check("comfy 有提示词库模块", "promptlib.py" in z.namelist() or any(n.endswith("/promptlib.py") for n in z.namelist()))
check("comfy 有标签库工具", "comfy_tag_lookup" in comfy_main and "character_profile" in comfy_main)
check("comfy 有绘图 SOP", "绘图 SOP" in comfy_main and "prompt 硬规则" not in comfy_main)
check(
    "comfy 有同步占位",
    "_quota_sync_reserve" in comfy_main and "_quota_sync_waiting" in comfy_main,
)
check(
    "comfy 有发言统计",
    "quota_count_speech" in comfy_main and "record_speech" in comfy_main,
)
check(
    "comfy 异步失败回上下文",
    "_pending_notes[umo]" in comfy_main
    and "任务已经结束" in comfy_main
    and "不要再声称它还在排队" in comfy_main,
)
check(
    "comfy_status 强制查询语义",
    "必须先调用本工具" in comfy_main,
)
check(
    "comfy 命令有全角/反斜杠别名",
    "／comfyui" in comfy_main and "\\comfyui" in comfy_main,
)
check(
    "comfyui 用 yield 输出",
    "yield event.plain_result" in comfy_main,
)
check(
    "comfy 发布排队条目",
    "queue_entries" in comfy_main and "_build_queue_entries" in comfy_main,
)
check(
    "comfy 入队/开画/收尾刷新面板",
    comfy_main.count("self._publish_state(force=True)") >= 3,
)
check(
    "监控页有排队列表",
    "renderComfyQueue" in mon_js
    and "cq-card" in mon
    and "Array.isArray(d.queue_entries)" in mon_js,
)
check("监控页排队只数 pending", 'e.status === "pending"' in mon_js)
check(
    "comfy 画幅提醒在入队前拦截",
    "_canvas_reminder" in comfy_main
    and "后台任务提前返回文字结果" in comfy_main
    and "w_early" in comfy_main,
)
check(
    "comfy 有 ControlNet 预处理",
    "control_preprocess" in comfy_main and "CONTROL_PREPROCESSORS" in comfy_presets,
)
check(
    "comfy 有 Krea2 pose 接线",
    "krea2-pose-control-mix025" in comfy_presets,
)
comfy_schema = json.loads(z.read("插件/astrbot_plugin_ncd_comfy/_conf_schema.json").decode("utf-8"))
check("comfy 已移除死配置宽高", "comfy_width" not in comfy_schema and "comfy_height" not in comfy_schema)
for key in ("comfy_async_api", "comfy_audit_nudge", "comfy_history_days", "comfy_history_per_session"):
    check(f"comfy schema 有 {key}", key in comfy_schema)
for key in ("comfy_busy_detect", "comfy_busy_external_gb", "comfy_busy_free_gb", "comfy_busy_util", "comfy_busy_queue", "comfy_fair_batch"):
    check(f"comfy schema 有 {key}", key in comfy_schema)
for key in (
    "comfy_quota_global_enabled",
    "comfy_quota_global_limit",
    "comfy_quota_queue_limited",
    "comfy_quota_queue_normal",
    "comfy_quota_interval_base",
    "comfy_quota_canvas_max_side",
    "comfy_quota_degrade_steps",
    "comfy_quota_ban_hours",
    "comfy_prompt_lib_dir",
):
    check(f"comfy schema 有 {key}", key in comfy_schema)
comfy_main = z.read("插件/astrbot_plugin_ncd_comfy/main.py").decode("utf-8")
check(
    "comfy 有只读查询提示",
    "_lookup_notice" in comfy_main
    and "comfy_lookup_notify" in comfy_schema
    and "开始查标签库" in comfy_main
    and "开始查法典范本" in comfy_main,
)
check("comfy 有中断原因提示", "_interrupted_job_counts" in comfy_main and "正在重载/退出" in comfy_main)
check("comfy 有自动参考图", "reference=auto" in comfy_main and "_history_image_bytes" in comfy_main)
check("comfy 采样提示带模式", "开始采样了…（" in comfy_main and "图生图" in comfy_main)
check("comfy 提示发送有超时", "timeout=8.0" in comfy_main and "发提示超时" in comfy_main)
check("comfy 自动发图带参数", "_image_caption" in comfy_main and "含权重加载" in comfy_main)
check("comfy 不再做关键词参考判断", "_wants_auto_reference" not in comfy_main)
check("comfy 有队列去重", "_duplicate_image_job" in comfy_main)
check("comfy 有重载提示落盘", "_save_reload_notices" in comfy_main and "_take_reload_notice" in comfy_main)
check("comfy 法典私聊 auto 两套", "_codex_categories" in comfy_main and 'category: str = "auto"' in comfy_main)
check("comfy 有异步任务", "_ASYNC_RUN" in comfy_main and "_spawn_image_job" in comfy_main)
check("comfy 有历史工具", "comfy_gallery" in comfy_main and "history.json" in comfy_main)
check("comfy 有异步图历史", "_append_image_history" in comfy_main and "系统记录" in comfy_main)
check("comfy 有取消提示", "_bg_umo" in comfy_main and "图画被中断了" in comfy_main)
check("comfy 有审核拉起", "_queue_audit_nudge" in comfy_main and "commit_event" in comfy_main)
check("comfy 有繁忙检测", "_gpu_metrics" in comfy_main and "设备繁忙" in comfy_main)

tb_meta = z.read("插件/astrbot_plugin_ncd_toolbox/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_toolbox", "toolbox")
session_meta = z.read("插件/astrbot_plugin_ncd_session/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_session", "session")
patch_meta = z.read("插件/astrbot_plugin_ncd_patch/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_patch", "patch")
extra_meta = z.read("插件/astrbot_plugin_ncd_extra/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_extra", "extra")
extra_main = z.read("插件/astrbot_plugin_ncd_extra/main.py").decode("utf-8")
extra_schema = json.loads(
    z.read("插件/astrbot_plugin_ncd_extra/_conf_schema.json").decode("utf-8")
)
check(
    "公告指令与 footer",
    '"公告"' in extra_main
    and ("广播" in extra_main or "broadcast" in extra_main)
    and "此消息不会进入bot上下文" in extra_main,
)
check(
    "公告默认只发好友",
    extra_schema["announce_scope"]["default"] == "friend",
)
check(
    "公告有审计/去重/限速",
    "ncd_announce" in extra_main
    and "announce_dedup_seconds" in extra_main
    and "max(1500" in extra_main,
)
check(
    "公告跳过 QQ 设备号",
    "QQ_DEVICE_IDS" in extra_main
    and "is_device_target" in extra_main
    and "skip_learned" in extra_main,
)
catchup_meta = z.read("插件/astrbot_plugin_ncd_catchup/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_catchup", "catchup")
check("catchup 有手动补台", "catchup_now" in z.read("插件/astrbot_plugin_ncd_catchup/main.py").decode("utf-8"))
comfy_fair = "comfy_fair_batch" in comfy_schema
check("comfy 有公平队列配置", comfy_fair)
check("comfy 有公平队列实现", "_pick_next_job" in comfy_main and "_fair_batch" in comfy_main)
tb_schema = json.loads(z.read("插件/astrbot_plugin_ncd_toolbox/_conf_schema.json").decode("utf-8"))
check("toolbox schema 有 web_notify", "web_notify" in tb_schema)
tb_main = z.read("插件/astrbot_plugin_ncd_toolbox/main.py").decode("utf-8")
check(
    "toolbox 保留 memory 三件套",
    all(x in tb_main for x in ("memory_write", "memory_search", "memory_forget")),
)
check("toolbox 回退文案不再默认承诺", "失败自动回退本机浏览器" not in tb_main)
check("toolbox 回退错误说明开关", "本地浏览器回退已关闭" in tb_main)
check("toolbox 有搜索提示", "开始搜索了" in tb_main and "开始读取页面" in tb_main)

# ---- 新插件：群聊迎新 ----
wel_meta = z.read("插件/astrbot_plugin_ncd_welcome/metadata.yaml").decode("utf-8")
check_version("astrbot_plugin_ncd_welcome", "迎新")
check(
    "迎新包含 main.py 与配置 schema",
    "插件/astrbot_plugin_ncd_welcome/main.py" in z.namelist()
    and "插件/astrbot_plugin_ncd_welcome/_conf_schema.json" in z.namelist(),
)
wel_main = z.read("插件/astrbot_plugin_ncd_welcome/main.py").decode("utf-8")
check(
    "迎新只认入群通知且不经 LLM",
    "notice_type" in wel_main
    and "group_increase" in wel_main
    and "should_call_llm(False)" in wel_main
    and "send_group_msg" in wel_main,
)
z.close()

print()
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
print("ZIP CHECKS PASSED")
