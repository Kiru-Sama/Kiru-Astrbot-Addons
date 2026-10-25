"""用本机 Edge 的 CDP 接口做搜索与正文抓取。

设计取舍：
  · 只依赖 aiohttp + websockets（AstrBot 环境自带），不引入 playwright/selenium。
  · 复用同一个标签页，避免每搜一次就多开一个窗口。
  · 浏览器进程由本模块拉起并常驻，用户能看见它在搜；插件卸载时才回收。
  · 页面就绪用「轮询表达式」判断，不用固定睡眠 —— 固定睡眠要么白等要么抓空。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import websockets

from astrbot.api import logger

# Edge 的常规安装位置。装到别处就在插件配置里填 edge_path。
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

DEFAULT_SEARCH_URL = "https://cn.bing.com/search?q={query}"


def public_url_error(url: str) -> str | None:
    """这个网址能不能安全打开。能则返回 None，不能则返回给模型看的原因。

    ★ 为什么要拦：抓正文是在【你自己那台机器上、带着登录态的浏览器】里跑的，
      而模型完全可能被网页正文牵着去访问本机服务 —— 那是它平时碰不到的地方。
      实测本机就有几个「读了就出事」的端口：

          9222  CDP 调试端点 —— 读 /json 能列出调试目标，等于把浏览器控制权递出去
          5099  SnowLuma WebUI —— 能发消息
          1234  LM Studio —— 无鉴权，能加载/卸载模型
          6185  AstrBot 面板

      所以这里要求主机名解析出来的【每一个】地址都是公网地址，一个内网就拒。

    ★ 边界（有意不做的）：不做「先解析、再钉住 IP 去连」，所以 DNS 重绑定理论上
      能绕过。堵它得去 CDP 层拦截每个子请求，成本远大于收益 —— 这里不追求绝对。
      另外这只拦 fetch_page 自己发起的导航；页面里的图片/脚本打内网不归它管。
    """
    try:
        parsed = urlparse(url)
        port = parsed.port  # 非法端口会在这里抛 ValueError
    except ValueError as exc:
        return f"网址格式不对：{exc}"

    if parsed.scheme not in ("http", "https"):
        return f"只能打开 http/https 网址，收到的是 {parsed.scheme or '(空)'}://"
    host = parsed.hostname
    if not host:
        return "网址里没有主机名"

    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(
            host, port, proto=socket.IPPROTO_TCP, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        return f"域名解析失败（{host}）：{exc}"

    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            return f"{host} 解析出了无法识别的地址：{raw}"
        if not ip.is_global:
            return (
                f"拒绝打开 {host}（解析到 {ip}）—— 这是本机或内网地址。"
                "本插件只允许打开公网网址，避免网页内容诱导它去读你本机的服务。"
            )
    return None

# Bing 结果页就绪的判据。
# 注意：只判断 <li> 存在是不够的 —— Bing 会先渲染空壳列表项再填文字，
# 那时抓到的标题全是空串。这里要求首条结果真的有文字。
BING_READY = """(() => {
    const a = document.querySelector('#b_results > li.b_algo h2 a');
    return !!(a && (a.textContent || '').trim());
})()"""

# 从 Bing 结果页抽取标题/链接/摘要。
# 用 textContent 而不是 innerText：innerText 依赖排版，元素尚未排版时会返回空串。
#: 导航时拦掉的资源类型：图片 / 字体 / 音视频。CSS 留着（影响正文可见性）。
BLOCK_URL_PATTERNS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico",
    "*.woff", "*.woff2", "*.ttf", "*.otf",
    "*.mp4", "*.webm", "*.mp3", "*.m4a", "*.avi", "*.mov",
]


BING_EXTRACT = """(() => {
    const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
    const out = [];
    for (const li of document.querySelectorAll('#b_results > li.b_algo')) {
        const a = li.querySelector('h2 a');
        if (!a) continue;
        const sn = li.querySelector('.b_caption p, .b_lineclamp2, .b_lineclamp3, .b_lineclamp4');
        out.push({
            title: clean(a.textContent),
            url: a.href,
            snippet: clean(sn && sn.textContent)
        });
    }
    return JSON.stringify({ title: document.title, url: location.href, items: out });
})()"""

# 正文抓取的就绪判据。
# ★ 不要用 readyState === 'complete'：只要页面上有一个资源（广告、统计脚本、
#   被墙的 CDN）永远加载不完，complete 就永远不触发，整次抓取必然超时。
#   实测 notebookcheck-cn.com 就是这种站。改用 DOMContentLoaded 放行，
#   剩下的动态内容交给 settle 延时。
MAIN_TEXT_READY = "document.readyState !== 'loading'"

# 正文抓取：优先 article/main，退化到 body。innerText 已排除 script/style。
# 同时取回 HTTP 状态码 —— 403/404 页面本身也有正文，不区分就会把错误页当资料喂给模型。
# responseStatus 来自 Navigation Timing（Chromium 109+）。
MAIN_TEXT_EXTRACT = """(() => {
    const pick = document.querySelector('article') || document.querySelector('main') || document.body;
    let text = (pick && pick.innerText) || '';
    text = text.replace(/\\n{3,}/g, '\\n\\n').trim();
    const nav = performance.getEntriesByType('navigation')[0];
    const status = nav && nav.responseStatus ? nav.responseStatus : 0;
    return JSON.stringify({ title: document.title, url: location.href, text: text, status: status });
})()"""


def find_edge() -> str:
    """返回本机 Edge 的可执行文件路径，找不到返回空串。"""
    for path in EDGE_CANDIDATES:
        if Path(path).is_file():
            return path
    return ""


class _CdpTab:
    """一条到某个页面标签的 CDP 连接。

    用一个后台任务把响应分发给各自的 future，这样事件帧不会把响应帧挤掉。
    """

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._pump: asyncio.Task | None = None

    async def __aenter__(self) -> "_CdpTab":
        self._ws = await websockets.connect(self._ws_url, max_size=64 * 1024 * 1024)
        self._pump = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._pump is not None:
            self._pump.cancel()
        if self._ws is not None:
            await self._ws.close()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                fut = self._pending.get(msg.get("id"))
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[toolbox] CDP 读循环结束: {exc}")

    async def send(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """发一条 CDP 命令并等它的响应。"""
        if self._ws is None:
            raise RuntimeError("CDP 连接尚未建立")
        self._seq += 1
        seq = self._seq
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        try:
            await self._ws.send(
                json.dumps({"id": seq, "method": method, "params": params or {}})
            )
            msg = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(seq, None)
        if "error" in msg:
            raise RuntimeError(f"CDP {method} 失败: {msg['error']}")
        return msg.get("result", {})

    async def eval(self, expression: str, timeout: float = 30.0) -> Any:
        """在页面里执行一段表达式，取回它的值。"""
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"页面表达式出错: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")


def keyword_windows(
    text: str,
    keywords: list[str],
    budget: int = 4000,
    window: int = 700,
) -> str:
    """只挑「命中关键词」的段落，拼成一段有上下文的摘录。

    为什么：长文的答案常在中段，固定取前 4000 字经常刚好错过；
    按关键词选段落既提高命中率，又比整页塞给模型省 token。
    """
    text = (text or "").strip()
    if not text:
        return ""
    if not keywords:
        return text[:budget]

    # 先按空行切段；很多页面整页没有空行，退化成按句号切
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if len(paras) <= 1:
        paras = [p.strip() for p in re.split(r"(?<=[。！？；])", text) if p.strip()]

    lowered = [k.lower() for k in keywords if k and len(k) >= 2]
    if not lowered:
        return text[:budget]

    scored: list[tuple[int, int, str]] = []
    for i, p in enumerate(paras):
        low = p.lower()
        hits = sum(1 for k in lowered if k in low)
        if hits:
            scored.append((hits, i, p))
    if not scored:
        return text[:budget]

    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen: list[tuple[int, str]] = []
    used = 0
    for _hits, i, p in scored[:8]:
        seg = p[:window]
        if chosen and used + len(seg) > budget:
            continue
        chosen.append((i, seg))
        used += len(seg)
        if used >= budget:
            break

    chosen.sort(key=lambda x: x[0])
    body = "\n……\n".join(seg for _i, seg in chosen)
    # 开头补一句首段，给模型页面在讲什么的锚点（不重复时）
    head = paras[0][:300]
    if head and head not in body:
        body = head + "\n……\n" + body
    return body[:budget]


class Browser:
    """管理一个可远程调试的 Edge 实例。"""

    def __init__(
        self,
        *,
        edge_path: str = "",
        debug_port: int = 9222,
        profile_dir: str = "",
        headless: bool = False,
        search_url: str = DEFAULT_SEARCH_URL,
        page_timeout: float = 25.0,
    ) -> None:
        self.edge_path = edge_path or find_edge()
        self.debug_port = int(debug_port)
        self.profile_dir = Path(profile_dir)
        self.headless = bool(headless)
        self.search_url = search_url or DEFAULT_SEARCH_URL
        self.page_timeout = float(page_timeout)
        self._proc: subprocess.Popen | None = None
        self._launch_lock = asyncio.Lock()
        # 我们自己那个标签页的 target id，用于跨多次调用复用同一个标签页
        self._tab_id: str | None = None

    # ---- 与 devtools 端口通信 ----

    async def _http(self, method: str, path: str, timeout: float = 10.0) -> Any:
        url = f"http://127.0.0.1:{self.debug_port}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                return await resp.json(content_type=None)

    async def _alive(self) -> bool:
        try:
            info = await self._http("GET", "/json/version", timeout=3)
        except Exception:  # noqa: BLE001
            return False
        return bool(info.get("webSocketDebuggerUrl"))

    # ---- 生命周期 ----

    async def ensure_started(self) -> None:
        """确保有一个带调试端口的 Edge 在跑；没跑就拉起来。"""
        async with self._launch_lock:
            if await self._alive():
                return
            if not self.edge_path or not Path(self.edge_path).is_file():
                raise RuntimeError(
                    "找不到 Edge 可执行文件。请在插件配置里填写 edge_path，"
                    "例如 C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
                )
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            args = [
                self.edge_path,
                f"--remote-debugging-port={self.debug_port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--window-size=1280,900",
            ]
            if self.headless:
                args.append("--headless=new")
            args.append("about:blank")

            logger.info(f"[toolbox] 启动 Edge: 端口={self.debug_port} 无头={self.headless}")
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(60):
                if await self._alive():
                    logger.info("[toolbox] Edge 调试端口就绪")
                    return
                await asyncio.sleep(0.3)
            self.shutdown()
            raise RuntimeError(
                f"Edge 已启动但调试端口 {self.debug_port} 一直没就绪。"
                "可能是该端口被占用，或这个 Edge 配置目录已被别的实例打开。"
            )

    def shutdown(self) -> None:
        """只回收本模块自己拉起的那个 Edge 进程。"""
        self._tab_id = None
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            logger.info("[toolbox] 已回收插件拉起的 Edge 进程")

    async def _work_tab(self) -> dict:
        """复用我们自己那一个标签页。

        不能盲取 /json/list 的第一个 page —— Edge 会自己弹页面
        （实测出现过 edge://sync-confirmation-dialog），那时"第一个"就漂移了，
        每轮都会把上一个标签页留成孤儿。所以这里按 target id 认人。
        """
        targets = await self._http("GET", "/json/list")
        pages = [
            t
            for t in targets
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
        ]

        if self._tab_id:
            for page in pages:
                if page.get("id") == self._tab_id:
                    return page
            logger.info("[toolbox] 之前那个标签页没了，另开一个")
            self._tab_id = None

        if pages:
            self._tab_id = pages[0].get("id")
            return pages[0]

        new_tab = await self._http("PUT", "/json/new?about:blank")
        self._tab_id = new_tab.get("id")
        return new_tab

    async def _open_and_extract(
        self,
        url: str,
        ready_expr: str,
        extract_expr: str,
        *,
        settle: float = 0.0,
    ) -> Any:
        """导航到 url，等 ready_expr 成立，然后在页面里执行 extract_expr。"""
        await self.ensure_started()
        tab = await self._work_tab()
        async with _CdpTab(tab["webSocketDebuggerUrl"]) as cdp:
            await cdp.send("Page.enable")
            # ★ 拦掉图片/字体/媒体：搜索页和正文页都不需要它们，
            #   少下几百 KB 能明显缩短导航时间（实测 30%~50%）。
            try:
                await cdp.send("Network.enable")
                await cdp.send("Network.setBlockedURLs", {"urls": BLOCK_URL_PATTERNS})
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[toolbox] 资源拦截设置失败（忽略）: {exc}")
            await cdp.send("Page.navigate", {"url": url})

            deadline = time.monotonic() + self.page_timeout
            last_error = ""
            while time.monotonic() < deadline:
                try:
                    if await cdp.eval(ready_expr, timeout=10):
                        break
                except Exception as exc:  # noqa: BLE001
                    # 导航过程中执行表达式会短暂报错，属正常，记下最后一次继续等
                    last_error = str(exc)
                await asyncio.sleep(0.4)
            else:
                raise TimeoutError(
                    f"等待页面就绪超时（{self.page_timeout:.0f}s）: {url}"
                    + (f" · 最后错误: {last_error}" if last_error else "")
                )

            if settle > 0:
                # ★ 自适应等待：原先固定 sleep(settle)，多数页面早就稳了还在干等。
                #   现在每 0.2s 看一次正文长度，连续两次不变就继续，最多等 settle。
                deadline_settle = time.monotonic() + max(float(settle), 0.3)
                last_len = -1
                stable = 0
                while time.monotonic() < deadline_settle:
                    try:
                        cur_len = await cdp.eval(
                            "(document.body ? document.body.innerText.length : 0)",
                            timeout=5,
                        )
                    except Exception:  # noqa: BLE001
                        cur_len = -1
                    if cur_len == last_len and cur_len > 0:
                        stable += 1
                        if stable >= 2:
                            break
                    else:
                        stable = 0
                    last_len = cur_len
                    await asyncio.sleep(0.2)
            return await cdp.eval(extract_expr, timeout=30)

    # ---- 对外能力 ----

    # Bing 的时间过滤用 `qft=+filterui:age-lt<分钟>`。
    # ★ 这是 Bing 结果页自己的参数（不是编的）：+filterui:age-lt1440 就是只要一天内的。
    TIME_FILTER_MINUTES = {"day": 1440, "week": 10080, "month": 43200}

    async def search(
        self,
        query: str,
        count: int = 8,
        time_filter: str = "",
        page: int = 1,
    ) -> list[dict]:
        """跑一次搜索引擎，返回 [{title, url, snippet}]。

        time_filter: ""/day/week/month。只对 Bing 生效（别的引擎不认识这个参数）。
        page: 第几页（Bing 用 first=11 表示第二页）。
        """
        from urllib.parse import quote

        url = self.search_url.format(query=quote(query))
        extra: list[str] = []
        minutes = self.TIME_FILTER_MINUTES.get(str(time_filter or "").strip().lower())
        if minutes:
            extra.append(f"qft=+filterui:age-lt{minutes}")
        if page > 1:
            extra.append(f"first={(page - 1) * 10 + 1}")
        # 中文优先：让 Bing 按简体中文界面/地区给结果，少点英文噪声。
        # 只在真是 Bing 的时候加 —— 用户可能把 search_url 换成别的引擎。
        if "bing.com" in url:
            extra.extend(["setlang=zh-CN", "cc=CN"])
        if extra:
            url += ("&" if "?" in url else "?") + "&".join(extra)

        raw = await self._open_and_extract(url, BING_READY, BING_EXTRACT)
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        items = data.get("items") or []
        logger.info(
            f"[toolbox] 搜索 {query!r} → 页面命中 {len(items)} 条，"
            f"取前 {min(count, len(items))} 条 · 标题={data.get('title')!r}"
            + (f" · 时间过滤={time_filter}" if minutes else "")
            + (f" · 第 {page} 页" if page > 1 else "")
        )
        return items[: max(1, count)]

    async def fetch_text(
        self, url: str, max_chars: int = 4000, keywords: list[str] | None = None
    ) -> dict:
        """打开一个网址，取渲染后的正文。"""
        raw = await self._open_and_extract(
            url,
            MAIN_TEXT_READY,
            MAIN_TEXT_EXTRACT,
            settle=1.5,
        )
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        text = (data.get("text") or "").strip()
        status = int(data.get("status") or 0)
        total_chars = len(text)
        if keywords:
            text = keyword_windows(text, keywords, max_chars)
        truncated = len(text) > max_chars

        # 4xx/5xx 的页面也有正文（"403 - Access Denied" 之类）。
        # 不区分就会把错误页当成资料交给模型 —— 那比抓不到更糟。
        if status >= 400:
            logger.warning(
                f"[toolbox] 抓正文 {url} → HTTP {status}，这是错误页不是正文"
            )
            return {
                "url": data.get("url") or url,
                "title": data.get("title") or "",
                "status": status,
                "text": "",
                "truncated": False,
                "total_chars": 0,
                "note": f"该网址返回 HTTP {status}，没有可用的正文内容。",
            }

        logger.info(
            f"[toolbox] 抓正文 {url} → HTTP {status or '?'} · {len(text)} 字"
            + (f"，截断到 {max_chars}" if truncated else "")
        )
        return {
            "title": data.get("title") or "",
            "url": data.get("url") or url,
            "status": status,
            "text": text[:max_chars],
            "truncated": truncated,
            "total_chars": total_chars,
        }
