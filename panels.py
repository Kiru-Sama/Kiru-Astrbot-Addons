# -*- coding: utf-8 -*-
"""手机 AstrBot 面板聚合入口（自带反向代理）。

双击「打开面板.bat」→ 浏览器打开 http://127.0.0.1:9001
一页看全：监控 / 出图额度 / 提示注入 / 记忆 / 防睡眠 / NapCat WebUI

为什么要反向代理（2026-10-30 踩坑）：
    最初 iframe 直接指向 http://192.168.137.71:6185，Python 能拉、curl 能拉，
    但 Chrome 里一直「拒绝连接」——这是浏览器的私有网络访问限制/扩展拦截。
    现在页面里所有 iframe 都指向 /p/… 和 /n/…，由本服务代转，
    浏览器只跟 127.0.0.1:9001 打交道，问题消失。

路由：
    /               聚合页
    /p/<rest>       → 手机 AstrBot   http://<phone>:6185/<rest>
    /n/<rest>       → 手机 NapCat    http://<phone>:6099/<rest>
    /<其它绝对路径>  → 优先 AstrBot（面板里的 /api/... 这类绝对引用）
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

# ── 电脑防睡眠（本机）────────────────────────────────────────────────
class _PCAwake:
    """本机 Windows 防睡眠：只改本进程的执行状态请求，不碰注册表/电源计划。

    与手机端 astrbot_plugin_ncd_awake 的区别：
      · 那个跑在 AstrBot（手机 Linux）里，只能管手机 —— 而且 Linux 下直接不支持；
      · 这个跑在面板进程（Windows）里，管的是这台电脑，出图/拉起 ComfyUI 时用得上。
    """

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active = False
        self._display = False
        self._since = 0.0
        self._error = ""

    def status(self) -> dict:
        t = self._thread
        if self._active and (t is None or not t.is_alive()):
            self._active = False
            self._error = self._error or "保持线程已结束"
        return {
            "active": self._active,
            "display": self._display,
            "since": self._since,
            "error": self._error,
            "platform": sys.platform,
        }

    def start(self, display: bool) -> tuple[bool, str]:
        if sys.platform != "win32":
            return False, f"只支持 Windows，当前是 {sys.platform}"
        with self._lock:
            if self._active and self._thread and self._thread.is_alive() and self._display == bool(display):
                return True, ""
            self._stop.set()
            old = self._thread
        if old is not None and old.is_alive():
            old.join(timeout=3.0)
        with self._lock:
            stop = threading.Event()
            self._stop = stop
            self._display = bool(display)
            self._active = True
            self._since = time.time()
            self._error = ""
            th = threading.Thread(target=self._loop, args=(stop,), daemon=True)
            self._thread = th
            th.start()
        return True, ""

    def _loop(self, stop: threading.Event) -> None:
        flags = self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED
        if self._display:
            flags |= self.ES_DISPLAY_REQUIRED
        k32 = ctypes.windll.kernel32
        try:
            while not stop.is_set():
                k32.SetThreadExecutionState(ctypes.c_uint(flags))
                stop.wait(30.0)
        finally:
            k32.SetThreadExecutionState(ctypes.c_uint(self.ES_CONTINUOUS))
            with self._lock:
                self._active = False

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=3.0)
        with self._lock:
            self._active = False


_awake = _PCAwake()


def build_awake_html(phone_path: str) -> str:
    """防睡眠页：电脑卡（可操作）+ 手机卡（原插件页，只读嵌入）。"""
    phone = (
        f'<iframe class="frame" src="/p{phone_path}"></iframe>'
        if phone_path and not phone_path.startswith("__ERR__")
        else '<div class="muted">手机端原页面暂不可用（插件在 Linux 上本来就不支持保持唤醒）。</div>'
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>防睡眠</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ margin:0; padding:18px; font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif;
        background:#f6f7f9; color:#1c1d21; }}
 @media (prefers-color-scheme: dark) {{ body {{ background:#16181c; color:#e6e7ea; }} }}
 .card {{ background:#fff; border:1px solid #e3e5e9; border-radius:12px; padding:16px 18px; margin-bottom:14px; }}
 @media (prefers-color-scheme: dark) {{ .card {{ background:#1e2127; border-color:#2b2f36; }} }}
 h2 {{ font-size:15px; margin:0 0 6px; }}
 .row {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:10px; }}
 .dot {{ width:9px; height:9px; border-radius:50%; background:#bbb; display:inline-block; }}
 .on {{ background:#22c55e; }} .off {{ background:#9ca3af; }}
 button {{ padding:7px 14px; border-radius:8px; border:1px solid #cfd3da; background:#fff; cursor:pointer; font-size:13px; }}
 @media (prefers-color-scheme: dark) {{ button {{ background:#262a31; border-color:#39404a; color:#e6e7ea; }} }}
 button.primary {{ background:#2563eb; border-color:#2563eb; color:#fff; }}
 button:disabled {{ opacity:.5; cursor:default; }}
 .muted {{ color:#8a8f98; font-size:12.5px; }}
 iframe.frame {{ width:100%; height:420px; border:1px solid #e3e5e9; border-radius:10px; margin-top:12px; background:#fff; }}
 code {{ background:#00000010; padding:1px 5px; border-radius:4px; }}
</style></head><body>
<div class="card">
  <h2>电脑保持唤醒 <span id="badge" class="muted">（读取中…）</span></h2>
  <div class="muted">只对面板进程生效：阻止 Windows 因空闲睡眠/休眠；勾上「连屏幕」可顺带阻止熄屏。
    关掉面板或点关闭即还原，不写注册表、不改电源计划。</div>
  <div class="row">
    <label><input type="checkbox" id="disp"> 连屏幕一起保持</label>
    <button class="primary" id="btn-on">开启保持</button>
    <button id="btn-off">关闭</button>
    <span class="muted" id="info"></span>
  </div>
</div>
<div class="card">
  <h2>手机（bot 所在）</h2>
  <div class="muted">这项由 AstrBot 插件 astrbot_plugin_ncd_awake 提供，跑在手机 Linux 上，系统不支持保持唤醒，仅供参照。</div>
  {phone}
</div>
<script>
async function j(u, opt) {{ const r = await fetch(u, opt); return await r.json(); }}
async function refresh() {{
  const s = await j('/awake/status');
  const b = document.getElementById('badge');
  if (s.active) {{
    b.innerHTML = '<span class="dot on"></span> 保持中' + (s.display ? '（含屏幕）' : '');
    b.title = '自 ' + new Date(s.since * 1000).toLocaleTimeString();
    document.getElementById('info').textContent = '自 ' + new Date(s.since * 1000).toLocaleTimeString() + ' 起';
  }} else {{
    b.innerHTML = '<span class="dot off"></span> 未开启';
    document.getElementById('info').textContent = s.error || '';
  }}
  document.getElementById('disp').checked = !!s.display;
}}
document.getElementById('btn-on').onclick = async () => {{
  const disp = document.getElementById('disp').checked;
  const r = await j('/awake/on', {{method:'POST', headers:{{'Content-Type':'application/json'}},
                                 body: JSON.stringify({{display: disp}})}});
  if (!r.ok) alert(r.error || '开启失败');
  refresh();
}};
document.getElementById('btn-off').onclick = async () => {{ await j('/awake/off', {{method:'POST'}}); refresh(); }};
refresh(); setInterval(refresh, 5000);
</script></body></html>"""


# ── 配置 ────────────────────────────────────────────────────────────
ADB = r"E:\软件和游戏\MuMuPlayer\nx_device\15.0\shell\adb.exe"
SERIAL = "STS0219701030196"
LOCAL_PORT = 16185          # 电脑侧 adb 转发端口
REMOTE_PORT = 6185          # 手机 AstrBot WebUI

PHONE_IP = "192.168.137.71"
NAPCAT_IP = "192.168.137.71"

# 手机端 dashboard 已改成 0.0.0.0，优先走热点；连不上才退回 USB 转发
ASTRBOT_CANDIDATES = [
    f"http://{PHONE_IP}:6185",
    f"http://127.0.0.1:{LOCAL_PORT}",
]
ASTRBOT = ASTRBOT_CANDIDATES[0]
NAPCAT = f"http://{NAPCAT_IP}:6099"
PC_API = "http://192.168.137.1:8201"      # 电脑侧 linkd（/sysinfo 提供电脑资源；8200 拉起器已退役）
USERNAME = "astrbot"
PASSWORD = "NcdPass2026"
SERVE_PORT = 9001
NAPCAT_PORT = 9002          # NapCat 单独一个端口，避免和 AstrBot 的 /api/ 路径打架
ASTRBOT_PORT = 9003         # AstrBot Dashboard 免登录反代（注入 token 后同源加载）

PAGES = [
    ("监控面板", "astrbot_plugin_ncd_monitor", "monitor"),
    ("出图额度", "astrbot_plugin_ncd_comfy", "quota"),
    ("提示注入", "astrbot_plugin_ncd_inject", "inject"),
    ("记忆", "astrbot_plugin_ncd_memory", "memory"),
    ("防睡眠", "astrbot_plugin_ncd_awake", "awake"),
    ("反馈", "astrbot_plugin_ncd_feedback", "feedback"),
]

# 失败结果只缓存 15 秒：服务短暂抖动后面板能自己恢复，而不是钉住 5 分钟
FAIL_CACHE_SEC = 15.0
_cache: dict = {"at": 0.0, "token": "", "urls": {}, "ok": True}
_lock = threading.Lock()
_napcat_token: str = ""

_FORWARD_HEADERS = ("authorization", "accept", "content-type", "cookie", "user-agent", "x-token")

# 插进聚合页的桥接脚本：代替 AstrBot 仪表盘来响应插件页的 apiGet 请求。
# 插件页通过 postMessage 向父窗口要数据（不是自己 fetch），父窗口必须回话，否则永远「加载中」。
BRIDGE_JS = r"""
const CHANNEL = "astrbot-plugin-page";
const allFrames = [...document.querySelectorAll('iframe')];
function frameFor(win) {
  return allFrames.find(f => f.contentWindow === win);
}
window.addEventListener("message", async (ev) => {
  const d = ev.data;
  if (!d || d.channel !== CHANNEL) return;
  const frame = frameFor(ev.source);
  if (!frame || !frame.dataset.plugin) return;
  const plugin = frame.dataset.plugin;
  const reply = (payload) => {
    try { ev.source.postMessage({ channel: CHANNEL, ...payload }, location.origin); } catch (e) {}
  };
  if (d.kind !== "request") return;
  const requestId = d.requestId;
  try {
    let url = "/api/plug/" + plugin + d.endpoint;
    const qs = new URLSearchParams(d.params || {}).toString();
    if (qs) url += (url.includes("?") ? "&" : "?") + qs;
    const init = { headers: {} };
    if (d.action === "api:post") {
      init.method = "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(d.body !== undefined ? d.body : {});
    } else if (d.action !== "api:get") {
      throw new Error("unsupported action " + d.action);
    }
    const r = await fetch(url, init);
    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch (e) { data = text; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    reply({ kind: "response", requestId, ok: true, data });
  } catch (err) {
    reply({ kind: "response", requestId, ok: false, error: String((err && err.message) || err) });
  }
});
"""


def _auth_header() -> dict:
    """给 AstrBot 的 /api/ 请求带上 JWT（插件页数据接口要 Bearer）。"""
    token = _cache.get("token") or ""
    return {"Authorization": f"Bearer {token}"} if token else {}


# ── 工具 ────────────────────────────────────────────────────────────
def _req(url: str, data: dict | None = None, token: str = "", timeout: float = 20.0):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ensure_forward() -> str:
    """建 adb 转发（仅在需要走 USB 兜底时才用），已存在就不动。"""
    if not os.path.isfile(ADB):
        return "adb 不存在"
    want = f"tcp:{LOCAL_PORT} tcp:{REMOTE_PORT}"
    try:
        out = subprocess.run([ADB, "-s", SERIAL, "forward", "--list"],
                             capture_output=True, timeout=20)
        if want in (out.stdout or b"").decode("utf-8", "replace"):
            return "ok（转发已存在）"
        subprocess.run([ADB, "-s", SERIAL, "forward",
                        f"tcp:{LOCAL_PORT}", f"tcp:{REMOTE_PORT}"],
                       capture_output=True, timeout=20)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"adb forward 失败: {exc}"


def _phone_ips() -> list[str]:
    """问 adb 要手机当前 wlan0 的 IP。

    ★ 热点 IP 是 DHCP 给的，重启后可能从 .71 变成 .72。
      写死一个 IP 迟早会失效，所以每次登录都实际问一下。
    """
    if not os.path.isfile(ADB):
        return []
    try:
        out = subprocess.run(
            [ADB, "-s", SERIAL, "shell", "ip", "-4", "addr", "show", "wlan0"],
            capture_output=True, timeout=15,
        )
        txt = (out.stdout or b"").decode("utf-8", "replace")
        ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", txt)
        return [ip for ip in ips if not ip.startswith("127.")]
    except Exception:  # noqa: BLE001
        return []


def login() -> str:
    """登录并锁定可用的 AstrBot 地址。

    顺序：写死的热点 IP → adb 查到的当前 wlan0 IP → USB 转发兜底。
    """
    global ASTRBOT
    cands: list[str] = [f"http://{PHONE_IP}:6185"]
    for ip in _phone_ips():
        u = f"http://{ip}:{REMOTE_PORT}"
        if u not in cands:
            cands.append(u)
    cands.append(f"http://127.0.0.1:{LOCAL_PORT}")
    last: Exception | None = None
    for base in cands:
        try:
            r = _req(f"{base}/api/auth/login",
                     {"username": USERNAME, "password": PASSWORD}, timeout=8)
            ASTRBOT = base
            return r["data"]["token"]
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last if last else RuntimeError("没有可用的 AstrBot 地址")


def page_path(token: str, plugin: str, view: str) -> str:
    """取插件页的 content_path（/api/v1/plugins/... 这种绝对路径）。"""
    q = urlencode({"name": plugin, "page": view})
    r = _req(f"{ASTRBOT}/api/plugin/page/entry?{q}", token=token)
    return r["data"]["content_path"]


def refresh(force: bool = False) -> dict:
    with _lock:
        ttl = 300.0 if _cache.get("ok", True) else FAIL_CACHE_SEC
        if not force and _cache["urls"] and time.time() - _cache["at"] < ttl:
            return _cache
        token = login()
        paths = {}
        for label, plugin, view in PAGES:
            try:
                paths[label] = page_path(token, plugin, view)
            except Exception as exc:  # noqa: BLE001
                paths[label] = f"__ERR__{type(exc).__name__}: {exc}"
        bad = any(str(v).startswith("__ERR__") for v in paths.values())
        _cache.update({"at": time.time(), "token": token, "urls": paths, "ok": not bad})
        return _cache


# ── 页面 ────────────────────────────────────────────────────────────
def build_html() -> str:
    try:
        data = refresh()
        paths = data["urls"]
        ok, err = True, ""
    except Exception as exc:  # noqa: BLE001
        paths, ok, err = {}, False, f"{type(exc).__name__}: {exc}"

    tabs, frames = [], []
    for i, (label, _p, _v) in enumerate(PAGES):
        path = paths.get(label, "")
        bad = not path or path.startswith("__ERR__")
        cls = "tab active" if i == 0 else "tab"
        if bad:
            cls += " bad"
        tabs.append(f'<button class="{cls}" data-i="{i}">{label}{" ⚠" if bad else ""}</button>')
        if bad:
            frames.append(f'<div class="frame" data-i="{i}"><div class="err">'
                          f'<b>{label}</b> 取不到页面<br><code>{path or err}</code></div></div>')
        elif label == "防睡眠":
            frames.append(
                f'<iframe class="frame" data-i="{i}" data-plugin="__local__" '
                f'data-src="/awake?p={path}"></iframe>'
            )
        else:
            # ★ 走本地代理，浏览器不直接连手机 IP；带上插件 id 供桥接用。
            #   data-src 而不是 src：懒加载，切到该标签才真正拉页面，
            #   避免一打开就 5 个面板同时轮询接口（监控面板轮询很勤）。
            frames.append(
                f'<iframe class="frame" data-i="{i}" data-plugin="{_p}" '
                f'data-src="/p{path}"></iframe>'
            )

    idx = len(PAGES)
    tabs.append(f'<button class="tab" data-i="{idx}">NapCat</button>')
    # NapCat WebUI 是 basename=/webui/ 的 React 应用，且要独占一个源（它自己也调 /api/）。
    # 9002 根路径会先写 localStorage 的 token 再跳 /webui/，所以这里指根路径 = 免登录。
    frames.append(
        f'<iframe class="frame" data-i="{idx}" '
        f'data-src="http://127.0.0.1:{NAPCAT_PORT}/"></iframe>'
    )

    idx = len(PAGES) + 1
    tabs.append(f'<button class="tab" data-i="{idx}">AstrBot 后台</button>')
    # 9003 反代：注入 token 后同源加载，打开即已登录（不用再输账号密码）
    frames.append(
        f'<iframe class="frame" data-i="{idx}" '
        f'data-src="http://127.0.0.1:{ASTRBOT_PORT}/"></iframe>'
    )

    mode = "热点直连" if "192.168" in ASTRBOT else "USB 转发"
    status = (f"已就绪 · {mode} {ASTRBOT} · {time.strftime('%H:%M:%S')}"
              if ok else f"<span style='color:#f88'>登录失败：{err}</span>")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>手机 AstrBot 面板</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin:0; height:100%; background:#0f1115; color:#dfe3ea;
                font: 13px/1.5 system-ui, "Microsoft YaHei", sans-serif; }}
  header {{ display:flex; align-items:center; gap:10px; padding:8px 12px;
            background:#161a22; border-bottom:1px solid #262c38; }}
  .brand {{ font-weight:600; letter-spacing:.3px; }}
  .status {{ color:#7c8798; font-size:12px; margin-left:auto; }}
  .pc {{ font-size:12px; color:#9fb2cc; background:#1a2330; border:1px solid #2a3648;
         border-radius:6px; padding:2px 9px; margin-left:10px; white-space:nowrap; }}
  .pc.dim {{ color:#6b7688; }}
  .fresh {{ background:#23324a; color:#8fb6ff; border:1px solid #2f4468;
            border-radius:6px; padding:3px 10px; cursor:pointer; font-size:12px; }}
  .tabs {{ display:flex; gap:4px; padding:8px 12px 0; background:#12161d;
           border-bottom:1px solid #262c38; overflow-x:auto; }}
  .tab {{ background:transparent; color:#98a3b3; border:1px solid transparent;
          border-bottom:none; padding:7px 14px; border-radius:8px 8px 0 0;
          cursor:pointer; font-size:13px; white-space:nowrap; }}
  .tab:hover {{ color:#dfe3ea; }}
  .tab.active {{ background:#0f1115; color:#7fb2ff; border-color:#262c38;
                 position:relative; top:1px; }}
  .tab.bad {{ color:#e88; }}
  main {{ height: calc(100% - 96px); }}
  .frame {{ width:100%; height:100%; border:0; background:#0f1115; display:none; }}
  .frame.active {{ display:block; }}
  .err {{ padding:40px; color:#f88; }}
  .err code {{ color:#fb0; word-break:break-all; }}
</style></head>
<body>
<header>
  <span class="brand">手机 AstrBot 面板</span>
  <span class="pc" id="pcinfo">电脑 …</span>
  <span class="status">{status}</span>
  <button class="fresh" onclick="location.reload()">刷新</button>
</header>
<div class="tabs">{''.join(tabs)}</div>
<main>{''.join(frames)}</main>
<script>
  const panes = [...document.querySelectorAll('.frame')];
  const tabButtons = [...document.querySelectorAll('.tab')];
  const LAZY = true;
  function activate(i){{
    tabButtons.forEach(t => t.classList.toggle('active', +t.dataset.i === i));
    panes.forEach(f => {{
      const on = +f.dataset.i === i;
      f.classList.toggle('active', on);
      // 懒加载：第一次切到才真正请求页面；之后保留，切回来不用重载
      if (on && f.dataset.src && f.dataset.loaded !== '1') {{
        f.dataset.loaded = '1';
        f.src = f.dataset.src;
      }}
    }});
    try {{ localStorage.setItem('astrbot-panel-tab', String(i)); }} catch (e) {{}}
  }}
  tabButtons.forEach(t => t.onclick = () => activate(+t.dataset.i));
  // ── 电脑资源条：每 3 秒问一次拉起器（经本地代理，浏览器不碰 192.168.x）
  const pcEl = document.getElementById('pcinfo');
  async function updPC() {{
    try {{
      const r = await fetch('/pc/sysinfo', {{cache: 'no-store'}});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      const bits = [];
      if (d.cpu !== undefined) bits.push('CPU ' + d.cpu.toFixed(0) + '%');
      if (d.gpu) {{
        bits.push('GPU ' + d.gpu.util.toFixed(0) + '%');
        bits.push('显存 ' + (d.gpu.mem_used_mb / 1024).toFixed(1) + '/' +
                  (d.gpu.mem_total_mb / 1024).toFixed(1) + 'G');
      }}
      if (d.ram_pct !== undefined) {{
        bits.push('内存 ' + d.ram_used_gb.toFixed(1) + '/' + d.ram_total_gb.toFixed(1) + 'G');
      }}
      pcEl.textContent = '💻 ' + bits.join(' · ');
      pcEl.classList.remove('dim');
    }} catch (e) {{
      pcEl.textContent = '💻 电脑离线';
      pcEl.classList.add('dim');
    }}
  }}
  updPC();
  setInterval(updPC, 3000);
  let start = 0;
  try {{
    const saved = parseInt(localStorage.getItem('astrbot-panel-tab') || '0', 10);
    if (!isNaN(saved) && saved >= 0 && saved < panes.length) start = saved;
  }} catch (e) {{}}
  activate(start);
  {BRIDGE_JS}
</script>
</body></html>"""


# ── HTTP ────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _proxy(self, base: str, subpath: str) -> None:
        url = base.rstrip("/") + subpath
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in _FORWARD_HEADERS:
                headers[k] = v
        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._send(r.status, r.headers.get("Content-Type", ""), r.read())
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.headers.get("Content-Type", ""), exc.read())
        except Exception as exc:  # noqa: BLE001
            msg = f"代理失败: {type(exc).__name__}: {exc}\n目标: {url}".encode("utf-8")
            self._send(502, "text/plain; charset=utf-8", msg)

    def _relay(self, raw: str) -> None:
        """兜底：先问 AstrBot，404 再问 NapCat（覆盖两边的绝对资源路径）。"""
        for base in (ASTRBOT, NAPCAT):
            url = base.rstrip("/") + raw
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() in _FORWARD_HEADERS}
            # ★ 插件页的数据接口（/api/plug/...）要 Bearer JWT，浏览器不带，由我们补
            if base is ASTRBOT and raw.startswith("/api/"):
                headers.setdefault("Authorization", _auth_header().get("Authorization", ""))
                headers = {k: v for k, v in headers.items() if v}
            req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self._send(r.status, r.headers.get("Content-Type", ""), r.read())
                    return
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and base is ASTRBOT:
                    continue          # 换 NapCat 再试
                self._send(exc.code, exc.headers.get("Content-Type", ""), exc.read())
                return
            except Exception as exc:  # noqa: BLE001
                self._send(502, "text/plain; charset=utf-8",
                           f"代理失败: {type(exc).__name__}: {exc}\n目标: {url}".encode("utf-8"))
                return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_GET(self) -> None:
        raw = self.path
        path, _, query = raw.partition("?")
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", build_html().encode("utf-8"))
        elif path == "/awake":
            q = query
            phone_path = ""
            if "p=" in q:
                from urllib.parse import unquote
                phone_path = unquote(q.split("p=", 1)[1])
            self._send(200, "text/html; charset=utf-8", build_awake_html(phone_path).encode("utf-8"))
        elif path == "/awake/status":
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(_awake.status(), ensure_ascii=False).encode("utf-8"))
        elif path == "/pc/sysinfo":
            # 电脑资源：代理到拉起器（同源，浏览器不用碰 192.168.x）
            self._proxy(PC_API, "/sysinfo")
        elif path.startswith("/p/"):
            self._proxy(ASTRBOT, path[2:] + (("?" + query) if query else ""))
        elif path.startswith("/n/"):
            tail = path[2:] or "/"
            self._proxy(NAPCAT, tail + (("?" + query) if query else ""))
        elif path.startswith("/webui/"):
            self._proxy(NAPCAT, raw)      # NapCat 的静态资源
        else:
            self._relay(raw)              # 面板里的绝对引用（/api/... 等）

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/awake/on", "/awake/off"):
            raw = b""
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n > 0 else b""
            except Exception:
                raw = b""
            if path == "/awake/on":
                display = False
                try:
                    display = bool((json.loads(raw or b"{}") or {}).get("display"))
                except Exception:
                    display = False
                ok, err = _awake.start(display)
            else:
                _awake.stop()
                ok, err = True, ""
            body = json.dumps({"ok": ok, "error": err, **_awake.status()}, ensure_ascii=False)
            self._send(200, "application/json; charset=utf-8", body.encode("utf-8"))
        elif path.startswith("/p/"):
            self._proxy(ASTRBOT, self.path[2:])
        elif path.startswith("/n/"):
            self._proxy(NAPCAT, self.path[2:])
        elif path.startswith("/webui/"):
            self._proxy(NAPCAT, self.path)
        else:
            self._relay(self.path)

    do_PUT = do_POST
    do_DELETE = do_POST

    def do_HEAD(self) -> None:
        self.do_GET()

    def log_message(self, *args):  # noqa: ANN001
        pass


def _read_napcat_token() -> str:
    """NapCat WebUI 登录 token。

    优先级：环境变量 NAPCAT_TOKEN > 同目录 napcat_token.txt > 内置默认值。
    （容器里的 /root 是 proot 路径，adb shell 读不到，所以不做远程实时读。）
    换 token 时改 napcat_token.txt 即可，不用动代码。
    """
    global _napcat_token
    if _napcat_token:
        return _napcat_token
    tok = os.environ.get("NAPCAT_TOKEN", "").strip()
    if not tok:
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "napcat_token.txt")
        try:
            with open(f, encoding="utf-8") as fh:
                tok = fh.read().strip()
        except OSError:
            tok = ""
    if not tok:
        tok = "c6ae1d43"          # 2026-10-31 扫码重登后轮换的新值（旧 9b13cf1e 已失效）
    _napcat_token = tok
    return tok


def _napcat_bootstrap() -> bytes:
    """NapCat 自动登录跳板。

    NapCat 前端不是直接用 webui.json 里的 token，而是：
        hash = SHA256(token + ".napcat")
        POST /api/auth/login {hash}  ->  {data:{Credential: "..."}}
        localStorage["token"] = JSON.stringify(Credential)
        Authorization: Bearer ${JSON.parse(localStorage.token)}
    所以这里在浏览器里把这几步做掉，再跳 /webui/，实现「打开即已登录」。

    ★ 优化：先拿已有凭据调 /api/auth/check，通过了就直接进，
      只有失效才真的去登录 —— 否则每刷一次页面就登录一次，
      会撞上 NapCat 的 loginRate 限制（默认 3），表现为莫名其妙回到登录页。
    """
    tok = _read_napcat_token()
    body = f"""<!doctype html><meta charset="utf-8"><title>NapCat</title>
<body style="background:#0f1115;color:#dfe3ea;font:14px system-ui;padding:24px">
正在登录 NapCat…
<script>
(async () => {{
  const TOKEN = {json.dumps(tok)};
  const go = () => location.replace("/webui/");
  const authz = (c) => ({{ "Authorization": "Bearer " + c, "Content-Type": "application/json" }});
  try {{
    // ① 已有凭据还有效就直接进
    const saved = localStorage.getItem("token");
    if (saved) {{
      try {{
        const c0 = JSON.parse(saved);
        const r0 = await fetch("/api/auth/check", {{method: "POST", headers: authz(c0), body: "{{}}"}});
        const j0 = await r0.json();
        if (j0 && j0.code === 0) {{ go(); return; }}
      }} catch (e) {{}}
    }}
    // ② 失效/没有才换新的
    const enc = new TextEncoder().encode(TOKEN + ".napcat");
    const buf = await crypto.subtle.digest("SHA-256", enc);
    const hex = [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
    const r = await fetch("/api/auth/login", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{hash: hex}})
    }});
    const j = await r.json();
    const cred = j && j.data && j.data.Credential;
    if (cred) localStorage.setItem("token", JSON.stringify(cred));
  }} catch (e) {{}}
  go();
}})();
</script>
</body>
"""
    return body.encode("utf-8")


class NapcatHandler(BaseHTTPRequestHandler):
    """9002 端口：把 NapCat WebUI 原样搬过来（它自己的 /api/ 与 AstrBot 不冲突）。"""

    protocol_version = "HTTP/1.1"

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _go(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _napcat_bootstrap())
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() in _FORWARD_HEADERS}
        req = urllib.request.Request(NAPCAT + self.path, data=body, headers=headers,
                                     method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._send(r.status, r.headers.get("Content-Type", ""), r.read())
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.headers.get("Content-Type", ""), exc.read())
        except Exception as exc:  # noqa: BLE001
            self._send(502, "text/plain; charset=utf-8", f"napcat proxy: {exc}".encode("utf-8"))

    do_GET = _go
    do_POST = _go
    do_PUT = _go
    do_DELETE = _go
    do_HEAD = _go

    def log_message(self, *args):  # noqa: ANN001
        pass


class AstrbotHandler(BaseHTTPRequestHandler):
    """9003 端口：AstrBot Dashboard 反代 + 免登录注入。

    ★ 为什么不用 /astrbot/ 子路径：SPA 的资源是绝对路径（/assets/...），
      放在子路径下全部 404。独立端口 = 同源根路径，最省事。
    """

    protocol_version = "HTTP/1.1"

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _token(self) -> str:
        tok = _cache.get("token") or ""
        if tok:
            return tok
        try:
            return login()
        except Exception:  # noqa: BLE001
            return ""

    def _go(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            tok = self._token()
            try:
                with urllib.request.urlopen(ASTRBOT.rstrip("/") + "/", timeout=20) as r:
                    html = r.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                self._send(502, "text/plain; charset=utf-8",
                           f"AstrBot 取首页失败: {type(exc).__name__}: {exc}".encode("utf-8"))
                return
            inject = (
                "<script>try{localStorage.setItem('token',"
                + json.dumps(tok)
                + ")}catch(e){}</script>"
            )
            low = html.lower()
            pos = low.find("<head")
            if pos >= 0:
                end = low.find(">", pos)
                html = html[: end + 1] + inject + html[end + 1:]
            else:
                html = inject + html
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() in _FORWARD_HEADERS}
        # 浏览器没带 token 的接口请求，用我们缓存的补上（前端刚启动时可能还没读 localStorage）
        if not any(k.lower() == "authorization" for k in headers):
            auth = _auth_header().get("Authorization", "")
            if auth:
                headers["Authorization"] = auth
        req = urllib.request.Request(ASTRBOT.rstrip("/") + self.path, data=body,
                                     headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._send(r.status, r.headers.get("Content-Type", ""), r.read())
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.headers.get("Content-Type", ""), exc.read())
        except Exception as exc:  # noqa: BLE001
            self._send(502, "text/plain; charset=utf-8", f"astrbot proxy: {exc}".encode("utf-8"))

    do_GET = _go
    do_POST = _go
    do_PUT = _go
    do_DELETE = _go
    do_PATCH = _go
    do_HEAD = _go

    def log_message(self, *args):  # noqa: ANN001
        pass


def _serve_astrbot() -> None:
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", ASTRBOT_PORT), AstrbotHandler)
    except OSError as exc:
        print(f"[面板] AstrBot 反代端口 {ASTRBOT_PORT} 起不来: {exc}", flush=True)
        return
    print(f"[面板] AstrBot 后台（免登录）: http://127.0.0.1:{ASTRBOT_PORT}/", flush=True)
    srv.serve_forever()

def _serve_napcat() -> None:
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", NAPCAT_PORT), NapcatHandler)
    except OSError as exc:
        print(f"[面板] NapCat 代理端口 {NAPCAT_PORT} 起不来: {exc}", flush=True)
        return
    print(f"[面板] NapCat 代理: http://127.0.0.1:{NAPCAT_PORT}/webui/", flush=True)
    srv.serve_forever()


def main() -> None:
    # 开机自启用 --no-browser：只起服务，不弹浏览器窗口
    no_browser = "--no-browser" in sys.argv
    url = f"http://127.0.0.1:{SERVE_PORT}/"
    try:
        refresh(force=True)
        print(f"[面板] 已登录，使用 {ASTRBOT}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[面板] 登录失败（页面仍会打开）: {exc}", flush=True)
        ensure_forward()

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", SERVE_PORT), Handler)
    except OSError:
        print(f"[面板] {SERVE_PORT} 已在运行", flush=True)
        if not no_browser:
            webbrowser.open(url)
            time.sleep(1.0)
        return

    print(f"[面板] 服务已启动: {url}", flush=True)
    threading.Thread(target=_serve_napcat, daemon=True).start()
    threading.Thread(target=_serve_astrbot, daemon=True).start()
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[面板] 退出", flush=True)


if __name__ == "__main__":
    main()
