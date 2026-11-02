r"""用正斜杠重打手机版 zip。

Windows PowerShell 的 Compress-Archive 会把路径写成 `插件\x\main.py`，
Linux 解压时反斜杠是合法文件名字符，结果就是一个名字里带反斜杠的单文件。
这里用 zipfile 逐条写，arcname 统一成 `/`。
"""
import io
import os
import zipfile

import sys

BASE = r"F:\Deepseek Harness WorkTable"
# ★ 2026-11-02：接受参数，让「手机电脑接入版」也能被重打。
#   原来写死手机版 → 接入版保留 Compress-Archive 的反斜杠，Linux 解压即损坏。
if len(sys.argv) >= 3:
    STAGE, OUT = sys.argv[1], sys.argv[2]
else:
    STAGE = os.path.join(BASE, "AstrBot插件分享包-手机版")
    OUT = os.path.join(BASE, "AstrBot插件分享包-手机版.zip")

if not os.path.isdir(STAGE):
    raise SystemExit("stage 目录不存在: " + STAGE)

count = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root, dirs, files in os.walk(STAGE):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, STAGE).replace("\\", "/")
            z.write(full, rel)
            count += 1

print("entries:", count)
print("size:", os.path.getsize(OUT), "bytes")

# 自检：不允许出现反斜杠
with zipfile.ZipFile(OUT) as z:
    bad = [n for n in z.namelist() if "\\" in n]
    print("backslash entries:", len(bad))
    if bad:
        for n in bad[:5]:
            print("  ", n)
    print("sample:")
    for n in z.namelist()[:6]:
        print("  ", n)
