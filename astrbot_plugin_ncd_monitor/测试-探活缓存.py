# -*- coding: utf-8 -*-
"""验证探活缓存：面板每 5 秒刷一次，不该每次都穿透到 LM Studio / SnowLuma。

为什么要有这个文件：
    面板 index.html 里 `setInterval(tick, 5000)` 每 5 秒调一次 /overview，
    而 api_overview 会真去 HTTP 探一次 LM Studio 和 SnowLuma。
    实测一天打了 1.7 万次 `GET /v1/models`、7 MB 日志 —— 全是面板自己刷的，
    而且这层噪音差点让我把空回复的锅扣到别的程序头上。
    加缓存之后，缓存本身又带来四个【不报错、只是悄悄不对】的坑：
      1. 该穿透时不穿透（模型状态变了，面板一直显示旧的）
      2. 该命中时不命中（缓存白写，等于没加）
      3. 缓存对象被调用方改坏（第二次读出来是脏的）
      4. 不同探活互相串味（LM 的结果顶掉 SnowLuma 的）
    所以在这里钉死。六个用例，见下面各段的注释。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-探活缓存.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""
import asyncio
import importlib.util
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")


class StubContext:
    """顶掉 register_web_api，让插件能在没有 AstrBot 运行时的情况下实例化。"""

    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_probe_cache", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_probe_cache"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


# snowluma_dir 指向不存在的目录：探活会失败，但失败结果同样要缓存，
# 而且不能抛异常。这是【别人机器上】的常见状态。
CONF = {
    "lm_studio_base": "http://127.0.0.1:1234",
    "snowluma_base": "http://127.0.0.1:5099",
    "snowluma_dir": r"X:\nonexistent\SnowLuma",
}


class _FakeResp:
    status = 200

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


async def main() -> int:
    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    bad: list[str] = []
    calls: list[str] = []
    ttl_backup = getattr(mod, "_PROBE_CACHE_SECONDS", None)

    def fake_urlopen(url, timeout=None):  # noqa: ANN001, ARG001
        target = url if isinstance(url, str) else getattr(url, "full_url", str(url))
        calls.append(target)
        return _FakeResp(json.dumps({"data": [{"id": "fake-model"}]}).encode())

    # 只隔离真正的外部依赖（网络），不去断言内部调用了哪个私有方法。
    orig_urlopen = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = fake_urlopen

    # ★ 探活动作怎么认：LM Studio 现在会**优先**打原生 /api/v0/models
    #   （多给 state / loaded_context_length / capabilities），老版本才退回
    #   /v1/models。所以两个都算 LM。SnowLuma 那边不含 /models。
    #   原来只认 "/v1/models"，改走原生端点后这里会数到 0，
    #   而且会把 /api/v0/models 误判成 SnowLuma —— 正是这次踩到的坑。
    def lm_calls() -> int:
        return sum(1 for u in calls if "/api/v0/models" in u or "/v1/models" in u)

    def sl_calls() -> int:
        return sum(1 for u in calls if "/models" not in u)

    try:
        # ── 用例 1（负向对照）：TTL 归零时必须每次都穿透 ──────────────
        # 没有这条，「用例 2 只穿透一次」可能是假的：万一计数根本没接上，
        # 或者被别的什么东西挡掉了，断言会无条件变绿。这里先证明
        # 「计数能数到 2」，用例 2 的「只数到 1」才有意义。
        if ttl_backup is None:
            bad.append("用例1：模块里没有 _PROBE_CACHE_SECONDS，缓存时长无从配置")
        else:
            mod._PROBE_CACHE_SECONDS = 0.0
            calls.clear()
            inst1 = cls(StubContext(), dict(CONF))
            inst1._lm_studio_info()
            inst1._lm_studio_info()
            if lm_calls() != 2:
                bad.append(
                    f"用例1：TTL=0 时应每次都穿透，实际只探了 {lm_calls()} 次"
                    "（负向对照失败，后面的用例不可信）"
                )
            else:
                print("  用例1：TTL=0 → 2 次调用穿透 2 次（负向对照成立）")

        # ── 用例 2：TTL 内只穿透一次，且两次返回值一致 ─────────────────
        mod._PROBE_CACHE_SECONDS = 20.0
        calls.clear()
        inst2 = cls(StubContext(), dict(CONF))
        a = inst2._lm_studio_info()
        b = inst2._lm_studio_info()
        if lm_calls() != 1:
            bad.append(f"用例2：TTL 内应只探 1 次，实际 {lm_calls()} 次（缓存没生效）")
        elif a != b:
            bad.append(f"用例2：命中缓存时两次返回值不一致\n    {a}\n    {b}")
        else:
            print(f"  用例2：TTL 内 2 次调用只探 1 次，返回值一致（models={a.get('models')}）")

        # ── 用例 3：过了 TTL 必须重新穿透 ─────────────────────────────
        # 缓存太"粘"会让面板永远显示「LM Studio 不可达」，比不缓存更坏。
        mod._PROBE_CACHE_SECONDS = 0.2
        calls.clear()
        inst3 = cls(StubContext(), dict(CONF))
        inst3._lm_studio_info()
        time.sleep(0.35)
        inst3._lm_studio_info()
        if lm_calls() != 2:
            bad.append(f"用例3：过期后应重新探，实际共探了 {lm_calls()} 次")
        else:
            print("  用例3：过了 TTL 重新穿透（缓存会失效）")

        # ── 用例 4：两个探活各存各的，不能串味 ────────────────────────
        # 共用一个 key 的话，SnowLuma 会读到 LM Studio 的结果 —— 面板上
        # 就会显示成「SnowLuma 显示了一堆模型名」。
        mod._PROBE_CACHE_SECONDS = 20.0
        calls.clear()
        inst4 = cls(StubContext(), dict(CONF))
        inst4._lm_studio_info()
        inst4._lm_studio_info()
        inst4._snowluma_info()
        inst4._snowluma_info()
        if lm_calls() != 1 or sl_calls() != 1:
            bad.append(
                f"用例4：LM 探了 {lm_calls()} 次、SnowLuma 探了 {sl_calls()} 次，"
                "各自应恰好 1 次（缓存 key 串味了？）"
            )
        else:
            print("  用例4：LM / SnowLuma 各缓存各的，互不串味")

        # ── 用例 5：调用方改返回值不能污染缓存 ────────────────────────
        # api_overview 会把这两个 dict 塞进响应体交给序列化。哪天有人顺手
        # 往里补个字段（比如加个 from_cache 标记），缓存本体就被改脏了，
        # 之后每次刷新读到的都是脏数据 —— 而且不会有任何报错。
        mod._PROBE_CACHE_SECONDS = 20.0
        calls.clear()
        inst5 = cls(StubContext(), dict(CONF))
        first = inst5._lm_studio_info()
        first["models"].append("脏数据")
        first["injected"] = True
        second = inst5._lm_studio_info()
        if "脏数据" in second.get("models", []) or second.get("injected"):
            bad.append("用例5：返回的是缓存本体，调用方一改就把缓存写脏了")
        elif lm_calls() != 1:
            bad.append(f"用例5：应命中缓存（1 次），实际 {lm_calls()} 次")
        else:
            print("  用例5：改返回值不影响缓存（返回的是副本）")

        # ── 用例 6：并发冲进来只能穿透一次 ────────────────────────────
        # api_overview 用 run_in_executor 把两个探针丢进线程池；面板刷新
        # 叠在一起时（比如切回标签页补跑定时器）会有多个线程同时进来。
        # 不做互斥的话它们会同时看到空缓存 → 各探一遍 → 请求量根本没降。
        mod._PROBE_CACHE_SECONDS = 20.0
        calls.clear()
        inst6 = cls(StubContext(), dict(CONF))
        out: list[dict] = []
        errs: list[Exception] = []
        n = 8
        barrier = threading.Barrier(n)

        def worker() -> None:
            try:
                barrier.wait(timeout=5)  # 8 个一起冲
                out.append(inst6._lm_studio_info())
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errs:
            bad.append(f"用例6：并发调探活抛异常了：{type(errs[0]).__name__}: {errs[0]}")
        elif lm_calls() != 1:
            bad.append(f"用例6：{n} 个线程同刻冲进来，实际探了 {lm_calls()} 次（应只 1 次）")
        elif len(out) != n:
            bad.append(f"用例6：{n} 个线程只回了 {len(out)} 个结果")
        else:
            print(f"  用例6：{n} 个线程同刻冲进来只探 1 次，且都拿到了结果")
    finally:
        mod.urllib.request.urlopen = orig_urlopen
        if ttl_backup is not None:
            mod._PROBE_CACHE_SECONDS = ttl_backup

    if bad:
        print("FAIL")
        for b in bad:
            print("  -", b)
        return 1

    print("PASS  探活缓存 6 个用例都过（含 TTL 归零的负向对照与 8 线程并发）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
