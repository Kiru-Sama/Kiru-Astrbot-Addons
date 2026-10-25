# -*- coding: utf-8 -*-
"""会话插件字段兼容：AstrBot 4.x 的 Conversation 用 cid，不是 conversation_id。"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location("session_under_test", PLUGIN / "main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

inst = object.__new__(mod.NcdSessionPlugin)
new_conv = SimpleNamespace(cid="5567507d-aaaa", title="新字段")
old_conv = SimpleNamespace(conversation_id="old-1234", title="旧字段")
checks = [
    ("读 cid", inst._cid(new_conv) == "5567507d-aaaa"),
    ("兼容旧 conversation_id", inst._cid(old_conv) == "old-1234"),
    ("按序号解析新字段", inst._resolve([new_conv], "1") == "5567507d-aaaa"),
    ("按 ID 前缀解析新字段", inst._resolve([new_conv], "5567") == "5567507d-aaaa"),
    ("空字段不误匹配", inst._resolve([SimpleNamespace(cid="")], "1") is None),
]
for name, ok in checks:
    print(("PASS " if ok else "FAIL ") + name)
if not all(ok for _, ok in checks):
    sys.exit(1)
print("ALL CHECKS PASSED")
