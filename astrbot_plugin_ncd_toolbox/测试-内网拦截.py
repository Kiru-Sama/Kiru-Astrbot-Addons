"""自测：抓正文的内网/回环地址拦截。

背景：`fetch_page` 是在【用户自己那台机器上、带着登录态的浏览器】里跑的。
模型可能被网页正文牵着去访问本机服务 —— 那是它平时碰不到的地方。
实测本机就有几个"读了就出事"的端口：

    9222  CDP 调试端点 —— 读 /json 能列出调试目标，等于把浏览器控制权递出去
    5099  SnowLuma WebUI —— 能发消息
    1234  LM Studio —— 无鉴权，能加载/卸载模型
    6185  AstrBot 面板

所以要求：主机名解析出来的【每一个】地址都必须是公网地址。

不联网：下面用的全是字面 IP，不做 DNS 查询。
用法：<AstrBot>\\.venv\\Scripts\\python.exe 测试-内网拦截.py
"""

import asyncio
import json
import pathlib
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

import main as plugin_main  # noqa: E402
from browser import public_url_error  # noqa: E402

FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    if ok:
        print(f"  OK  {label}")
    else:
        print(f"  ✗   {label} {detail}")
        FAILURES.append(f"{label} {detail}".strip())


# (网址, 该不该拦)
CASES: list[tuple[str, bool]] = [
    # ── 必须拦下面这些 ────────────────────────────────────────────
    ("http://127.0.0.1:5099/", True),
    ("http://127.0.0.1:9222/json", True),
    ("http://127.0.0.1:1234/v1/models", True),
    ("http://127.0.0.1:6185/", True),
    ("https://127.0.0.1/", True),
    ("http://0.0.0.0:1234/", True),
    ("http://localhost:5099/", True),
    ("http://10.0.0.5/", True),
    ("http://192.168.1.1/", True),
    ("http://172.16.0.1/", True),
    ("http://169.254.1.1/", True),          # 链路本地
    ("http://100.64.0.1/", True),           # 运营商级 NAT
    ("http://[::1]:6185/", True),           # IPv6 回环
    ("http://[fe80::1]/", True),            # IPv6 链路本地
    ("http://[::ffff:127.0.0.1]/", True),   # IPv4 映射的 IPv6 回环
    # ── 协议/格式不对 ────────────────────────────────────────────
    ("file:///C:/Windows/win.ini", True),
    ("ftp://example.com/", True),
    ("javascript:alert(1)", True),
    ("http://", True),
    ("http:///path", True),
    ("http://example.com:99999/", True),    # 端口越界
    # ── 必须放行 ─────────────────────────────────────────────────
    ("http://93.184.216.34/", False),       # 公网字面 IP（example.com）
    ("https://1.1.1.1/", False),
    ("https://8.8.8.8:443/x", False),
    ("http://[2606:4700:4700::1111]/", False),   # 公网 IPv6
]


async def main() -> int:
    print("===== 一、地址判定表 =====")
    for url, should_block in CASES:
        reason = public_url_error(url)
        blocked = reason is not None
        mark = "拦" if should_block else "放"
        print(f"  [{mark}] {url:38s} {('→ ' + reason[:60]) if reason else ''}")
        if blocked != should_block:
            FAILURES.append(
                f"{url} 应当{'拦' if should_block else '放行'}，实际"
                f"{'拦了' if blocked else '放行了'}（{reason or '—'}）"
            )

    print("\n===== 二、fetch_page 必须在内网网址上【根本不去碰浏览器】=====")
    plugin = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    plugin._conf = {}
    plugin._browser = None
    plugin._browser_lock = asyncio.Lock()

    class _Ev:
        pass

    for url in ("http://127.0.0.1:5099/", "http://127.0.0.1:9222/json",
                "http://192.168.1.1/", "file:///C:/Windows/win.ini"):
        raw = await plugin.fetch_page(_Ev(), url=url)
        data = json.loads(raw)
        ok = "error" in data
        check(ok, f"fetch_page 拒绝 {url}", raw[:120])
        if plugin._browser is not None:
            FAILURES.append(f"{url} 竟然把浏览器拉起来了 —— 拦截必须发生在导航之前")

    check(
        plugin._browser is None,
        "被拦的请求一次都没有拉起浏览器（拦截发生在导航之前）",
    )

    print("\n===== 三、负面对照：公网网址不该被误拦 =====")
    check(
        public_url_error("http://93.184.216.34/") is None,
        "公网字面 IP 放行（证明上一节的「拦」不是无差别全拦）",
    )

    print("\n===== 四、开关 block_private_urls=false 时要能放行 =====")
    # 为什么要给这个开关：实测这台机器上 example.com / github.com 会被解析成
    # 127.0.0.1（DNS 屏蔽手法），而 Edge 走 DoH 其实打得开 example.com
    # （实测 status=200）。严格判据会误伤这类站，所以留一个能关的开关。
    plugin2 = plugin_main.NcdToolboxPlugin.__new__(plugin_main.NcdToolboxPlugin)
    plugin2._conf = {"block_private_urls": False}
    plugin2._browser = None
    plugin2._browser_lock = asyncio.Lock()
    plugin2._conf_int = lambda key, default: default     # 不真的去连浏览器

    class _FakeBrowser:
        async def fetch_text(self, url, max_chars):
            return {
                "url": url, "title": "本地页", "status": 200,
                "text": "内网正文", "truncated": False, "total_chars": 4,
            }

    async def _fake_get_browser():
        return _FakeBrowser()

    plugin2._get_browser = _fake_get_browser
    raw = await plugin2.fetch_page(_Ev(), url="http://127.0.0.1:5099/")
    data = json.loads(raw)
    check(
        "error" not in data,
        "关掉开关后，内网地址确实放行（开关真的能生效，不是摆设）",
        raw[:120],
    )

    if FAILURES:
        print("\n失败项:")
        for item in FAILURES:
            print(f"  ✗ {item}")
        return 1
    print("\nPASS: 内网/回环/异常协议一律拦下，公网放行，且拦截发生在导航之前")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
