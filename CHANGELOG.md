## 2026-11-20 深夜 · 济川AI 接入 + monitor 默认模型显示修正

- 新增第三方 provider「济川AI」（source_id=jichuan，openai_chat_completion，api_base=https://api.jichuanai.com/v1，模型 deepseek-v4.1-flash-1017，modalities text/image/tool_use，max_context_tokens 128k）。
- 默认模型切为 jichuan/deepseek-v4.1-flash-1017，fallback 保留 deepseek/deepseek-flash；chat/tool_use/vision 实测通过，provider test 返回 available。
- monitor 1.7.89：_astrbot_info 优先显示 agent_runner 默认模型（之前只显示 provider 列表第一个），health/状态图不再误显示旧模型。

## 2026-11-20 · monitor 1.7.88：LLM API 余额接入

- 状态查询新增 LLM 余额：从 AstrBot provider/provider_sources 读 DeepSeek api_base+key，调官方 /user/balance，5 分钟缓存；
cd_api_status(scope=llm) 与 /api状态 文本里显示「CNY3.56（充值 3.56/赠送 0.00）」，欠费/不可用会标注。
- 新增聊天指令 /余额（别名 /api余额 /llm余额 /查余额，15 秒会话冷却）。
- 非 DeepSeek provider 明确返回「余额接口不适用」。

## 2026-11-20 · monitor 1.7.86：API/服务状态查询

- 新增 LLM 工具 
cd_api_status(scope=all)：汇总 ComfyUI（运行/模型/队列/最近任务/额度）、电脑 linkd（CPU/内存/GPU/显存）、手机（CPU/内存/温度/运行时长）、AstrBot（模型/命令数/插件数）与最近 ERROR 日志；用户问「出不了图 / 服务挂了吗 / 卡不卡 / API 正常吗」时由 bot 自主调用。
- 新增聊天指令 /api状态（别名 /服务状态 /链路状态 /健康检查，20 秒会话冷却）。
- 新增 Web API GET /ncd-monitor/health?scope=all（返回 snapshot JSON + 中文摘要文本，面板/排查可用）。
- 错误日志过滤只收 [ERROR]/[CRITICAL]/Traceback/Error: 行，排除 WARN 噪音。

## 2026-11-19 深夜 · QQ空间评论关注（qzone 1.2.0）

- 新增「空间评论关注」后台任务：每 10 分钟拉一次自己最近 10 条说说，
  某条说说出现第一条评论后等 60 分钟，让模型读全部评论生成一条统一回复，
  过文本审核后发到该说说下面；每条说说只自动回一次，记录写发布记录（kind=auto_comment_reply）。
- 配置项：开关 / 轮询间隔 / 延迟分钟 / 检查条数 / 回复字数上限（页面与 AstrBot 插件配置均可改）。
- 实测：今晚那条说说 7-9 条评论，自动回复成功发布（已恢复正式参数 60/10）。

## 2026-11-19 深夜 · 全量插件审查（17 插件）修复

- 高危：comfy 图库 dir 路径穿越（可读手机任意文件）、session 群聊对话无权限、catchup 心跳吞停机窗口。
- 中危：natural 数据目录/多行丢内容、memory 头像串行超时、toolbox PDF SSRF 与跳转复查、inject 重复注入与写盘节流、toolbox 提示词 API 路由、awake 非 Windows 静默、extra linkd 403 提示。
- 新增 REVIEW_CONTEXT.md「全量审查修复轮」与已知低危清单，供后续复用子代理审查。

## 2026-11-19 深夜 · 剩余中低危修复

- linkd：/run 支持共享 token 校验（_probe/linkd_run_token.txt 存在才启用；extra 配置 ncd_pc_linkd_token 同值），启动并发互斥。
- extra 1.4.9：/拉起向日葵 带 token；公告先过滤已屏蔽目标再截断（之前屏蔽目标占名额）。
- feedback 1.0.9：管理命令统一 _is_admin（副管理员可用，不再被框架过滤器拦）；清空反馈写盘失败回滚并如实报错。
- comfy 2.44.2：/屏蔽tips 原子写；私聊主动跨天重置 noreply；调试 fire 复核白名单。
- comfy_watchdog：GGUF 清单抽检 10 秒 → 5 分钟；接口异常不再当作成功；重试后降频日志。
- monitor 1.7.84：/设备状态 出图按会话冷却 60 秒；删除当前背景自动回退内置；schema 文案改为 1 张图。
- qzone 1.1.4：删死代码；vision-test 用 NamedTemporaryFile；文本审核 prompt 防注入；页面 error/tid 转义；visibility 文档明确留空=公开。
- status 1.0.12：删除当前背景自动回退内置。
- 新增 REVIEW_CONTEXT.md：给复用子代理的审查上下文（项目地图/接口陷阱/验证命令/已修清单）。

## 2026-11-19 晚间 · bug 修复（子代理审查后）

- status 1.0.11：去重/清空 等修改操作提前做管理员校验（之前普通成员可清空整库）；上传文件名清洗、只收真图片、删除只限图库内文件；settings 原子写 tmp 名唯一。
- comfy 2.44.1：私聊禁止 /主动回复 强制（之前可清空全部主动开关）；强制清空只清群级设置；私聊白名单先校验后落盘、tick 时复核；comfy_generate 入口重置画风模板名上下文（防串台）。
- monitor 1.7.83：状态图预览 preview 从 query 读（之前每次渲染 6000px 原图）+ 页面取值形状/mime 修正；渲染串行化防全局缩放串位；文件名微秒（datetime，Windows 友好）；温度 chip 颜色按数值（之前永远灰）；上传校验/删除限制与 status 对齐。
- qzone 1.1.3：频控改为原子「检查+预占」，并发不超发、失败回滚（之前 check-then-act 可击穿）；配图只允许 base64/公网 URL/data 目录内本地图（禁任意路径与内网 SSRF）；登录态失效清客户端缓存；失败记录也 compact；插件卸载关闭 httpx 连接池。
- panels.py：桥接把 {status:error} 当错误抛（之前当成功返回空数据），解包规则与官方父窗口对齐。

## 2026-11-19

### comfy 2.44.0
- 生图信息里显示「画风模板」名字（模板名随任务传递到后台 caption）。
- 修私聊主动消息不发送：私聊检查独立于群主动开关每 5 分钟跑；重载时按私聊开关恢复循环；/ncd-comfy/proactive 增加私聊排期。
- object_info 兼容 ComfyUI 0.42 的 COMBO 选项格式（不再误报「拿不到底模清单」）。
- 新增 /屏蔽tips（会话级屏蔽出图随机 Tips）。

### monitor 1.7.81
- 状态图：出图额度卡、模型卡 3 列、最长边 6000 原生高清、发送版缩略、背景缓存。
- 状态图背景页：内置/指定/随机、上传（多选+拖拽+去重 sha256）、模糊/压暗。
- 修桥接查询参数注入（预览接口一直回退内置图的根因）。

### 新插件
- strbot_plugin_ncd_status（状态图背景）：独立入口、页面、上传/设置。
- strbot_plugin_ncd_qzone（QQ空间说说）：LLM 工具先商量后发布；文本+配图审核（群聊审核同一套，视觉通道实测可用）；每人每天 1 条、全站 5 条、间隔 30 分钟；仅好友；发布记录；成熟引擎 qzone_bridge + OneBot get_cookies 发布（不再依赖协议端 send_qzone_msg）。

### extra 1.4.8 / feedback 1.0.8
- /拉起向日葵（仅最高管理员，linkd 白名单 + 计划任务免 UAC）。
- /反馈清空（自动备份后清空全部反馈）。

### 其他
- linkd：/run 白名单、优先计划任务启动（免 UAC）。
- comfy_watchdog：启动前等外挂模型库就绪、启动后清单为空自动重启（修「假掉盘」）。
- panels.py：插件页桥接解包业务 data（页面数据/预览正常）、新增状态图背景 tab。
- help.py：/help 改为合并转发「聊天记录」并按功能分类（手机框架补丁）。

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
- 副管理员仅在主管理员不在群聊时生效（两者都在插件配置里填，代码不带 QQ 号）。

## 2026-10-30

- comfy 默认风格 LoRA 改为「秘制柔和动漫风24 @ 0.8」，`+名字` 表示叠加而非替换。
- 进程生命周期迁到 Windows 计划任务（`ComfyUI常驻看护`），插件只做 HTTP。
