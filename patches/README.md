# AstrBot 内置指令补丁

## P1 · 群聊指令限管理员（`group_admin_commands.py`）

**做什么**：给 AstrBot 内置的 `/stop`、`/stats` 补上
`@filter.permission_type(filter.PermissionType.SHARED_GROUP_ADMIN)`。

**为什么**：AstrBot 原生只给 `/reset`、`/new` 加了这条限制 ——
群聊要管理员、私聊不限。但 `/stop`、`/stats` 没有：

| 指令 | 原生行为 | 打补丁后 |
|---|---|---|
| `/reset`、`/new` | 群聊需管理员 | 不变 |
| `/stop` | **任何人**都能停掉本会话正在跑的生成 | 群聊需管理员，私聊不限 |
| `/stats` | **任何人**都能看本会话 token 用量 | 群聊需管理员，私聊不限 |
| `/help`、`/sid` | 所有人可用 | **故意不动**：`/sid` 是群友拿到自己 ID 去加白名单的唯一途径 |

**怎么用**

```bash
# 先看当前状态（不改文件，返回 0 = 已打全）
python group_admin_commands.py --check

# 打补丁（幂等，重复跑不会叠两行）
python group_admin_commands.py

# 撤掉（逐字节还原）
python group_admin_commands.py --revert

# 显式指定 AstrBot 根目录
python group_admin_commands.py --root "D:\\AstrBot"
```

自动找 `<AstrBot>/.venv/Lib/site-packages/astrbot/builtin_stars/builtin_commands/main.py`。

**注意**

- 打的是 **site-packages**，AstrBot 升级会覆盖它 —— 升级后重跑一次（`--check` 可以放进启动脚本）。
- 改完**要重启 AstrBot** 才生效（模块已经在内存里）。
- 是否限权由 AstrBot 的 `admins_id` 决定：QQ 号在里面的人 `role` 才是 `admin`。
  私聊永远不受这条限制。
- 回归测试：插件目录里的 `测试-群管补丁.py` 用假文件覆盖了识别 / 打上 / 幂等 / 撤回 / 缺指令不炸。
