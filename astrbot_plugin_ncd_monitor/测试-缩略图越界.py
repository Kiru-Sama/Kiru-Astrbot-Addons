# -*- coding: utf-8 -*-
"""回归护栏：/thumb 不能靠客户端给的 key 读出缓存目录之外的文件。

背景（真实修复过，别再回来）：
    `_thumb_file(key)` = os.path.join(self._thumb_dir, f"{key}.jpg")
    —— key 直接参与拼路径。而 api_thumb 原本只校验「非空 + len<=64」，
    并且 `_make_thumb` 是【先按客户端 key 读缓存，再查有没有登记过】。

    于是 key = "..\\secret\\shot" 就能读出 <插件目录>\\..\\secret\\shot.jpg
    的 base64 —— 「已认证的任意 .jpg 读取」。

修法两层：
    ① api_thumb 里对 key 做字符集白名单：合法键只可能是 sha1(path) 的前 16 位十六进制
    ② _make_thumb 里【先查登记、再碰磁盘】，并加 realpath 前缀校验做纵深防御

★ 本测试带一个"正对照"：先证明这条越界路径【确实能解析到目标文件】，
  再证明请求被挡下来了 —— 否则万一路径根本没拼对，
  "被挡住"就是假的（什么都没验证到）。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-缩略图越界.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""
import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "main.py")

TRAVERSAL_KEY = r"..\secret\shot"          # 拼出来是 <thumbdir>\..\secret\shot.jpg
LEGIT_KEY = "a1b2c3d4e5f60718"             # 16 位十六进制，形状合法
NONHEX_KEY = "ZZZZZZZZZZZZZZZZ"            # 16 位，但不是十六进制


class StubContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, desc))


def load_plugin():
    spec = importlib.util.spec_from_file_location("ncd_thumb_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncd_thumb_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_plugin_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            if any(b.__name__ == "Star" for b in obj.__mro__[1:]):
                return obj
    return None


def resp_code(resp):
    return getattr(resp, "status_code", None)


def resp_json(resp):
    try:
        return json.loads(bytes(resp.body).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def make_jpeg(path, size=200):
    from PIL import Image

    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", (size, size), (200, 30, 30)).save(path, "JPEG")


async def main() -> int:
    mod = load_plugin()
    cls = find_plugin_class(mod)
    if cls is None:
        print("找不到插件类")
        return 1

    root = tempfile.mkdtemp(prefix="ncd_thumb_")
    thumb_dir = os.path.join(root, "thumbcache")
    victim = os.path.join(root, "secret", "shot.jpg")
    os.makedirs(thumb_dir, exist_ok=True)
    make_jpeg(victim)

    inst = cls(StubContext())
    inst._thumb_dir = thumb_dir

    bad = []
    print(f"临时根目录 : {root}")
    print(f"缩略图目录 : {thumb_dir}")
    print(f"越界目标   : {victim}")
    print()

    # ── 正对照：证明这条越界路径确实解析到目标文件 ──────────────────
    print("===== 正对照：越界路径确实能拼到目标文件 =====")
    escaped = inst._thumb_file(TRAVERSAL_KEY)
    resolved = os.path.realpath(escaped)
    print(f"  _thumb_file({TRAVERSAL_KEY!r}) = {escaped}")
    print(f"  realpath                        = {resolved}")
    print(f"  == 目标文件 ?                    {resolved == os.path.realpath(victim)}")
    if resolved != os.path.realpath(victim):
        bad.append("正对照失败：这条 key 没拼到目标文件，后面几条断言就说明不了问题")
    print()

    # ── 用例 1：越界 key 必须被字符集检查挡下（400）─────────────────
    print("===== 用例 1：越界 key → 必须 400 =====")
    r1 = await inst.api_thumb(key=TRAVERSAL_KEY)
    body1 = resp_json(r1)
    print(f"  HTTP = {resp_code(r1)}  body = {body1}")
    if resp_code(r1) != 400:
        bad.append(f"★核心回归：越界 key 应返回 400，实际 {resp_code(r1)}")
    if "data" in body1:
        bad.append("★核心回归：越界 key 竟然返回了图片数据！")
    print()

    # ── 用例 2：非十六进制的 key 也要 400 ──────────────────────────
    print("===== 用例 2：非十六进制 key → 必须 400 =====")
    r2 = await inst.api_thumb(key=NONHEX_KEY)
    print(f"  HTTP = {resp_code(r2)}")
    if resp_code(r2) != 400:
        bad.append(f"用例2：非十六进制 key 应返回 400，实际 {resp_code(r2)}")
    print()

    # ── 用例 3：合法的键但没登记过 → 404（不能瞎猜路径）────────────
    print("===== 用例 3：合法格式但未登记的 key → 必须 404 =====")
    r3 = await inst.api_thumb(key=LEGIT_KEY)
    print(f"  HTTP = {resp_code(r3)}")
    if resp_code(r3) != 404:
        bad.append(f"用例3：未登记的合法键应返回 404，实际 {resp_code(r3)}")
    print()

    # ── 用例 4：_make_thumb 在 path 未登记时必须直接返回 None ────────
    print("===== 用例 4：_make_thumb(越界 key, path='') → None =====")
    got = inst._make_thumb(TRAVERSAL_KEY, "")
    print(f"  返回 = {got!r}")
    if got is not None:
        bad.append("★核心回归：path 为空时 _make_thumb 仍读出了文件")
    print()

    # ── 用例 5：即使把真实路径喂进去，realpath 校验也要拦住越界写 ─────
    print("===== 用例 5：_make_thumb(越界 key, 真实路径) → 被 realpath 拦住 =====")
    outside_before = sorted(os.listdir(os.path.join(root, "secret")))
    got5 = inst._make_thumb(TRAVERSAL_KEY, victim)
    outside_after = sorted(os.listdir(os.path.join(root, "secret")))
    print(f"  返回 = {got5!r}")
    print(f"  secret 目录内容 {outside_before} -> {outside_after}")
    if got5 is not None:
        bad.append("★核心回归：越界 key 仍然读到了文件内容")
    if outside_before != outside_after:
        bad.append("用例5：越界 key 往缓存目录之外写了文件")
    print()

    # ── 用例 6：正常路径不能被改坏 ────────────────────────────────
    print("===== 用例 6：正常路径仍然可用（别把好功能修坏）=====")
    inst._thumb_paths[LEGIT_KEY] = victim
    r6 = await inst.api_thumb(key=LEGIT_KEY)
    body6 = resp_json(r6)
    data = body6.get("data", "")
    print(f"  HTTP = {resp_code(r6)}  data 前缀 = {data[:32]!r}  长度 = {len(data)}")
    if resp_code(r6) != 200:
        bad.append(f"用例6：正常 key 应返回 200，实际 {resp_code(r6)}")
    if not data.startswith("data:image/jpeg;base64,"):
        bad.append("用例6：正常 key 应返回 jpeg data URL")
    cached = os.path.join(thumb_dir, LEGIT_KEY + ".jpg")
    print(f"  缓存文件已生成: {os.path.exists(cached)}")
    if not os.path.exists(cached):
        bad.append("用例6：正常路径应把缩略图写进缓存目录")
    # 第二次应命中缓存
    r6b = await inst.api_thumb(key=LEGIT_KEY)
    if resp_json(r6b).get("data") != data:
        bad.append("用例6：第二次请求（走缓存）结果不一致")
    print()

    await inst.terminate()
    shutil.rmtree(root, ignore_errors=True)

    print("===== 判定 =====")
    if bad:
        print("FAIL")
        for x in bad:
            print("  -", x)
        return 1
    print("PASS  越界被拦、未登记被拒、正常路径照常工作")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
