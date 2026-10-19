# 链路补丁（astrbot_plugin_ncd_patch）

AstrBot 源码补丁的**管理器**：只做状态自检与手动应用/回滚，**绝不自动打补丁**。

## 为什么单独一个插件

补丁以前散在监控插件里，谁都不知道"到底打没打"；AstrBot 一升级补丁就静默失效。
现在收在一处：

- 清单 `patches/校验值.json`：每个补丁记录目标文件 + 打前/打后 SHA256 前 16 位
- 状态自检：**已应用 / 未应用 / 版本不匹配 / 目标缺失**
- 手动应用与回滚，且都先 `git apply --check`（预检不过 → 一个字节都不改）

## 工具（仅管理员）

| 工具 | 作用 |
|---|---|
| `patch_status()` | 列出全部补丁及状态 |
| `patch_apply(id)` | 打上（先 `--check`，再 `git apply -p1`） |
| `patch_revert(id)` | 回滚（先 `--check`，再 `git apply -R -p1`） |

非管理员调用会被直接拒绝（`event.role != "admin"`）。

## 当前补丁

| ID | 目标 | 说明 |
|---|---|---|
| P1 | `core/tools/cron_tools.py` | 定时任务参数兜底 |
| P2 | `builtin_stars/astrbot/main.py` | 戳一戳按人设回应 |
| P4 | `core/astr_main_agent.py` | 模型不支持工具时不再塞工具提示 |

> P3（动图取 3 帧）已于 2026-10-19 撤销：AstrBot 恢复原版 3×3 九宫格，
> 补丁文件与校验条目都已删除。

## 升级 AstrBot 之后

`patch_status` 会显示**版本不匹配**（当前 SHA 与打前/打后都不一致）。
这时不要硬打：先 `git apply --check` 看失败点，再重新生成补丁
（监控插件里的 `生成源码补丁.py` 默认输出到这里）。

## 手动应用（不用插件也行）

```powershell
# 在 <AstrBot>\.venv\Lib\site-packages\astrbot 下
git apply --check -p1 <本目录>\patches\P1-cron_tools.py.patch
git apply -p1 <本目录>\patches\P1-cron_tools.py.patch
```

也可以用 `patches/应用补丁.ps1`（带校验值比对）。

## 测试

```bash
python 测试-补丁管理.py    # 四种状态 / 非管理员拦截 / 未知 id / 真实补丁预检
```
