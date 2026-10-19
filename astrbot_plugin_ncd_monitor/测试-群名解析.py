# -*- coding: utf-8 -*-
"""回归护栏：群名要读【当前账号】的身份库，而且改名后要能刷新。

背景（2026-10-14 实测）：
    面板上群名一直显示不出来（只有「群 <群号>」）。查下来两处问题：

    ① 挑库用的是 _first_match(data\\*, snowluma_identity.db) —— 它是
       sorted()[0]，于是永远挑到 **旧账号** 的库（换号后就不再更新，
       里面只有 13 个群，新账号进的群一个都没有）。
       新账号的库其实一直在更新，只是没被读到。

    ② 群名表是「只读一次、永久缓存」（_group_names_loaded）——
       就算库更新了、群改名了，也要重载插件才看得到。

    ③ OneBot 配置（config\\onebot_<QQ号>.json）当时也用的 _first_match，
       同一个坑漏改了（2026-10-17 发现）。读错的表现：面板上 SnowLuma 的
       has_token / host / ws_clients 三项全是旧账号的值——比如显示
       "ws_clients: 0"，而新账号明明有 1 条 AstrBot 的连接。

跑：<AstrBot>\\.venv\\Scripts\\python.exe 测试-群名解析.py
    （GBK 终端下要加 PYTHONIOENCODING=utf-8）
"""

import os
import pathlib
import sqlite3
import sys
import tempfile
import time

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main as plugin_main  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, message: str) -> None:
    if not condition:
        failures.append(f"{label}: {message}")


def make_db(path: pathlib.Path, groups: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute(
        "create table if not exists groups "
        "(group_id integer, group_name text, remark text, member_count int)"
    )
    con.execute("delete from groups")
    for gid, name in groups.items():
        con.execute("insert into groups values (?,?,?,?)", (int(gid), name, "", 0))
    con.commit()
    con.close()


def make_plugin(db_path: str):
    """绕过 __init__：只放缓存和一个假的 env 缓存。"""
    inst = plugin_main.Main.__new__(plugin_main.Main)
    inst._conf = {}
    inst._group_names = {}
    inst._group_names_loaded = False
    inst._group_names_mtime = 0.0
    inst._env_cache = {"sl_identity_db": db_path}
    return inst


tmp = pathlib.Path(tempfile.mkdtemp(prefix="ncd_gname_"))
data_dir = tmp / "data"
OLD = data_dir / "1111111111" / "snowluma_identity.db"
NEW = data_dir / "2222222222" / "snowluma_identity.db"

make_db(OLD, {"1037142102": "旧库里的群", "111111111": "只有旧库有"})
time.sleep(0.05)
make_db(NEW, {"1130165461": "新账号的新群", "1037142102": "卡拉彼丘（新名）"})

# =====================================================================
#  用例 1：挑库必须挑【新的那个】，不能是 sorted()[0]
# =====================================================================
print("===== 用例 1：挑身份库 =====")
old_first = plugin_main._first_match(str(data_dir / "*"), "snowluma_identity.db")
picked = plugin_main._find_identity_db(str(tmp))
print(f"  旧逻辑 _first_match → {os.path.relpath(old_first, tmp)}")
print(f"  新逻辑 _find_identity_db → {os.path.relpath(picked, tmp)}")
check("用例1", os.path.basename(os.path.dirname(old_first)) == "1111111111",
      "自检失败：旧逻辑竟然没挑到旧账号那个，用例测不出东西")
check("用例1", picked == str(NEW), f"★挑错了库：应挑新账号的，实际 {picked}")
check("用例1", bool(picked) and os.path.isfile(picked), "挑出来的路径不存在")

# 边界：没有库时返回空串，不能抛
empty = tmp / "empty"
empty.mkdir()
check("用例1", plugin_main._find_identity_db(str(empty)) == "", "空目录应返回空串")

# =====================================================================
#  用例 2：加载后应拿到【新库】里的群名
# =====================================================================
print("\n===== 用例 2：群名来自新库 =====")
inst = make_plugin(str(NEW))
inst._load_group_names()
print(f"  读到 {len(inst._group_names)} 个群名")
print(f"  1130165461 → {inst._group_name('1130165461')!r}")
print(f"  1037142102 → {inst._group_name('1037142102')!r}")
check("用例2", inst._group_name("1130165461") == "新账号的新群",
      "新账号的群查不到名字（说明还在读旧库）")
check("用例2", inst._group_name("1037142102") == "卡拉彼丘（新名）",
      "同名群应取新库里的名字")
check("用例2", inst._group_name("111111111") == "",
      "只存在于旧库的群不该出现（那说明挑的还是旧库）")

# =====================================================================
#  用例 3：群改名后，刷新要能跟上（不用重载插件）
# =====================================================================
print("\n===== 用例 3：改名后刷新 =====")
# 对照：不调刷新的话，缓存里还是旧名字
before = inst._group_name("1130165461")
make_db(NEW, {"1130165461": "改过名的新群", "1037142102": "卡拉彼丘（新名）"})
stale = inst._group_name("1130165461")
print(f"  改库之后、未刷新 → {stale!r}（应还是旧名，证明缓存确实在生效）")
check("用例3", stale == before, "自检失败：没刷新就已经变了，用例测不出东西")

inst._refresh_group_names()
after = inst._group_name("1130165461")
print(f"  调 _refresh_group_names() 之后 → {after!r}")
check("用例3", after == "改过名的新群", "★改名后没刷新（还是旧名字）")

# 没变化时不该反复重读（mtime 没变 → 直接返回）
mtime_before = inst._group_names_mtime
inst._refresh_group_names()
check("用例3", inst._group_names_mtime == mtime_before, "无变化时不该重读")

# =====================================================================
#  用例 4：显式配置优先
# =====================================================================
print("\n===== 用例 4：显式配置优先 =====")
inst4 = make_plugin(str(OLD))
inst4._conf = {"snowluma_identity_db": str(OLD)}
inst4._env_cache = {"sl_identity_db": str(OLD)}
inst4._load_group_names()
check("用例4", inst4._group_name("111111111") == "只有旧库有",
      "显式配置了就该用配置的那个")

# =====================================================================
#  用例 5：库读不到时不能崩、也不能把旧数据留着装没事
# =====================================================================
print("\n===== 用例 5：库消失/损坏 =====")
inst5 = make_plugin(str(tmp / "nope" / "snowluma_identity.db"))
inst5._load_group_names()
print(f"  读不存在的库后，群名表 = {inst5._group_names}")
check("用例5", inst5._group_names == {}, "库不存在时不该有群名")
check("用例5", inst5._group_name("123") == "", "库不存在时查询应返回空串而不是抛异常")

# =====================================================================
#  用例 6：OneBot 配置也要挑【最新改动的那个】
#     和用例 1 是同一个坑：文件名是 onebot_<QQ号>.json，
#     sorted()[0] 永远挑到号最小的——也就是换号前的旧账号。
#     读错的表现：面板上 SnowLuma 的 has_token / host / ws_clients 三项全错。
# =====================================================================
print("\n===== 用例 6：挑 OneBot 配置 =====")
cfg_dir = tmp / "config"
cfg_dir.mkdir(parents=True, exist_ok=True)
OLD_CFG = cfg_dir / "onebot_1111111111.json"
NEW_CFG = cfg_dir / "onebot_2222222222.json"
OLD_CFG.write_text('{"networks": {"httpServers": [], "wsClients": []}}', encoding="utf-8")
time.sleep(0.05)
NEW_CFG.write_text(
    '{"networks": {"httpServers": [{"host": "0.0.0.0", "port": 3000, "accessToken": ""}],'
    ' "wsClients": [{"name": "ncd-app", "url": "ws://127.0.0.1:38978/ws"}]}}',
    encoding="utf-8",
)

old_pick = plugin_main._first_match(str(cfg_dir), "onebot_*.json")
new_pick = plugin_main._find_onebot_config(str(tmp))
print(f"  旧逻辑 _first_match           → {os.path.basename(old_pick)}")
print(f"  新逻辑 _find_onebot_config    → {os.path.basename(new_pick)}")
check("用例6", old_pick.endswith("onebot_1111111111.json"),
      "自检失败：旧逻辑竟然没挑到旧账号那个，用例测不出东西")
check("用例6", new_pick == str(NEW_CFG), f"★挑错了 OneBot 配置：应挑新号的，实际 {new_pick}")

# 边界：没有配置目录时返回空串，不能抛
no_cfg = tmp / "no_config_here"
no_cfg.mkdir()
check("用例6", plugin_main._find_onebot_config(str(no_cfg)) == "",
      "没有 config 目录时应返回空串")

# 边界：只有一个配置时也要挑到它（退回 _first_match 的那条路）
only = tmp / "only_one"
(only / "config").mkdir(parents=True)
(only / "config" / "onebot_3333333333.json").write_text("{}", encoding="utf-8")
check("用例6", plugin_main._find_onebot_config(str(only)).endswith("onebot_3333333333.json"),
      "只有一个配置时应挑到它")

tmp_cleanup = str(tmp)
import shutil  # noqa: E402

shutil.rmtree(tmp_cleanup, ignore_errors=True)

print("\n===== 判定 =====")
if failures:
    print("失败项:")
    for item in failures:
        print(f"  ✗ {item}")
    sys.exit(1)
print("PASS: 挑库（新账号优先）/ 群名来源 / 改名后刷新 / 显式配置 / 库缺失降级 / 挑 OneBot 配置 全部正确")
