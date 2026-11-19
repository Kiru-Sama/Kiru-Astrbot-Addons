"""把本地插件目录同步到 Kiru-Astrbot-Addons 仓库（排除运行时数据）。"""

import os
import shutil

SRC = r"F:\NapCatDesk\ASTRBOT\data\plugins"
DST = r"F:\Deepseek Harness WorkTable\Kiru-Astrbot-Addons"

EXCLUDE_DIRS = {"__pycache__", "data", "thumbcache", ".git"}
EXCLUDE_SUFFIX = (".pyc",)
EXCLUDE_MARK = (".bak-",)

changed = []


def sync_tree(src_root: str, dst_root: str) -> None:
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(dirpath, src_root)
        dst_dir = os.path.join(dst_root, rel) if rel != "." else dst_root
        os.makedirs(dst_dir, exist_ok=True)
        for name in filenames:
            if name.endswith(EXCLUDE_SUFFIX):
                continue
            if any(m in name for m in EXCLUDE_MARK):
                continue
            src = os.path.join(dirpath, name)
            dst = os.path.join(dst_dir, name)
            try:
                if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src) or open(src, "rb").read() != open(dst, "rb").read():
                    shutil.copy2(src, dst)
                    changed.append(os.path.relpath(dst, DST))
            except OSError as exc:
                print(f"  [跳过] {src}: {exc}")


for name in sorted(os.listdir(SRC)):
    src_dir = os.path.join(SRC, name)
    if not os.path.isdir(src_dir) or not name.startswith("astrbot_plugin_ncd_"):
        continue
    dst_dir = os.path.join(DST, name)
    if not os.path.isdir(dst_dir):
        print(f"  [新插件] {name}")
    sync_tree(src_dir, dst_dir)

# 同步根目录下的 linkd.py（PC 侧服务，仓库也收）
local_linkd = r"F:\Deepseek Harness WorkTable\linkd.py"
repo_linkd = os.path.join(DST, "linkd.py")
if os.path.isfile(local_linkd):
    a = open(local_linkd, "rb").read()
    b = open(repo_linkd, "rb").read() if os.path.isfile(repo_linkd) else b""
    if a != b:
        shutil.copy2(local_linkd, repo_linkd)
        changed.append("linkd.py")

print(f"\n  同步完成，变更文件 {len(changed)} 个:")
for c in changed:
    print("   ", c)
