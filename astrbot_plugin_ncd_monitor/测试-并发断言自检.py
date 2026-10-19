# -*- coding: utf-8 -*-
"""自检：把 _ENV_LOCK 换成空壳后，用例 8 会不会真的报错？

"测试能不能失败"本身也要验证 —— 否则断言写错了会永远绿。
（这个教训来自测试-流式捕获.py 的第一版：它靠"闭包捕获了谁"判断，
 修好之后闭包不捕获了，于是把"没捕获"误判成"已修复"，报了个假 PASS。）
"""
import contextlib
import importlib.util
import logging
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")


class StubContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


def load():
    spec = importlib.util.spec_from_file_location("ncd_lock_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_lock_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def count_env_logs(mod, cls, lock) -> int:
    records = []
    mod_first = mod._first_match

    def slow_first_match(directory, pattern):  # noqa: ANN001
        time.sleep(0.05)  # 拉长临界区，让"另一个线程也看到空缓存"成为必然
        return ""

    class C(logging.Handler):
        def emit(self, record):
            try:
                records.append(record.getMessage())
            except Exception:
                pass

    mod._ENV_LOCK = lock
    mod._first_match = slow_first_match
    h = C()
    mod.logger.addHandler(h)
    old = mod.logger.level
    mod.logger.setLevel(logging.INFO)
    try:
        inst = cls(StubContext(), {"snowluma_dir": r"X:\nonexistent\SnowLuma"})
        N = 8
        barrier = threading.Barrier(N)

        def worker():
            try:
                barrier.wait(timeout=5)
                inst._env()
            except Exception:
                pass

        ts = [threading.Thread(target=worker) for _ in range(N)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        mod._first_match = mod_first
        mod.logger.removeHandler(h)
        mod.logger.setLevel(old)
    return len([r for r in records if "环境解析" in r])


def main() -> int:
    mod = load()
    cls = None
    for name in dir(mod):
        o = getattr(mod, name)
        if isinstance(o, type) and o.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in o.__mro__[1:]):
                cls = o
                break
    if cls is None:
        print("找不到插件类")
        return 1

    real = count_env_logs(mod, cls, threading.Lock())
    print(f"  用真锁      : 8 线程 -> {real} 行「环境解析」")

    # 换成"永不阻塞"的空壳锁，模拟修复前的竞态
    mod2 = load()
    for name in dir(mod2):
        o = getattr(mod2, name)
        if isinstance(o, type) and o.__module__ == mod2.__name__:
            if any(b.__name__ == "Star" for b in o.__mro__[1:]):
                cls2 = o
                break
    fake = count_env_logs(mod2, cls2, contextlib.nullcontext())
    print(f"  换空壳锁后  : 8 线程 -> {fake} 行「环境解析」")

    print()
    if fake > 1:
        print("PASS  这条断言确实能失败 —— 去掉锁就会冒出多行，说明测试有效")
        return 0
    print("FAIL  换掉锁也还是 1 行：说明这条断言测不出问题（形同虚设）")
    print("      （可能是线程没真正并发：加个 sleep 或者加大线程数再试）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
