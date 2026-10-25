# 链路醒着（astrbot_plugin_ncd_awake）

在 AstrBot 面板上给这台 Windows 机器加一个「保持唤醒」开关：bot 在跑的时候不让它因为空闲进入睡眠/休眠（可选连屏幕一起常亮）。

## 为什么写它

NapCat 和 AstrBot 都没有这个开关；NapCat 的配置里只有托盘/轻量模式，AstrBot 没有系统电源相关设置。GitHub 上叫 `astrbot_plugin_sleep*` 的插件都是群聊作息/睡眠人设，不是防系统休眠，所以这里单独做一个。

## 安装

插件目录：`data/plugins/astrbot_plugin_ncd_awake/`

放好后重启 AstrBot（或在插件管理里重载），侧栏会出现「链路醒着」。

## 使用

- 打开「开启保持」→ 系统不会因空闲睡眠/休眠；AstrBot 退出或关掉开关就恢复。
- 勾「连屏幕一起保持常亮」→ 显示器也不关（更费电）。
- 面板上的开关会写回插件配置：
  - `enabled`：AstrBot 启动时自动开启
  - `keep_display`：是否连屏幕一起
  - `refresh_seconds`：重申间隔，默认 30 秒

## 它怎么做的

Windows `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED [| ES_DISPLAY_REQUIRED])`，在一个常驻线程里调用并定期重申。**不改注册表、不改电源计划**，只对 AstrBot 进程生效。

## 限制

- 不拦手动关机/重启、开始菜单睡眠、合盖动作、Windows 更新自动重启。
- 只支持 Windows；其他系统面板会显示「不支持」。
- AstrBot 进程必须活着；进程退出，唤醒请求自动撤销。

## 回滚

1. 面板关掉开关；
2. 删除插件目录（或停用插件）；
3. 如需清配置，删 `data/config/astrbot_plugin_ncd_awake_config.json`。

没有任何系统级残留，不需要改回电源计划。

## 自查

- `python 测试-唤醒.py`：验证开关、状态、重复启停、页面 HTML 不会被 AstrBot 的资源重写改坏。
- 管理员命令行 `powercfg /requests` 可以看到 `AstrBot` / `python` 的 SYSTEM 请求（非管理员可能看不到明细）。
