# -*- coding: utf-8 -*-
"""静态检查：找出「函数里引用了、但模块级没定义」的名字。

为什么需要（2026-11-02 两次真实事故）：
  · P1-9 拆函数时留下 `force` 引用 → api_link 每次 NameError
  · ② 错误分类时 main.py 用了 settings_mod 却没导入 → 出图必 NameError
  这两个 py_compile 都过（语法合法），只有运行到那一行才炸。
  symtable 能在不执行代码的前提下抓出来。

用法：python _lint_undefined.py [文件...]
"""

import builtins
import io
import symtable
import sys

DEFAULT_FILES = []

# ★ 2026-11-02：原来是 6 个写死的文件，漏掉了 presets.py 这种
#   「有 logger 调用但没导入」的模块。改成扫全部插件（排除 测试-*.py 与 __pycache__）。
import glob as _glob

_PLUGINS_ROOT = r'F:\\NapCatDesk\\ASTRBOT\\data\\plugins'
for _p in _glob.glob(_PLUGINS_ROOT + '\\\\astrbot_plugin_ncd_*\\\\*.py'):
    if '测试-' in _p or '__pycache__' in _p:
        continue
    DEFAULT_FILES.append(_p)

BUILTIN = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__"}


def undefined_names(path: str) -> list[tuple[str, str]]:
    src = io.open(path, encoding="utf-8").read()
    table = symtable.symtable(src, path, "exec")
    module_names = {
        s.get_name()
        for s in table.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace()
    }
    missing: list[tuple[str, str]] = []

    def walk(tbl):
        for sym in tbl.get_symbols():
            name = sym.get_name()
            if not sym.is_global():
                continue
            if name in module_names or name in BUILTIN:
                continue
            if any(n == name for n, _ in missing):
                continue
            missing.append((name, tbl.get_name()))
        for child in tbl.get_children():
            walk(child)

    walk(table)
    return missing


def main(argv: list[str]) -> int:
    files = argv[1:] or DEFAULT_FILES
    bad = 0
    for f in files:
        try:
            miss = undefined_names(f)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {f}: {type(exc).__name__}: {exc}")
            bad += 1
            continue
        short = f.split("\\")[-1]
        if miss:
            bad += 1
            print(f"  FAIL  {short}: {len(miss)} 个未定义名")
            for name, scope in miss[:10]:
                print(f"        {name}  （在 {scope} 里被引用）")
        else:
            print(f"  PASS  {short}: 无未定义名")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
