# -*- coding: utf-8 -*-
"""全量自检：把手机 AstrBot + 面板代理 + NapCat + ComfyUI 整条链路打一遍。

用法：python 全量测试.py
输出：每项 PASS/FAIL + 末尾汇总
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request

PANEL = "http://127.0.0.1:9001"
NAPCAT_PROXY = "http://127.0.0.1:9002"
PHONE = "http://192.168.137.71:6185"
NAPCAT_DIRECT = "http://192.168.137.71:6099"
LAUNCHER = "http://192.168.137.1:8200"
COMFY = "http://192.168.137.1:8199"
USER, PWD = "astrbot", "NcdPass2026"
NAPCAT_TOKEN = "c6ae1d43"

results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    t0 = time.time()
    try:
        detail = fn() or ""
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    ms = int((time.time() - t0) * 1000)
    results.append((name, ok, f"{detail} ({ms}ms)" if ok else detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and ok else ""))
    if not ok:
        print(f"        └─ {detail}")


def http(url: str, method: str = "GET", body: dict | None = None,
         headers: dict | None = None, timeout: float = 20.0) -> tuple[int, bytes, dict]:
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def login() -> str:
    st, body, _ = http(f"{PHONE}/api/auth/login", "POST", {"username": USER, "password": PWD})
    assert st == 200, f"登录 HTTP {st}"
    return json.loads(body)["data"]["token"]


def main() -> int:
    print("=" * 70)
    print("一、面板聚合服务（9001）")
    print("=" * 70)
    page = {"html": "", "iframes": []}

    def t_page():
        st, body, _ = http(f"{PANEL}/", timeout=30)
        assert st == 200, f"HTTP {st}"
        page["html"] = body.decode("utf-8", "replace")
        return f"{len(body)} 字节"

    check("聚合页可访问", t_page)

    def t_tabs():
        labels = re.findall(r'class="tab[^"]*" data-i="\d+">([^<]+)</button>', page["html"])
        assert len(labels) >= 6, f"只有 {len(labels)} 个标签: {labels}"
        return " / ".join(labels)

    check("六个标签齐全", t_tabs)

    def t_bridge():
        assert "astrbot-plugin-page" in page["html"], "桥接脚本没注入"
        assert "data-plugin=" in page["html"], "缺 data-plugin"
        return "桥接 + data-plugin 都在"

    check("父窗口桥接已注入", t_bridge)

    def t_iframes():
        page["iframes"] = re.findall(r'data-src="(/p[^"]+)"', page["html"])
        # ★ 2026-11-01：防睡眠改成电脑本地页 /awake（不再走手机插件 iframe），
        #   所以 /p/ iframe 由 6 个变 5 个；多出来的是本地页。
        assert len(page["iframes"]) == 5, f"期望 5 个 /p/ iframe，实际 {len(page['iframes'])}"
        assert 'data-src="/awake?p=' in page["html"], "防睡眠没有指向本地 /awake 页"
        napcat = 'data-src="http://127.0.0.1:9002/"' in page["html"]
        assert napcat, "NapCat iframe 不是指向 9002 根路径"
        # 懒加载：初始不该有任何 iframe 已带 src（那会一次拉 5 个面板）
        eager = re.findall(r'<iframe[^>]*\ssrc="', page["html"])
        assert not eager, f"有 {len(eager)} 个 iframe 是立即加载的"
        return "5 个插件页 + 本地防睡眠页 + NapCat(9002)，全部懒加载"

    check("iframe 结构正确", t_iframes)

    def t_lazy():
        assert "f.src = f.dataset.src" in page["html"], "没有懒加载赋值逻辑"
        assert "astrbot-panel-tab" in page["html"], "没有记住上次标签"
        return "切到才加载 · 记住上次标签"

    check("懒加载 + 记忆标签", t_lazy)

    names = ["monitor", "quota", "inject", "memory", "feedback"]
    page_bodies: dict[str, str] = {}

    def t_plugin_pages():
        bad = []
        for i, path in enumerate(page["iframes"]):
            st, body, _ = http(PANEL + path, timeout=30)
            if st != 200:
                bad.append(f"{names[i]}=HTTP{st}")
            else:
                page_bodies[names[i]] = body.decode("utf-8", "replace")
        assert not bad, "失败: " + ", ".join(bad)
        return "5/5 面板 HTML 200（" + ", ".join(f"{k}:{len(v)//1024}KB" for k, v in page_bodies.items()) + "）"

    check("五个插件页可达", t_plugin_pages)


    print()
    print("=" * 70)
    print("二、插件数据接口（面板桥接的目标）")
    print("=" * 70)
    token = login()

    def t_apis():
        total, fails, skipped = 0, [], []
        for key, html in page_bodies.items():
            eps = sorted(set(re.findall(r'apiGet\(\s*["\'`](/[^"\'`]+)["\'`]', html)))
            for ep in eps:
                if any(x in ep for x in ("${", "+", "' +", '" +')):
                    skipped.append(f"{key}{ep}(模板)")
                    continue
                # 以 / 结尾的是带路径参数的端点（/thumb/<key>），空参调用必然报错，跳过
                if ep.endswith("/"):
                    skipped.append(f"{key}{ep}(需参数)")
                    continue
                total += 1
                st, body, _ = http(f"{PANEL}/api/plug/{plug_of(key)}{ep}",
                                   headers={"Authorization": f"Bearer {token}"})
                if st != 200 or b'"status":"error"' in body or b'"code":-1' in body:
                    fails.append(f"{key}{ep}=HTTP{st}")
        assert not fails, f"{len(fails)}/{total} 失败: " + ", ".join(fails[:6])
        note = f"，跳过 {len(skipped)} 个需参数端点" if skipped else ""
        return f"{total} 个接口全部 200{note}"

    def plug_of(kind: str) -> str:
        return {
            "monitor": "astrbot_plugin_ncd_monitor",
            "quota": "astrbot_plugin_ncd_comfy",
            "inject": "astrbot_plugin_ncd_inject",
            "memory": "astrbot_plugin_ncd_memory",
            "awake": "astrbot_plugin_ncd_awake",
            "feedback": "astrbot_plugin_ncd_feedback",
        }[kind]

    check("各面板 apiGet 端点", t_apis)

    def t_feedback():
        """反馈通道：stats + list 两个接口都要能读到数据。"""
        auth = {"Authorization": f"Bearer {token}"}
        st, body, _ = http(
            f"{PANEL}/api/plug/astrbot_plugin_ncd_feedback/ncd-feedback/stats", headers=auth)
        assert st == 200, f"stats HTTP {st}: {body[:120]}"
        stats = json.loads(body)
        assert stats.get("ok"), f"stats 返回 {body[:120]}"
        st2, body2, _ = http(
            f"{PANEL}/api/plug/astrbot_plugin_ncd_feedback/ncd-feedback/list",
            "POST", {"limit": 5}, auth)
        assert st2 == 200, f"list HTTP {st2}: {body2[:120]}"
        d = json.loads(body2)
        assert d.get("ok"), f"list 返回 {body2[:120]}"
        return f"stats+list 可用 · 共 {stats.get('total')} 条"
    check("反馈通道接口", t_feedback)

    def t_monitor_data():
        st, body, _ = http(f"{PANEL}/api/plug/astrbot_plugin_ncd_monitor/ncd-monitor/overview",
                           headers={"Authorization": f"Bearer {token}"})
        assert st == 200, f"HTTP {st}"
        d = json.loads(body)
        assert "server_time" in d, "返回里没有 server_time"
        return f"server_time={d['server_time']:.0f}"

    check("监控面板真实数据", t_monitor_data)
    check("反馈通道接口", t_feedback)

    def t_foreign_keep():
        """外来工具白名单：FOREIGN_KEEP 少了 feedback_submit，
        沉浸模式下这个工具会被摘掉（bot 会说“没有这个工具”）。

        ★ 2026-11-02（T-2）：这段原来写在 t_feedback 的 return 之后，属于死代码，
          永远不会执行；现在抽成独立检查。
        """
        tb = io.open(
            r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_toolbox\main.py",
            encoding="utf-8").read()
        keep_start = tb.find("FOREIGN_KEEP = (")
        assert keep_start >= 0, "toolbox 里找不到 FOREIGN_KEEP"
        keep_end = tb.find(")", keep_start)
        block = tb[keep_start:keep_end]
        assert "feedback_submit" in block, \
            "toolbox 的 FOREIGN_KEEP 没有 feedback_submit，bot 看不到这个工具"
        return "FOREIGN_KEEP 含 feedback_submit（沉浸模式不会摘掉）"

    check("FOREIGN_KEEP 覆盖跨插件工具", t_foreign_keep)
    def t_quota_get():
        st, body, _ = http(f"{PANEL}/api/plug/astrbot_plugin_ncd_comfy/ncd-comfy/quota",
                           headers={"Authorization": f"Bearer {token}"})
        assert st == 200, f"HTTP {st}"
        d = json.loads(body)
        g = d.get("global") or {}
        return f"count={g.get('count')}/{g.get('limit')} enabled={g.get('enabled')}"

    check("额度接口可读", t_quota_get)

    def t_quota_write():
        """真写入校验：带 values 包装，改一个值 → 写回 → 读回 → 再改回。

        ★ 2026-11-02（T-1）：原版把 GET 回来的 config 原样 POST，不带 values；
          服务端 `values = body.get("values")` 取不到就按空处理、照样返回 ok:true，
          所以这条测试从来没真正写过配置（永远 PASS）。
        """
        auth = {"Authorization": f"Bearer {token}"}
        base = f"{PANEL}/api/plug/astrbot_plugin_ncd_comfy/ncd-comfy/quota"

        def read_limit() -> int:
            st, body, _ = http(base, headers=auth)
            assert st == 200, f"读配置 HTTP {st}"
            cfg = json.loads(body).get("config") or {}
            assert cfg, "拿不到当前配置"
            return int(cfg["global_limit"])

        orig = read_limit()
        probe = orig + 1 if orig < 70 else max(1, orig - 1)

        st2, body2, _ = http(base + "/config", "POST",
                             {"values": {"global_limit": probe}}, auth)
        assert st2 == 200, f"写回 HTTP {st2}"
        d2 = json.loads(body2)
        assert d2.get("ok"), f"写回失败: {body2[:200]}"
        assert int((d2.get("config") or {}).get("global_limit", -1)) == probe, \
            f"写回返回的 limit 不是 {probe}: {body2[:200]}"

        now = read_limit()
        assert now == probe, f"写回没真正生效：{orig} → 期望 {probe}，实际 {now}"

        st3, _, _ = http(base + "/config", "POST", {"values": {"global_limit": orig}}, auth)
        assert st3 == 200, f"恢复 HTTP {st3}"
        back = read_limit()
        assert back == orig, f"恢复失败：期望 {orig}，实际 {back}"
        return f"{orig} → {probe} → {orig}（带 values，真写入并读回）"
    check("额度可写回（双向通信）", t_quota_write)

    print()
    print("=" * 70)
    print("三、NapCat（9002 代理 + 自动登录）")
    print("=" * 70)

    def t_nap_boot():
        st, body, _ = http(f"{NAPCAT_PROXY}/")
        assert st == 200, f"HTTP {st}"
        html = body.decode("utf-8", "replace")
        assert "SHA-256" in html and "Credential" in html, "跳板脚本不完整"
        return f"{len(body)} 字节，含自动登录逻辑"

    check("自动登录跳板", t_nap_boot)

    def t_nap_webui():
        st, body, _ = http(f"{NAPCAT_PROXY}/webui/")
        assert st == 200, f"HTTP {st}"
        return f"{len(body)} 字节"

    check("WebUI 外壳", t_nap_webui)

    nap = {"cred": ""}

    def nap_login(retry: int = 3) -> str:
        """换 Credential。NapCat 有 loginRate 限制，被限流时等一会儿重试。"""
        h = hashlib.sha256(f"{NAPCAT_TOKEN}.napcat".encode()).hexdigest()
        last = ""
        for i in range(retry):
            st, body, _ = http(f"{NAPCAT_PROXY}/api/auth/login", "POST", {"hash": h})
            try:
                d = json.loads(body)
            except Exception:  # noqa: BLE001
                d = {}
            cred = (d.get("data") or {}).get("Credential") or ""
            if cred:
                nap["cred"] = cred
                return cred
            last = body.decode("utf-8", "replace")[:120]
            if i < retry - 1:
                print(f"        （被限流，{20 * (i + 1)}s 后重试）{last}")
                time.sleep(20 * (i + 1))
        raise AssertionError(f"拿不到 Credential: {last}")

    def t_nap_login():
        cred = nap_login()
        return f"Credential {len(cred)} 字符"

    check("token 换 Credential", t_nap_login)

    def t_nap_auth():
        cred = nap["cred"] or nap_login()
        st, b2, _ = http(f"{NAPCAT_PROXY}/api/auth/check", "POST", {},
                         {"Authorization": f"Bearer {cred}"})
        assert st == 200, f"HTTP {st}"
        assert json.loads(b2).get("code") == 0, f"鉴权失败: {b2[:120]}"
        st3, b3, _ = http(f"{NAPCAT_PROXY}/api/QQLogin/CheckLoginStatus", "POST", {},
                          {"Authorization": f"Bearer {cred}"})
        d3 = json.loads(b3).get("data") or {}
        assert d3.get("isLogin"), f"QQ 未登录: {b3[:120]}"
        return f"isLogin=True loginPhase={d3.get('loginPhase')}"

    check("鉴权 + QQ 登录态", t_nap_auth)

    print()
    print("=" * 70)
    print("四、AstrBot 本体")
    print("=" * 70)

    def t_plugins():
        st, body, _ = http(f"{PHONE}/api/plugin/get", headers={"Authorization": f"Bearer {token}"})
        assert st == 200, f"HTTP {st}"
        data = json.loads(body)["data"]
        mine = [p for p in data if "ncd_" in (p.get("name") or "")]
        bad = [p["name"] for p in mine if not p.get("activated")]
        assert not bad, f"未启用: {bad}"
        vers = ", ".join(f"{p['name'].replace('astrbot_plugin_ncd_','')}={p.get('version')}" for p in mine)
        return f"{len(mine)} 个已启用 · {vers}"

    check("插件全部启用", t_plugins)

    PLUG_SRC = r"F:\NapCatDesk\ASTRBOT\data\plugins"

    def t_version_sync():
        """手机上的插件版本必须和本地源码 metadata 一致（防止「改了没装」）。"""
        st, body, _ = http(f"{PHONE}/api/plugin/get", headers={"Authorization": f"Bearer {token}"})
        data = json.loads(body)["data"]
        phone = {p["name"]: p.get("version") for p in data}
        bad, seen = [], 0
        import os
        for name in sorted(os.listdir(PLUG_SRC)):
            meta = os.path.join(PLUG_SRC, name, "metadata.yaml")
            if not name.startswith("astrbot_plugin_") or not os.path.isfile(meta):
                continue
            with open(meta, encoding="utf-8") as f:
                m = re.search(r"^version:\s*(\S+)", f.read(), re.M)
            if not m:
                continue
            want = m.group(1).strip().strip('"\'')
            got = phone.get(name)
            seen += 1
            if got != want:
                bad.append(f"{name}: 手机 {got} ≠ 源码 {want}")
        assert not bad, "; ".join(bad)
        return f"{seen} 个插件版本与源码一致"

    check("插件版本与源码同步", t_version_sync)

    print()
    print("=" * 70)
    print("五、ComfyUI 链路（拉起器 + min 版）")
    print("=" * 70)

    def t_watchdog():
        """ComfyUI 常驻看护（Windows 计划任务）在跑，8199 由它维护。

        ★ 2026-11-02（P1-2）：原来断言 8200 拉起器 /status==200；
          现在进程生命周期归 Windows 计划任务 + comfy_watchdog，8200 已退役。
        """
        import subprocess as _sp

        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-ScheduledTask -TaskName 'ComfyUI常驻看护' -EA SilentlyContinue).State"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        assert "Running" in out, f"计划任务「ComfyUI常驻看护」不在运行：{out!r}"
        st, body, _ = http(f"{COMFY}/system_stats", timeout=20)
        assert st == 200, f"ComfyUI 未就绪 HTTP {st}"
        return f"计划任务 Running · ComfyUI 就绪"

    check("ComfyUI 常驻看护在运行", t_watchdog)

    def t_comfy_up():
        """ComfyUI 是否就绪；没起就等常驻看护拉起来（不再依赖 8200 拉起器）。"""
        import time as _t

        last = ""
        for _ in range(12):          # 最多等 60 秒
            st, body, _ = http(f"{COMFY}/system_stats", timeout=20)
            if st == 200:
                d = json.loads(body)
                return (f"ComfyUI {d['system']['comfyui_version']} · "
                        f"{d['devices'][0]['name'][:34]}")
            last = f"HTTP {st}"
            _t.sleep(5)
        raise AssertionError(f"ComfyUI 一直未就绪（{last}）")

    check("min 版 ComfyUI 就绪", t_comfy_up)

    def t_models():
        st, body, _ = http(f"{COMFY}/object_info/UNETLoader", timeout=25)
        assert st == 200, f"HTTP {st}"
        lst = json.loads(body)["UNETLoader"]["input"]["required"]["unet_name"][0]
        moss = [x for x in lst if x.startswith("MOSS\\")]
        assert moss, "UNETLoader 里没有 MOSS 模型"
        assert any("mossImage_sft_pruned_int8" in x for x in moss), \
            f"找不到默认底模 mossImage_sft_pruned_int8；现有: {moss[:5]}"
        return f"UNETLoader {len(lst)} 个，MOSS {len(moss)} 个"

    check("默认底模 mossImage_sft_pruned_int8 在清单里", t_models)

    print()
    print("=" * 70)
    print("六、今晚改动的回归（默认值 / LoRA / 群聊策略 / 命令）")
    print("=" * 70)

    cfgbox: dict = {}

    def t_comfy_cfg():
        st, body, _ = http(
            f"{PHONE}/api/v1/plugins/config?plugin_id=astrbot_plugin_ncd_comfy",
            headers={"Authorization": f"Bearer {token}"}, timeout=30)
        assert st == 200, f"HTTP {st}"
        cfg = json.loads(body)["data"]["config"]
        cfgbox.update(cfg)
        assert cfg.get("comfy_preset") == "moss", f"默认预设是 {cfg.get('comfy_preset')}"
        assert 4 <= int(cfg.get("comfy_steps") or 0) <= 14, f"默认步数 {cfg.get("comfy_steps")} 不在加速档 4~14（当前应为 12）"
        assert float(cfg.get("comfy_cfg") or 0) == 1.0, f"CFG 是 {cfg.get('comfy_cfg')}"
        assert str(cfg.get("comfy_default_lora") or "").endswith(
            "MOSS-Image-SFT-8-steps.safetensors"
        ), f"默认 LoRA 是 {cfg.get('comfy_default_lora')!r}"
        return (f"preset={cfg.get('comfy_preset')} · steps={cfg.get('comfy_steps')} · "
                f"cfg={cfg.get('comfy_cfg')} · "
                f"lora={cfg.get('comfy_default_lora')} ×{cfg.get('comfy_default_lora_strength')}")

    check("默认出图参数（moss / 12步 / CFG1 / 8步LoRA）", t_comfy_cfg)

    def t_lora_resolve():
        """默认 LoRA 必须能在 moss 族里解析到（否则图注会显示「LoRA: 无」）。"""
        want = str(cfgbox.get("comfy_default_lora") or "")
        assert want, "配置里没有默认 LoRA"
        st, body, _ = http(f"{COMFY}/object_info/LoraLoader", timeout=30)
        assert st == 200, f"HTTP {st}"
        s = json.loads(body)["LoraLoader"]["input"]["required"]["lora_name"]
        lst = s[0] if isinstance(s[0], list) else s[1].get("options", [])
        moss = [n for n in lst if n.lower().startswith("moss\\")]
        assert want in moss, f"{want} 不在 moss 族里；族内前几个: {moss[:5]}"
        return f"moss 族 {len(moss)} 个，目标命中"

    check("默认 LoRA 可在 moss 族解析", t_lora_resolve)

    def t_lora_stacking():
        """8 步加速 LoRA 必须「总是带上」，且允许再叠风格 LoRA。

        这是踩过的坑：原来是 `if not loras:` 才加默认 LoRA，模型一指定风格 LoRA，
        加速 LoRA 就被挤掉，8 步出图直接发糊。
        """
        src = open(r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy\main.py",
                   encoding="utf-8").read()
        assert "loras.insert(" in src, "加速 LoRA 没有插到 loras 最前面（会被风格 LoRA 顶掉）"
        assert "if not loras:" not in src, "加速 LoRA 仍被「没指定才加」的门控着"
        assert "加速 LoRA" in src and "4~14" in src, "工具文案没说明加速 LoRA 的生效区间"
        assert "fast_mode" in src, "没有加速档判定 fast_mode"
        assert "comfy_cfg_normal" in src, "正常档没有独立 CFG（>14 步会吃 CFG1 发灰）"
        assert "叠加，不是替换" in src, "工具文案没说明风格 LoRA 是叠加"
        assert "4 <= int(steps_val) <= 14" in src, "强制区间不是 4~14 步"
        assert "（加速）" in src, "图注没有把加速 LoRA 标出来"
        strength = float(cfgbox.get("comfy_default_lora_strength") or 0)
        assert strength == 1.0, f"加速 LoRA 强度是 {strength}，应为 1.0"
        return f"总是插最前 · 文案含必带/叠加 · 强度 {strength:g}"

    check("8步加速LoRA 必带且可与风格LoRA叠加", t_lora_stacking)

    def t_new_loras():
        """今晚归类的 6 个 LoRA 必须被 ComfyUI 认到（分类子目录不影响识别）。"""
        st, body, _ = http(f"{COMFY}/object_info/LoraLoader", timeout=30)
        s = json.loads(body)["LoraLoader"]["input"]["required"]["lora_name"]
        lst = s[0] if isinstance(s[0], list) else s[1].get("options", [])
        need = ["观澜", "kieed", "ArtStyle-anon", "大白兔", "anireal", "detaile"]
        miss = [k for k in need if not any(k in n for n in lst)]
        assert not miss, f"没认到: {miss}"
        cats = {n.split("\\")[1] for n in lst if n.lower().startswith("moss\\") and n.count("\\") >= 2}
        return f"6 个新增都在 · 分类目录: {', '.join(sorted(cats)) or '无'}"

    check("归类的 6 个 LoRA 已被识别", t_new_loras)

    def t_one_reset():
        """只应该有一个 /reset（内置的）；插件不要再抢注。"""
        st, body, _ = http(f"{PHONE}/api/v1/commands",
                           headers={"Authorization": f"Bearer {token}"}, timeout=30)
        assert st == 200, f"HTTP {st}"
        items = json.loads(body)["data"]["items"]
        hits = [it for it in items
                if (it.get("effective_command") or it.get("original_command")) == "reset"]
        assert len(hits) == 1, f"有 {len(hits)} 个 reset: {[h.get('plugin') for h in hits]}"
        assert hits[0].get("plugin") == "builtin_commands", f"reset 来自 {hits[0].get('plugin')}"
        return f"reset 唯一，来自 {hits[0].get('plugin')}"

    check("reset 命令唯一（内置）", t_one_reset)

    def t_group_policy():
        """群聊两条消息策略的开关必须在，且语音/出图去文字钩子已带事件标记。"""
        assert cfgbox.get("comfy_image_drop_text_group") is True, \
            f"comfy_image_drop_text_group={cfgbox.get('comfy_image_drop_text_group')}"
        src = open(r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy\main.py",
                   encoding="utf-8").read()
        assert '_comfy_image_turn' in src and '_comfy_voice_turn' in src, "事件内标记没写进源码"
        assert 'is_model_result' in src, "缺少 is_model_result 兜底"
        assert 'if not loras and preset_name == "sdxl":' not in src, \
            "默认 LoRA 还卡在「只有 sdxl」的门里"
        return "开关在 · 事件标记 + is_model_result 兜底 · 无 sdxl 硬编码"

    check("群聊两条消息策略 + 钩子安全", t_group_policy)

    def t_upscale():
        """放大（超分）是可选项：配置项齐、源码里有注入、本机有放大模型。"""
        need = (
            "comfy_upscale_default",
            "comfy_upscale_model",
            "comfy_upscale_factor",
            "comfy_upscale_max_side",
            "comfy_upscale_method",
        )
        miss = [k for k in need if k not in cfgbox]
        assert not miss, f"缺配置项: {miss}"
        src = io.open(
            r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy\main.py",
            encoding="utf-8",
        ).read()
        pre = io.open(
            r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy\presets.py",
            encoding="utf-8",
        ).read()
        assert "_insert_upscale" in pre and "upscale_resize" in pre, "presets 里没有放大注入"
        assert 'upscale: str = ""' in src, "comfy_generate 没有 upscale 参数"
        assert "_parse_upscale" in src, "没有 upscale 参数解析"
        _decl = src.find('up_scale_text = ""')
        _use = src.find('if up_scale_text:')
        assert 0 < _decl < _use, "up_scale_text 没在函数级先声明（会 NameError）"
        assert _decl < src.find('async with self._gen_slot(event, sync_quota'), \
            "up_scale_text 声明应在出图函数入口（async with 之前）"
        assert "@staticmethod\n    def _image_caption" not in src, \
            "_image_caption 又变回 staticmethod —— 里面用了 self，会 NameError"
        assert 'if low in ("model", "upscale", "超分", "放大")' in src, \
            "裸写 model 的分支丢了（会被当成模糊模型名然后放弃）"
        assert "upscale_models" in src, "comfy_models 不支持列放大模型"
        pre = io.open(r"F:\\NapCatDesk\\ASTRBOT\\data\\plugins\\astrbot_plugin_ncd_comfy\\presets.py", encoding="utf-8").read()
        assert "upscale_preshrink" in pre, "4x 模型放大前没有预缩小（中间图会爆显存）"
        assert "moss_control_lora" in pre and "krea2_control_lora" in pre, \
            "控制 LoRA 还是写死路径（库重排后会 400）"
        assert "Moss\\\\工具\\\\moss_image_color_depth_control" in pre, "控制 LoRA 兜底路径没更新"
        assert "fail-closed" in src, "is_model_result 兜底不是 fail-closed"
        assert '"upscale": upscale,' in src, "异步出图入队时把 upscale 参数丢了（传了也不生效）"
        assert int(cfgbox.get("comfy_max_side") or 0) == 1920, \
            f"基础出图单边上限是 {cfgbox.get('comfy_max_side')}，应为 1920"
        assert int(cfgbox.get("comfy_upscale_max_side") or 0) == 4096, \
            f"放大后单边上限是 {cfgbox.get('comfy_upscale_max_side')}，应为 4096"
        st, body, _ = http(f"{COMFY}/object_info/UpscaleModelLoader", timeout=25)
        assert st == 200, f"HTTP {st}"
        s = json.loads(body)["UpscaleModelLoader"]["input"]["required"]["model_name"]
        lst = s[0] if isinstance(s[0], list) else s[1].get("options", [])
        assert lst, "这台 ComfyUI 没有放大模型（models/upscale_models 为空）"
        return (f"配置 5 项 · 放大模型 {len(lst)} 个 · "
                f"默认 {cfgbox.get('comfy_upscale_factor')}x · 上限 {cfgbox.get('comfy_upscale_max_side')}px")

    check("放大（超分）可选功能", t_upscale)

    def t_preset_defaults():
        """8 步 / CFG 1 只属于 moss；其它预设要走 28 步 / CFG 6。

        事故：SDXL 套了 comfy_steps=8 + comfy_cfg=1，出图一片灰白雾
        （实测 512x512 像素标准差 38.7 vs 正常 64.6）。
        """
        for k in ("comfy_steps_other", "comfy_cfg_other"):
            assert k in cfgbox, f"缺配置项 {k}"
        src = io.open(
            r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy\main.py",
            encoding="utf-8",
        ).read()
        assert "elif preset_name == \"moss\":" in src, "步数/CFG 没有按 moss 分流"
        assert "other_steps" in src and "other_cfg" in src, "非 moss 分支没读 other 配置"
        assert ("默认 12 步" in src) or ("别把 12 步/CFG1 套到 sdxl" in src), "工具文案没提醒 moss 默认 12 步、sdxl 别套"
        moss_steps = int(cfgbox.get("comfy_steps") or 0)
        other_steps = int(cfgbox.get("comfy_steps_other") or 0)
        other_cfg = float(cfgbox.get("comfy_cfg_other") or 0)
        assert other_steps >= 20, f"非 moss 步数只有 {other_steps}，SDXL 会糊"
        assert other_cfg >= 4, f"非 moss CFG 只有 {other_cfg}，会发灰"
        return (f"moss {moss_steps}步/CFG{float(cfgbox.get('comfy_cfg') or 0):g}"
                f" · 其它 {other_steps}步/CFG{other_cfg:g}")

    def t_quota_split():
        import io as _io
        base = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy"
        page = _io.open(base + r"\pages\quota\index.html", encoding="utf-8").read()
        src = _io.open(base + r"\quota.py", encoding="utf-8").read()
        assert "私聊全局成功生图" in page, "额度页没把全局口径标成私聊"
        assert "群聊全局生图" in page, "额度页没有群聊生图统计"
        assert "group_counts" in src and "group_totals" in src, "后端没有群聊计数"
        return "私聊/群聊分开统计"

    def t_send_block():
        """2026-11-01 回退：不再搞风控冷却/待发队列，失败直接回错误给模型。"""
        import io as _io
        base = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy"
        src = _io.open(base + r"\main.py", encoding="utf-8").read()
        for gone in ("_img_send_block", "_send_block_left", "_pending_images",
                     "_note_send_failure", "_drain_pending_images"):
            assert gone not in src, f"风控限时代码没清干净: {gone}"
        assert "按用户要求回退「风控限时」" in src, "没有回退说明"
        assert "发送生成的图失败" in src, "发送失败分支丢了"
        assert "图画好了但发送失败" not in src, "发送错误还会回给模型（会被复述到群里）"
        assert "可能已送达" in src, "没说明「抛错但可能已送达」这个情况"
        return "无冷却/无排队，失败直接回错误"

    def t_pc_offline():
        import io as _io
        base = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy"
        src = _io.open(base + r"\main.py", encoding="utf-8").read()
        assert "comfy未连接电脑" in src, "没有「comfy未连接电脑」状态文案"
        assert "_pc_link_state" in src, "没有链路探测"
        assert src.count("comfy未连接电脑") >= 2, "状态文案没覆盖 tools 与 status"
        return "离线时直接说明"

    def t_tool_decorators():
        import ast as _ast
        import io as _io
        base = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy"
        src = _io.open(base + r"\main.py", encoding="utf-8").read()
        tree = _ast.parse(src)
        got = {}
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    txt = _ast.unparse(d)
                    if "llm_tool" in txt and "name=" in txt:
                        name = txt.split("name=")[1].rstrip(")").strip().strip(chr(39)+chr(34))
                        got[name] = node.name
        bad = {k: v for k, v in got.items() if k != v}
        assert not bad, f"工具注册错位（名字与函数不一致）: {bad}"
        assert "comfy_tools" in got and "comfy_generate" in got, "核心工具没注册"
        return f"{len(got)} 个工具注册正常"

    check("工具装饰器没有挂错函数", t_tool_decorators)

    check("没连电脑时 comfy 状态显示未连接", t_pc_offline)

    check("发图失败直接回错误（无风控冷却）", t_send_block)

    check("额度面板区分私聊与群聊生图", t_quota_split)

    check("moss 与非 moss 的步数/CFG 分流", t_preset_defaults)

    # ── 2026-11-01 新增 ────────────────────────────────────────────────
    def t_lora_tristate():
        src = io.open(
            r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy\main.py",
            encoding="utf-8").read()
        assert "_add_on_default" in src, "没有解析 + 前缀"
        assert "_keep_default_style" in src, "没有默认风格开关"
        assert "替换默认风格" in src and "叠加" in src, "工具说明没写三态语义"
        return "留空=默认 / 名字=替换 / +名字=叠加"

    check("LoRA 三态语义（默认 / 替换 / 叠加）", t_lora_tristate)

    def t_default_style_lora():
        st, body, _ = http(
            f"{PHONE}/api/v1/plugins/config?plugin_id=astrbot_plugin_ncd_comfy",
            timeout=30,
            headers={"Authorization": f"Bearer {token}"})
        assert st == 200, f"配置接口 HTTP {st}"
        cfg = (json.loads(body).get("data") or {}).get("config") or {}
        val = str(cfg.get("comfy_default_style_lora") or "")
        assert "秘制柔和动漫风24" in val, f"默认风格没指向猫猫24：{val!r}"
        w = cfg.get("comfy_default_style_strength")
        assert abs(float(w) - 0.8) < 1e-6, f"默认风格强度 {w!r}"
        return f"{val.split(chr(92))[-1]} @ {w}"

    check("默认风格 LoRA = 秘制柔和动漫风24", t_default_style_lora)

    def t_reference_guard():
        """reference / 历史图只能读三根目录；http 拒私网（P0-2）。

        ★ 2026-11-02（第四批）：护栏已抽到 safety.py，这里改成断言「main 真的调了它」，
          文案/行为细节交给 离线回归.py 的行为断言（t_offline）。
        """
        import io as _io

        base = r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy"
        src = _io.open(base + r"\main.py", encoding="utf-8").read()
        saf = _io.open(base + r"\safety.py", encoding="utf-8").read()
        assert "_safety.path_allowed" in src, "main.py 没有调用 safety.path_allowed"
        assert "_safety.public_url_error" in src, "main.py 没有调用 safety.public_url_error"
        assert "async def _image_url_bytes(self, url: str, umo" in src, \
            "_image_url_bytes 没带 umo（无法按会话目录白名单校验）"
        assert "拒绝读取白名单外的文件" in src, "没有拒绝日志"
        assert "只允许公网地址" in saf, "safety.py 没有公网地址限制"
        return "main 调 safety（白名单 + SSRF），行为由离线回归验证"

    def t_offline():
        """跑 离线回归.py：6 条行为断言（白名单/SSRF/裁剪/版本/接口/额度隔离）。"""
        import subprocess as _sp
        import os as _os

        script = _os.path.join(r"F:\Deepseek Harness WorkTable", "离线回归.py")
        r = _sp.run([sys.executable, script], capture_output=True, text=True, timeout=300)
        tail = (r.stdout or "").strip().splitlines()[-3:]
        assert r.returncode == 0, "离线回归失败：\n" + (r.stdout or r.stderr or "")[-500:]
        return " / ".join(x.strip() for x in tail if x.strip())[-120:]

    check("reference 取图受白名单保护", t_reference_guard)

    check("离线回归（行为断言 6 条）", t_offline)

    def t_queue_interval():
        """发图间隔在排队阶段挡，发送兜底最多 15 秒（P0-4）。"""
        import io as _io

        src = _io.open(
            r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy\main.py",
            encoding="utf-8").read()
        assert "不占死队列" in src, "发送兜底没封顶"
        assert 'self._image_allowed(umo0, job0.get("event"))' in src, \
            "取任务时没按会话间隔过滤"
        assert "最多等 {cap}s" in src, "没有封顶等待日志"
        return "排队阶段挡 + 发送封顶 15s"

    check("群聊发图间隔不占死队列", t_queue_interval)

    def t_deputy_first_try():
        """副管理员判定：命令入口走 async 版本 + 刷新任务持引用（P1-3）。"""
        import io as _io

        src = _io.open(
            r"F:\NapCatDesk\ASTRBOT\data\plugins\astrbot_plugin_ncd_comfy\main.py",
            encoding="utf-8").read()
        assert "_is_admin_event_async" in src, "没有 async 管理员判定"
        assert "add_done_callback(tasks.discard)" in src, "刷新任务没持引用"
        assert src.count("await self._is_admin_event_async(event)") >= 2, \
            "命令入口没改用 async 判定"
        return "命令入口 async 判定 + task 持引用"

    check("副管理员首次即可判定", t_deputy_first_try)

    def t_link_plugin():
        st, body, _ = http(
            f"{PHONE}/api/plug/astrbot_plugin_ncd_link/ncd-link/status",
            headers={"Authorization": f"Bearer {token}"})
        assert st == 200, f"看护接口 HTTP {st}"
        d = json.loads(body)
        data = d.get("data", d)
        assert isinstance(data.get("online"), bool), f"online 不是布尔: {data.get('online')!r}"
        return f"online={data['online']} · 断线记录 {len(data.get('gaps') or [])} 条"

    check("链路看护插件在跑（QQ 在线探测）", t_link_plugin)

    def t_awake_page():
        st, body, _ = http("http://127.0.0.1:9001/awake", timeout=15)
        assert st == 200, f"/awake HTTP {st}"
        html = body.decode("utf-8", "replace")
        assert "电脑保持唤醒" in html, "本地防睡眠页没有电脑卡片"
        return f"{len(body)} 字节 · 含电脑卡片"

    check("面板防睡眠本地页可用", t_awake_page)

    def t_linkd():
        st, body, _ = http("http://127.0.0.1:8201/link/state", timeout=10)
        assert st == 200, f"linkd HTTP {st}"
        d = json.loads(body)
        assert d.get("pc_reachable") is True, "linkd 没报 pc_reachable"
        return f"detail={d.get('detail')}"

    check("电脑侧 linkd 探针在线", t_linkd)

    print()
    print("=" * 70)
    ok = sum(1 for _, p, _ in results if p)
    bad = [r for r in results if not r[1]]
    print(f"汇总：{ok}/{len(results)} 通过" + (f"，{len(bad)} 项失败" if bad else "，全部通过"))
    if bad:
        print()
        for name, _, detail in bad:
            print(f"  ✗ {name}\n      {detail}")
    print("=" * 70)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
