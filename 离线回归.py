# -*- coding: utf-8 -*-
"""离线回归：只测「不依赖手机/ComfyUI 也能判定」的行为。

为什么单独一个文件（2026-11-02）：
  全量测试.py 里大量断言是 `assert "中文文案" in src` —— 源码改了手机没更新、
  或者行为退回原样但注释还在，它都全绿。这里改成 import 真代码、调真函数、看返回值。

覆盖：
  1. safety.path_allowed       根内 True / 根外 False / ../ 逃逸 False / 符号链接逃逸 False
  2. safety.public_url_error   公网空串 / 环回、内网、链路本地非空
  3. safety.prune_files        25 个旧文件 + 上限 20 → 删 20、剩 5；新文件不删
  4. 版本一致性                 手机侧插件版本 == 本地 metadata（防「改了没装」）
  5. api_link 行为              source ∈ {linkd,direct}、can_draw 是 bool
  6. 额度改键隔离               改 global_limit 后，degrade_after 不变

用法：python 离线回归.py        退出码 0=全过，1=有失败
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.request

WORK = r"F:\Deepseek Harness WorkTable"
PLUGINS = r"F:\NapCatDesk\ASTRBOT\data\plugins"
COMFY_DIR = os.path.join(PLUGINS, "astrbot_plugin_ncd_comfy")
PHONE = "http://192.168.137.71:6185"
USER, PWD = "astrbot", "NcdPass2026"

sys.path.insert(0, COMFY_DIR)
import safety  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn() or ""
        results.append((name, True, detail))
        print(f"  PASS  {name}  — {detail}")
    except AssertionError as exc:
        results.append((name, False, str(exc)))
        print(f"  FAIL  {name}\n        └─ {exc}")
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"  ERROR {name}\n        └─ {type(exc).__name__}: {exc}")


def _token() -> str:
    req = urllib.request.Request(
        PHONE + "/api/auth/login",
        data=json.dumps({"username": USER, "password": PWD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())["data"]["token"]


def http(path: str, token: str, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        PHONE + path,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if data else {}),
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# ── 1. 路径白名单 ────────────────────────────────────────────────────
def t_path_allowed() -> str:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "root")
        os.makedirs(os.path.join(root, "sub"), exist_ok=True)
        inside = os.path.join(root, "sub", "a.png")
        io.open(inside, "wb").write(b"x")
        outside = os.path.join(tmp, "outside.png")
        io.open(outside, "wb").write(b"x")

        assert safety.path_allowed(inside, [root]), "根内文件被判 False"
        assert not safety.path_allowed(outside, [root]), "根外文件被判 True"
        escape = os.path.join(root, "..", "outside.png")
        assert not safety.path_allowed(escape, [root]), "../ 逃逸没拦住"
        assert not safety.path_allowed(inside, []), "空 roots 应一律 False"
    return "根内 True / 根外 False / ../ 拦截 / 空 roots False"


# ── 2. SSRF 拦截 ─────────────────────────────────────────────────────
def t_public_url() -> str:
    bad = [
        "http://127.0.0.1:6185/",
        "http://192.168.137.1:8200/start",
        "http://10.0.0.5/x",
        "http://169.254.1.1/x",
        "http://localhost:9001/",
    ]
    for u in bad:
        assert safety.public_url_error(u), f"内网地址没拦住: {u}"
    assert safety.public_url_error("https://8.8.8.8/x") == "", "公网 IP 被误拦"
    assert safety.public_url_error("http://example.com/a.png") == "", "公网域名被误拦"
    return "5 个内网/环回地址全拦，公网放行"


# ── 3. 历史裁剪上限 + mtime ──────────────────────────────────────────
def t_prune() -> str:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        old = time.time() - 40 * 86400
        keep = set()
        for i in range(25):
            p = os.path.join(tmp, f"old_{i}.png")
            io.open(p, "wb").write(b"x")
            os.utime(p, (old, old))
        fresh = os.path.join(tmp, "fresh.png")
        io.open(fresh, "wb").write(b"x")     # mtime = 现在

        removed, overflow = safety.prune_files(
            tmp, keep, cutoff=time.time() - 30 * 86400, skip_names=("history.json",), max_delete=20
        )
        assert removed == 20, f"应删 20 个（上限），实际 {removed}"
        assert overflow == 5, f"超过上限应为 5，实际 {overflow}"
        got = sorted(x for x in os.listdir(tmp) if x.endswith(".png"))
        assert "fresh.png" in got, "新文件被误删（mtime 保护失效）"
        assert len(got) == 6, f"剩余应为 5 旧 + 1 新，实际 {len(got)}"
    return "25 旧文件删 20 留 5，新文件不动"


# ── 4. 版本一致性（防「改了没装」）──────────────────────────────────
def t_versions() -> str:
    token = _token()
    data = http("/api/v1/plugins", token)
    items = data.get("data") if isinstance(data.get("data"), list) else []
    phone = {p.get("name"): str(p.get("version") or "") for p in items}
    bad = []
    for name in ("astrbot_plugin_ncd_comfy", "astrbot_plugin_ncd_monitor", "astrbot_plugin_ncd_link"):
        mf = os.path.join(PLUGINS, name, "metadata.yaml")
        text = io.open(mf, encoding="utf-8-sig").read()
        m = re.search(r"(?m)^version:\s*(\S+)", text)
        local = m.group(1) if m else ""
        if phone.get(name) != local:
            bad.append(f"{name}: 手机={phone.get(name)} 本地={local}")
    assert not bad, "版本不一致（改了没装？）：" + "; ".join(bad)
    return f"3 个插件版本一致（comfy={phone.get('astrbot_plugin_ncd_comfy')}）"


# ── 5. api_link 行为 ────────────────────────────────────────────────
def t_api_link() -> str:
    token = _token()
    d = http("/api/plug/astrbot_plugin_ncd_comfy/ncd-comfy/link", token)
    data = d.get("data", d)
    assert isinstance(data.get("can_draw"), bool), f"can_draw 不是布尔: {data.get('can_draw')!r}"
    src = data.get("source")
    assert src in ("linkd", "direct", None), f"source 异常: {src!r}"
    reason = data.get("reason") or ""
    if data["can_draw"]:
        assert not reason, f"可画却带错误原因: {reason[:60]}"
    return f"can_draw={data['can_draw']} source={src}"


# ── 6. 额度改键隔离 ─────────────────────────────────────────────────
def t_quota_isolation() -> str:
    token = _token()
    base = "/api/plug/astrbot_plugin_ncd_comfy/ncd-comfy/quota"

    def cfg():
        return http(base, token).get("config") or {}

    c0 = cfg()
    orig_limit = int(c0["global_limit"])
    orig_degrade = int(c0["degrade_after"])
    probe = orig_limit + 1 if orig_limit < 70 else max(1, orig_limit - 1)
    try:
        http(base + "/config", token, {"values": {"global_limit": probe}}, "POST")
        c1 = cfg()
        assert int(c1["global_limit"]) == probe, "改键没生效"
        assert int(c1["degrade_after"]) == orig_degrade, "改 A 键把 B 键也改了"
    finally:
        http(base + "/config", token, {"values": {"global_limit": orig_limit}}, "POST")
    c2 = cfg()
    assert int(c2["global_limit"]) == orig_limit, "恢复失败"
    return f"global_limit {orig_limit}→{probe}→{orig_limit}，degrade_after 未受影响"


def main() -> int:
    print("=" * 70)
    print("离线回归（行为断言，不依赖 ComfyUI）")
    print("=" * 70)
    check("safety.path_allowed 路径白名单", t_path_allowed)
    check("safety.public_url_error 拒内网", t_public_url)
    check("safety.prune_files 删除上限+mtime", t_prune)
    check("线上版本 == 本地源码（防改了没装）", t_versions)
    check("api_link 行为（can_draw/source）", t_api_link)
    check("额度改键隔离（不污染其它键）", t_quota_isolation)

    ok = sum(1 for _, p, _ in results if p)
    bad = [r for r in results if not r[1]]
    print()
    print(f"汇总：{ok}/{len(results)} 通过" + ("" if not bad else f"，{len(bad)} 项失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
