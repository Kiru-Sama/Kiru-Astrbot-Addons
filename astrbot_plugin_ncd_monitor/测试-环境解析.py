# -*- coding: utf-8 -*-
"""验证环境解析（`_env()`）：配置优先 → 自动探测 → 内置缺省。

为什么要有这个文件：
    发给别人用的版本不能再写死 `<NapCat>\\...`。改成可配置之后，
    多出了三类新逻辑，每一类错了都【不会报错、只会悄悄不对】：
      1. 配置了却不生效（还被自动探测的值盖掉）
      2. 自动探测找错目录 → 读到别人的 OneBot 配置，或者读到空
      3. 用户从资源管理器复制路径时带上了引号 / 首尾空格 → open() 失败
    所以在这里钉死。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-环境解析.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""
import asyncio
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")


class StubContext:
    """顶掉 register_web_api，让插件能在没有 AstrBot 运行时的情况下实例化。"""

    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_env_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_env_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


async def main() -> int:
    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    bad = []

    # ── 用例 1：不给配置（模拟别人机器上首次加载）────────────────────
    # 自动探测应该找不到【这台机器以外】的东西；在这台机器上应该能找到。
    # 两种结果都算过 —— 关键是：不能抛异常，字段必须是字符串。
    inst = cls(StubContext())
    env = inst._env()
    for key in ("lm_base", "sl_base", "sl_dir", "sl_config", "sl_identity_db"):
        if key not in env:
            bad.append(f"用例1：_env() 缺字段 {key}")
        elif not isinstance(env[key], str):
            bad.append(f"用例1：{key} 不是字符串，是 {type(env[key]).__name__}")
    if env.get("lm_base") != "http://127.0.0.1:1234":
        bad.append(f"用例1：lm_base 缺省值错，得到 {env.get('lm_base')!r}")
    if env.get("sl_base") != "http://127.0.0.1:5099":
        bad.append(f"用例1：sl_base 缺省值错，得到 {env.get('sl_base')!r}")
    # 探测到了就必须自洽：有目录就该能推出身份库
    if env.get("sl_dir"):
        if not env.get("sl_identity_db"):
            bad.append(
                f"用例1：探测到 SnowLuma 目录 {env['sl_dir']} 但没推出身份库"
            )
        elif not os.path.exists(env["sl_identity_db"]):
            bad.append(f"用例1：推出一个不存在的身份库 {env['sl_identity_db']}")
        if env.get("sl_config") and not os.path.exists(env["sl_config"]):
            bad.append(f"用例1：推出一个不存在的 OneBot 配置 {env['sl_config']}")

    # ── 用例 2：显式配置必须【盖过】自动探测 ────────────────────────
    fake_dir = r"X:\nonexistent\SnowLuma"
    inst2 = cls(
        StubContext(),
        {
            "lm_studio_base": "http://10.0.0.5:9999",
            "snowluma_base": "http://10.0.0.5:8888",
            "snowluma_dir": fake_dir,
        },
    )
    env2 = inst2._env()
    if env2["lm_base"] != "http://10.0.0.5:9999":
        bad.append(f"用例2：lm_studio_base 没生效，得到 {env2['lm_base']!r}")
    if env2["sl_base"] != "http://10.0.0.5:8888":
        bad.append(f"用例2：snowluma_base 没生效，得到 {env2['sl_base']!r}")
    if env2["sl_dir"] != fake_dir:
        bad.append(f"用例2：snowluma_dir 没生效，得到 {env2['sl_dir']!r}（被探测值盖掉了？）")
    # 这个假目录不存在 → 下面两项推导为空，且不能炸
    if env2["sl_config"]:
        bad.append(f"用例2：假目录不该推出一项 OneBot 配置，得到 {env2['sl_config']!r}")
    if env2["sl_identity_db"]:
        bad.append(f"用例2：假目录不该推出身份库，得到 {env2['sl_identity_db']!r}")

    # ── 用例 3：显式指定两个文件路径时，snowluma_dir 可以留空 ────────
    inst3 = cls(
        StubContext(),
        {
            "snowluma_dir": "",
            "snowluma_onebot_config": r"Y:\a\onebot_1.json",
            "snowluma_identity_db": r"Y:\b\snowluma_identity.db",
        },
    )
    env3 = inst3._env()
    if env3["sl_config"] != r"Y:\a\onebot_1.json":
        bad.append(f"用例3：显式 OneBot 配置没生效，得到 {env3['sl_config']!r}")
    if env3["sl_identity_db"] != r"Y:\b\snowluma_identity.db":
        bad.append(f"用例3：显式身份库没生效，得到 {env3['sl_identity_db']!r}")

    # ── 用例 4：从资源管理器复制来的路径常带引号/空格，要能收拾 ──────
    inst4 = cls(
        StubContext(),
        {
            "lm_studio_base": '  "http://127.0.0.1:7777"  ',
            "snowluma_dir": "  'Z:\\x\\SnowLuma'  ",
        },
    )
    env4 = inst4._env()
    if env4["lm_base"] != "http://127.0.0.1:7777":
        bad.append(f"用例4：引号没剥掉，得到 {env4['lm_base']!r}")
    if env4["sl_dir"] != r"Z:\x\SnowLuma":
        bad.append(f"用例4：引号/空格没剥掉，得到 {env4['sl_dir']!r}")

    # ── 用例 5：配置是空字符串时不能把缺省值也顶掉 ──────────────────
    inst5 = cls(StubContext(), {"lm_studio_base": "", "snowluma_base": "   "})
    env5 = inst5._env()
    if env5["lm_base"] != "http://127.0.0.1:1234":
        bad.append(f"用例5：空配置应回落到缺省值，得到 {env5['lm_base']!r}")
    if env5["sl_base"] != "http://127.0.0.1:5099":
        bad.append(f"用例5：空白配置应回落到缺省值，得到 {env5['sl_base']!r}")

    # ── 用例 6：群名读不到时不能崩，_group_name 返回空串 ─────────────
    try:
        inst6 = cls(StubContext(), {"snowluma_dir": r"X:\nonexistent\SnowLuma"})
        inst6._load_group_names()
        if inst6._group_name("123") != "":
            bad.append("用例6：身份库不存在时 _group_name 应返回空串")
        if inst6._group_name(None) != "":
            bad.append("用例6：group_id 为 None 时应返回空串")
    except Exception as exc:  # noqa: BLE001
        bad.append(f"用例6：身份库缺失时崩了：{type(exc).__name__}: {exc}")

    # ── 用例 7：/overview 和 /snowluma 相关函数在探测不到时也不能炸 ──
    try:
        inst7 = cls(StubContext(), {"snowluma_dir": r"X:\nonexistent\SnowLuma"})
        sl = inst7._snowluma_info()
        if "endpoints" in sl and sl["endpoints"]:
            bad.append("用例7：假目录不该读出端点")
        if not sl.get("config_error"):
            bad.append("用例7：读不到配置时 config_error 应有说明，前端要靠它提示")
        lm = inst7._lm_studio_info()
        if "base" not in lm:
            bad.append("用例7：lm_studio 信息里应带 base，便于排查端口配错")
    except Exception as exc:  # noqa: BLE001
        bad.append(f"用例7：探测失败路径崩了：{type(exc).__name__}: {exc}")

    # ── 用例 8：并发调 _env() 只能解析一次、只打一行日志 ──────────────
    # 为什么这条重要：api_overview 把 _lm_studio_info 和 _snowluma_info
    # 丢进【两个线程】并发跑，两边都会调 _env()。没有锁的话它们会同时
    # 看到缓存是空的 → 各算一遍 → 日志里出现两行一模一样的「环境解析」，
    # 看着像加载了两个插件实例，非常误导排查。
    #
    # ★★ 光起 8 个线程【测不出问题】：_env() 只要几十微秒，
    #    第一个线程早就把缓存填好了，后面全部命中缓存 —— 那样即使把锁删掉
    #    断言也照样绿，等于没测。所以这里做两件事把竞态【做成必然】：
    #      ① 用 Barrier 让 8 个线程同一瞬间进 _env()
    #      ② 把 _first_match 换成带 sleep 的版本，拉长临界区
    #    （这一步是 _自检-锁是否必要.py 发现并逼出来的：
    #      换掉锁必须能让断言变红，否则这个用例就是摆设。）
    import logging
    import threading
    import time

    records: list[str] = []

    class _Counter(logging.Handler):
        def emit(self, record):  # noqa: ANN001
            try:
                records.append(record.getMessage())
            except Exception:  # noqa: BLE001
                pass

    handler = _Counter()
    orig_first_match = mod._first_match

    def slow_first_match(directory, pattern):  # noqa: ANN001
        time.sleep(0.05)  # 拉长临界区，让"另一个线程也看到空缓存"成为必然
        return ""

    mod._first_match = slow_first_match
    mod.logger.addHandler(handler)
    old_level = mod.logger.level
    mod.logger.setLevel(logging.INFO)
    try:
        inst8 = cls(StubContext(), {"snowluma_dir": r"X:\nonexistent\SnowLuma"})
        results: list[dict] = []
        errors: list[Exception] = []
        N = 8
        barrier = threading.Barrier(N)

        def worker() -> None:
            try:
                barrier.wait(timeout=5)  # 8 个一起冲
                results.append(inst8._env())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            bad.append(f"用例8：并发调 _env() 抛异常了：{errors[0]}")
        hits = [r for r in records if "环境解析" in r]
        print(f"  用例8：{N} 个线程同刻冲进 _env()，打了 {len(hits)} 行「环境解析」")
        if len(hits) != 1:
            bad.append(
                f"用例8：并发调 _env() 应只打 1 行日志（只解析一次），实际 {len(hits)} 行"
            )
        if len(results) != N or any(r is not results[0] for r in results):
            bad.append("用例8：并发调用拿到的不是同一个缓存对象")
    finally:
        mod._first_match = orig_first_match
        mod.logger.removeHandler(handler)
        mod.logger.setLevel(old_level)

    if bad:
        print("FAIL")
        for b in bad:
            print("  -", b)
        return 1

    print("PASS  环境解析 8 个用例都过（含并发只解析一次）")
    print(f"  本机自动探测结果：")
    print(f"    LM Studio      {env['lm_base']}")
    print(f"    SnowLuma WebUI {env['sl_base']}")
    print(f"    SnowLuma 根目录 {env['sl_dir'] or '(未探测到)'}")
    print(f"    OneBot 配置     {env['sl_config'] or '(未探测到)'}")
    print(f"    身份库          {env['sl_identity_db'] or '(未探测到)'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
