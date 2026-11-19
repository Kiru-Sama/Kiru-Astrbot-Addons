# AstrBot 插件群 · 代码审查上下文（子代理复用）

> 给"找 bug / 审查 / 验证"类子代理的固定上下文。先读本文件，不要再从零探索项目。
> 上次全量审查 + 修复：2026-11-19（详见文末"已修复清单"，不要重复报告已修项）。

## 1. 项目地图

- 插件源码（手机运行时）：`F:\NapCatDesk\ASTRBOT\data\plugins\`
- 仓库快照（与本地插件内容一致）：`F:\Deepseek Harness WorkTable\Kiru-Astrbot-Addons`（remote: https://github.com/Kiru-Sama/Kiru-Astrbot-Addons.git，分支 main）
- PC 工具：`F:\Deepseek Harness WorkTable\`：`panels.py`（聚合面板 9001）、`linkd.py`（8201）、`comfy_watchdog.py`（8192 单实例锁 8202）、`打包.ps1`、`打包-手机版.ps1`、`sync_to_repo.py`
- 审查过的插件（前缀 `astrbot_plugin_ncd_`）：
  - `comfy`（链路本地生成，v2.44.2，main.py 大文件）
  - `monitor`（链路监控/设备状态图，v1.7.84；`status_render.py` 负责出图）
  - `status`（状态图背景独立插件，v1.0.12）
  - `qzone`（QQ空间说说，v1.1.4；`qzone_bridge/` 是第三方 MIT 引擎，只审我们的调用/魔改）
  - `extra`（v1.4.9）、`feedback`（v1.0.9），其余 ncd_* 插件未重点审

## 2. 环境事实（避免误报）

- 手机 AstrBot 4.29.4：`http://192.168.137.242:6185`，账号 `astrbot` / `NcdPass2026`；登录 `POST /api/auth/login` 拿 `data.token`（Bearer）。
- 协议端：NapCat 4.18.41（`get_version_info` → app_name=NapCat.Onebot）。**没有** `send_qzone_msg`/`get_qzone_msg_list`（1404 不支持的 Api）。
- qzone 发布走成熟引擎 `qzone_bridge`：OneBot `get_cookies` 拿 Cookie → `QzoneClient.publish_mood(content, photos=[{source:"base64://...",name,mime_type}], ugc_right=1|4|16|64|128)`；本地魔改加了 `ugc_right` 参数（原版硬编码 1）。ugc_right 合法值：1/4/16/64/128（3 非法，之前就是它导致"参数错误"）。
- AstrBot 插件 Web API 契约：
  - 注册：`context.register_web_api(f"/{PLUGIN_DIR_NAME}/ncd-xxx/<route>", handler, methods, desc)`。
  - **handler 只注入路径参数**，查询参数要 `from astrbot.api.web import request as _req; _req.query.get("name")`；请求体 `await _req.json({})`。
  - 返回 `json_response({...})`；插件页 bridge `P.apiGet(endpoint, params)` 的 endpoint 不能含 `?`/`#`/`\`；页面拿到的是**裸业务 JSON**（无 `{data}` 包装）。
  - 面板 9001 桥接（panels.py BRIDGE_JS）会把 AstrBot 响应 `{status,data}` 解包成业务 data 返回；`status==="error"` 时必须 throw（已修）。
- 微信/QQ 消息图：AstrBot 把聊天图缓存为 `/root/AstrBot/data/temp/media_...`；qzone 插件只允许 `AstrBot data 目录 / 系统 temp` 内的本地路径 + 公网 http(s)（禁 file://、私网/回环）。
- 审核：qzone 文本审核 + 配图审核都用当前模型 `deepseek/deepseek-flash`（V4.1 Flash，原生多模态，实测能读图，视觉通道 `provider.text_chat(image_urls=[...])` 正常）；配图审核复用 comfy 的 `moderation.judge`（只喂图片，不带上下文）。
- 9001 面板：本机 `http://127.0.0.1:9001/`；`GET /api/plug/<plugin>/<route>` 可直连真实插件 API（子代理可用它做实测，无需登录）。

## 3. 常用验证命令

- 手机 API：`POST http://192.168.137.242:6185/api/auth/login` → token；插件 API 路径 `/api/v1/plugins/extensions/<plugin>/<去插件名前缀的 route>`。
- 插件列表/版本：`GET /api/v1/plugins`（看 activated/version）。
- 插件配置读写：`GET /api/v1/plugins/config?plugin_id=...`；`PUT /api/v1/plugins/config` body `{plugin_id, config}`（保存后自动热重载）。
- 面板桥接实测：`GET http://127.0.0.1:9001/api/plug/astrbot_plugin_ncd_monitor/ncd-monitor/status-images?preview=1`（会真渲一张，注意频率）。
- 安装插件：打包 zip（排除 `__pycache__`/data/pyc）→ `python F:\Deepseek Harness WorkTable\_probe\install_zip.py <zip>` → 等 60~80s。
- 本地 Python：`C:\Users\KIRUSI\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`（PIL/requests 可用）。
- 同步/推送/打包：`python sync_to_repo.py` → 手动复制根文件 → `git add/commit/push`（Steam++ GitHub 加速需开启；github.com 直连会被 reset）→ `powershell -File 打包.ps1`。

## 4. 审查规范（输出要求）

- 只读审查：不要改现有文件；临时脚本可写 `F:\Deepseek Harness WorkTable\_probe\`。
- 输出中文，按严重度分组：高（功能错误/数据丢失/安全）、中（边界/并发/一致性）、低（建议）。
- 每条：**文件:行号、问题、触发条件、证据（跑过的命令/输出或代码路径）、修复建议**。
- 不要报告以下"已确认非问题"：
  - qzone 发布链路（get_cookies/发布/图片审核/ugc_right/频控预占）已实测 OK；
  - monitor 预览 `preview` 参数已修（返回 455×1200 缩略）；
  - `list_object_names` COMBO 兼容已实测 OK；
  - 插件页 bridge 端点合法性/面板桥接解包已修并验证；
  - status/monitor 预览路径穿越已防。
- 不要凭空说"已验证"，必须给出实际跑过的命令/输出。

## 5. 已修复清单（2026-11-19，不要重复报告）

- status 1.0.12：修改类操作先做管理员校验（之前普通成员可清空图库）；上传文件名清洗+图片校验；删除只限图库内文件；删除当前背景自动回退内置；settings 原子写 tmp 唯一。
- comfy 2.44.2：私聊禁止 `/主动回复 强制`；强制只清群级设置；私聊白名单先校验后落盘、tick/调试入口复核；`comfy_generate` 入口重置画风模板名；tips 原子写；跨天重置 noreply。
- monitor 1.7.84：预览 `preview` 从 query 读；页面 `r.images` 取值 + mime 修正；渲染串行锁；文件名微秒（datetime）；温度 chip 颜色按数值；上传/删除校验；删除当前背景回退；状态图 60s 会话冷却；schema 文案改 1 张。
- qzone 1.1.4：频控原子预占+失败回滚；图片只允许 base64/公网 URL（禁私网 SSRF）/data 目录内本地图；登录态失效清客户端缓存；失败记录 compact；terminate 关闭连接池；删死代码；vision-test 用 NamedTemporaryFile；文本审核 prompt 防注入；页面 error/tid 转义；visibility docstring 明确"留空=公开"。
- extra 1.4.9：linkd token 透传；公告先过滤屏蔽再截断。
- feedback 1.0.9：管理命令统一 `_is_admin`（副管理员可用）；清空写盘失败回滚并如实报错。
- linkd：`/run` 支持 token 校验（`_probe\linkd_run_token.txt` 存在才启用；手机 extra 配置 `ncd_pc_linkd_token` 同值）；启动并发互斥。
- comfy_watchdog：GGUF 清单抽检从每 10s 拉长到 5 分钟；异常/判断不了不再当作成功；重试后降频日志。
- panels.py：桥接 `{status:"error"}` 抛错；解包与官方父窗口对齐（有 data 键取内层）。

## 6. 已知未修（低优先，可继续跟进）

- 状态图页面切模式时会对每张图重复请求 preview（性能）；qzone 页面未做 preview 缓存。
- `comfy api_proactive` 是 GET 但有启动循环副作用（读接口副作用）。
- extra `/拉起向日葵` 别名（远程/向日葵）较宽，可能与普通对话冲突。
- feedback 备份轮转只在清空成功后执行（低风险）。
- status/monitor 背景图库没有数量上限/配额（可加）。

## 6. 全量审查（17 插件）修复轮 · 2026-11-19 晚

- H1 comfy：gallery/full|thumb|thumbs 的 dir 路径穿越已修（只允许 out 下单层目录 + 只服务图片后缀），实测 dir=../../.. 已返回 ok=false。
- H2 session：群聊 /切换 /命名 /删除 加管理员校验（主/副管理员或平台管理员；私聊不限）。
- H3 catchup：补台失败重试 3 次；心跳只在 _catchup_done 后推进处理点，停机窗口不再被吞。
- M1 natural：数据目录修正为 data/plugin_data/ncd_natural，旧 data/data/... 启动时一次性迁移。
- M2 memory：会话头像改为并发拉取（Semaphore 8 + 4s 总超时），冷启动不再 25s 超时。
- M3 toolbox：PDF 读取改为手动逐跳跟随 + 每跳 public_url_error 复查（防 302 内网 SSRF），临时文件 finally 清理。
- M4 toolbox：web_read_many 浏览器回退结果复查最终落点。
- M5 extra：确认这只是上次快照误报（token 配置一直在）；新增 linkd 403 的明确提示。
- M6 natural：多行回复不再只发前 4 行（超出部分合并一条），非文本组件（图/语音）补发。
- M7 inject：同一念头最多注入 3 次后自动出队，修“请求失败反复重复注入”。
- M8 inject：state.json 节流写盘（5s）+ 会话表保留最近 200 个。
- M9 toolbox：4 个工具提示词 handler 补注册路由（/ncd-toolbox/tools/*），实测 tools/info 返回 ok。
- M10 awake：非 Windows 平台初始化静默跳过；手机端配置 enabled=false。

**已知未修（低危）**：comfy 调试写盘后门（仅登录面板可用）、linkd 无传感器时周期性 spawn PowerShell 探温、watchdog 外挂盘缺失日志、memory 上限默认值不一致、附件/PDF 临时文件（PDF 已修）、natural _prev 兼容层死代码、toolbox per_page_chars 强转、natural _ensure_all_pack 重算。

## 7. 记忆插件替换（2026-11-19 晚）

- 旧 strbot_plugin_ncd_memory（链路记忆 1.2.1）已**停用**（数据仍保留在 data/plugin_data/ncd_memory，97 条已备份到 _backup/20261119_pre_memory_companion/）。
- 新 strbot_plugin_memory_companion（我会牢牢记住你 2.6.6，MIT，无运行期第三方依赖，SQLite 本地）已启用并接管记忆注入。
- 配置要点：
etrieval.embedding_enabled=false（AstrBot 里唯一 embedding provider 是 LM Studio 的 nomic-embed，电脑没开 → 走本地全文检索，不耗模型）；memory_summary.enabled=true（默认 provider，达 8 条事件/30 分钟触发一次总结）。
- 已通过其页面 API（/page/conversation-import/qq/preview + /start，CSRF 头）对三个好友的最近 31 天私聊做了历史导入：2384440471 狐悠（2 条记忆）、2493543208 卡拉彼丘-APERTURE（6 条）、2984423598（4 条），批次均 completed。
- 安装注意：插件运行期会读 docs/contracts/... 等资源，**不能精简 docs 目录**（之前裁掉导致 FileNotFoundError）。
- 面板入口：AstrBot 插件页 → 我会牢牢记住你 → 记忆面板。
