# -*- coding: utf-8 -*-
"""把【所有注册的 Web API】都真调一遍（路由回归测试）。

为什么要有这个文件：
    有一次改 `_system_info()`（顺手让它复用 GPU 采样值），
    只测了新写的 /sysinfo —— 它好好的、73 个点、四条曲线都画出来了 ——
    却没测被顺手改动的 /overview，结果 @staticmethod 里写 self.xxx 抛 NameError，
    整个 /overview 500，页面上所有数据变成 "—"，直到用户截图才暴露。

    ★ 教训：动了哪条路由，就把【所有】路由都跑一遍。

跑：python 测试-所有路由.py
    （用 AstrBot 自己的 venv：<AstrBot>\\.venv\\Scripts\\python.exe）
"""
import asyncio
import importlib.util
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")


class StubContext:
    """顶掉 register_web_api，让插件能在没有 AstrBot 运行时的情况下实例化。"""

    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_routes_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_routes_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


async def main() -> int:
    mod = load_plugin()
    cls = None
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                cls = obj
                break
    if cls is None:
        print("找不到插件类")
        return 1

    ctx = StubContext()
    inst = cls(ctx)
    inst._ensure_sampler()
    await asyncio.sleep(2.0)      # 让采样器有点数据

    results = []
    for path, handler, desc in ctx.routes:
        kwargs = {}
        if "<key>" in path:
            # 故意传一个没登记过的 key
            kwargs["key"] = "deadbeefdeadbeef"
        label = f"{handler.__name__:16} {path.split('/ncd-monitor/')[-1]}"
        try:
            resp = await handler(**kwargs)
            code = getattr(resp, "status_code", "?")
            raw = bytes(resp.body).decode("utf-8")
            body = json.loads(raw)

            # /thumb 传没登记过的 key，400/404 是【正确行为】，只要不是 500
            expected_bad = "<key>" in path and code in (400, 404)
            ok = code == 200 or expected_bad

            if isinstance(body, dict):
                shape = "顶层键: " + ", ".join(sorted(body.keys())[:9])
                # count 是 int、items/series 是 list —— 一律别假设长度
                for k in ("count", "items", "series"):
                    if k not in body:
                        continue
                    v = body[k]
                    shape += f"  {k}={len(v)}" if isinstance(v, (list, dict)) else f"  {k}={v}"
            else:
                shape = type(body).__name__

            tag = "OK  " if ok else f"HTTP {code}"
            suffix = f"   (预期内的 {code})" if expected_bad else ""
            print(f"[{tag}] {label}{suffix}")
            print(f"        {shape}")
            if not ok:
                print(f"        体积 {len(raw)} 字节: {raw[:160]}")
            results.append((label, ok))
        except Exception:
            print(f"[FAIL] {label}")
            for line in traceback.format_exc().strip().splitlines()[-4:]:
                print(f"        {line}")
            results.append((label, False))

    print("\n" + "=" * 60)
    bad = [lbl for lbl, ok in results if not ok]
    print(f"总计 {len(results)} 条，失败 {len(bad)} 条")
    for lbl in bad:
        print(f"  ❌ {lbl}")
    await inst.terminate()
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
