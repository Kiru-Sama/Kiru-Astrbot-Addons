# CHANGELOG（手机 AstrBot 插件 + 电脑侧脚本）

> 只记录「用户能感知」或「影响交付」的变更；修复类写清触发场景。
> 版本号对应 `astrbot_plugin_ncd_comfy/metadata.yaml`。

## 2026-11-02

### 审查修复（三批，对应外部审查报告的 P0/P1）

- **comfy 2.20.38+**：额度阈值不再写 AstrBot 框架的插件配置文件（原来会被框架保存覆盖），
  改存 `data/plugin_data/ncd_comfy/quota/config.json`；不再替换 `self._conf`。
- **comfy**：`reference` 只能读三个根目录（会话图 / ref 缓存 / AstrBot temp），
  http 取图拒内网与环回地址（SSRF）。
- **monitor 1.7.31+**：`/state/comfy` 与 `/thumb` 的 PIL 解码移出事件循环（`asyncio.to_thread`）。
- **comfy**：群聊发图间隔改到排队阶段判定，发送前最多等 15 秒，不再占死单 worker。
- **comfy**：出图中断线检测——每 15 秒探活，连续 3 次失败立刻中止，不再干等 300 秒。
- **comfy**：超时后调用 `/interrupt` + `/queue delete`，避免一次超时导致后续张张超时。
- **comfy**：历史裁剪叠加文件 mtime 校验 + 单次最多删 20 张（防索引时间戳异常时一次删光）。
- **comfy**：`_pc_link_state` 的同步 HTTP 移出事件循环。
- **link 1.0.5**：补 `terminate()`，热重载不再残留探测循环；metadata 乱码修复。
- **monitor**：手机端没有 `nvidia-smi` 时 GPU 采样直接短路（原来每 5 秒一条 WARNING）。

### 工程化重构（S1 / S2 / B / 错误分类）

- **新增 `comfy_transport.py`**：纯 stdlib 的 ComfyUI 传输层
  （submit / wait / cancel / health / object_info / view / queue），
  错误分四类：`TransportUnreachable / TransportTimeout / ExecutionError / ProtocolError`。
- **`comfy_backend.py`**：协议收发全部委托 Transport；进程管理与排队保留在 ComfyClient。
- **新增 `comfy_settings.py`**：连接参数（host/port/超时/目录）集中到 `ComfySettings`，
  `main.py._client()` 不再逐个 `_conf_*` 拼参数。
- **错误分类**：`ComfyError` 派生出 `ComfyUnreachable / ComfyTimeout / ComfyExecutionError`，
  用户看到的提示按类型区分（检查热点 / 调小尺寸 / 看节点报错）。

### 交付链

- 打包脚本：排除 `测试-*`、`*.bak-*`；robocopy 退出码 ≥8 直接报错；暂存目录兜底清理。
- `_verify_package.py`：版本从 zip 内 metadata 读并与 live 比对；清单 12 个；加垃圾断言。
- 分享包体积从 819 KB 降到 577 KB（剔掉 266 KB 旧版备份与 49 个测试脚本）。
- 插件目录里误复制的 5 份 `cmd_config.json`（含 dashboard 口令哈希）与模板缓存已清理。

### 测试

- 新增 `离线回归.py`（9 条行为断言，不依赖手机/显卡）：
  路径白名单 / SSRF / 裁剪上限 / 版本一致 / api_link / 额度隔离 / 传输层 10 条 / ComfySettings / 错误分类。
- 新增 `测试-transport.py`：假 ComfyUI 上跑 submit→wait→cancel→超时→执行错 全流程。
- 全量测试 43 条串联离线回归。

## 2026-10-31

- 群聊发图：失败直接回错误，不重发、不冷却（按决策砍掉风控限时机制）。
- 副管理员（3028227446）仅在主管理员（3384998797）不在群聊时生效。

## 2026-10-30

- comfy 默认风格 LoRA 改为「秘制柔和动漫风24 @ 0.8」，`+名字` 表示叠加而非替换。
- 进程生命周期迁到 Windows 计划任务（`ComfyUI常驻看护`），插件只做 HTTP。
