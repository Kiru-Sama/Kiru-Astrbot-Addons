# Kiru-Astrbot-Addons

给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 写的插件与补丁合集。都是先给自己用、再把私人配置剥干净后放上来的：
**能不碰上游源码就不碰**，必须动的地方单独记成补丁；每个结论都尽量附可复现的证据（日志、测试用例）。

## 更新记录

**2026-10-30（第三版：comfy 2.18.3 / toolbox 2.17.9 / session 1.0.3）**

- **群聊只发两条**：出图轮原来是「加载权重 + 开始采样 + 图 + 模型补一句」四条，
  群里刷屏感很强。现在群里只保留「开始采样」和「图+说明」；私聊保持不变。
  可用 `comfy_notify_group` / `comfy_image_drop_text_group` 单独开关。
- **修掉一个「命令像没执行」的严重 bug**（我自己引入的）：出图后 120 秒内，
  这个会话的任何回复都会被去文字钩子剥光 Plain，只剩引用组件，
  于是 `respond.stage` 判定「only Reply and At」直接跳过发送，
  `_has_send_oper` 保持 False，消息最终落到 LLM——表现为 `/reset` 之类命令
  毫无反应、只回一句聊天。现在改成 **事件内标记（`set_extra`）+ 只剥
  `is_model_result()`**，命令/系统结果永远不会被碰，也不再误伤后续消息。
- **8 步加速 LoRA 现在是强制的**（comfy 2.18.4）：步数 ≤12 时无条件插在 loras[0]、
  强度 1.0，风格 LoRA 串在它后面（叠加不替换），重复的会自动去重；
  步数 >12 则不加（高步数会被蒸馏 LoRA 带偏）。留 comfy_default_lora 为空可整体关掉。
  工具文案（comfy_tools / lora 参数 / docstring / 报错兜底）都写明了这条规则。
- **默认 LoRA 对所有预设生效**：原来写死 `preset_name == "sdxl"`，
  默认预设换成 moss 后，8 步加速 LoRA 再也没被加载过（图注显示「LoRA: 无」是对的）。
- **`comfy_tools` 不再一口气列出底模清单**（最多 20 条带大小），只报数量并提示
  按需调用 `comfy_models`，避免每次工具返回都挤占上下文。
- **`toolbox` 取 DeepSeek Key 时不再刷警告**：原来用 `get_provider_by_id("deepseek")`，
  而 `deepseek` 是 provider_sources 的 id，真实 provider id 是 `deepseek/deepseek-flash`，
  每次都打一条 "Provider deepseek was not found"。
- **`session` 回退**：曾试过自己实现一份 `/reset`，定位后发现内置命令本身没问题，
  已移除，避免同名命令重复触发。

**2026-10-30（第二版：monitor 1.7.25 / toolbox 2.17.8）**

- **收藏表情在 NapCat 上修好了**（此前"一张都没有"）。根因是 NapCat 的
  `fetch_custom_face_detail` 原样返回 QQ 原始对象（驼峰 `emojiId`），而插件只认
  `emoji_id`，于是把整份收藏判成空。现在两种命名都接受；另外 NapCat **没有**
  `send_custom_face`，发送时自动退回「按图片 URL 发出」。
- **监控面板在手机竖屏上不再被切掉右侧**。根因是 `.col-side` 的 `min-width:380px`
  在 1080 断点只改了 `width/max-width`、没重置 `min-width`，在 360 宽的屏上把整页顶宽。
- **时间戳加了两处显式说明**（面板顶部徽标 + 注入给模型的一行）：AstrBot 4.29 用
  `now(timezone.utc)` 写 `updated_at`，与 4.28 遗留在同一列的本地时间差 8 小时，
  表现是「刚聊过的会话显示成 8 小时前」。只是显示差异，排序不受影响。

### astrbot_plugin_ncd_feedback — 反馈通道

群友想要某种画风但没对应 LoRA、缺底模、或者有奇怪的小要求，都往这里丢：

`	ext
/反馈 画风 想要像素风的 LoRA        ← 普通群友
/反馈 想要 4 倍放大默认开           ← 分类可省略，归「其他」
/反馈列表 / /反馈处理 <编号>        ← 管理员（仅 ADMIN 权限）
`

bot 也会在合适时机自己调 eedback_submit 工具替你记一条。
所有反馈落在 data/plugin_data/ncd_feedback/items.jsonl，面板里可以筛选、
标记已处理/忽略、删除 —— 面板路径：插件页 eedback，聚合面板里的「反馈」标签。

配置项：eedback_max_items（保留条数）、eedback_allow_group（群里能不能用）、
eedback_categories（分类词表）、eedback_notify_admin（新反馈写 WARNING 日志；
★ 真正的私聊推送还没做）。

## 安装（简单）

这不是 AstrBot 本体的魔改版，不需要替换 AstrBot 源码：

1. 把需要的插件目录复制到 `AstrBot/data/plugins/`（解压后应直接看到
   `astrbot_plugin_ncd_xxx/main.py`，不要多套一层文件夹）。
2. 重启 AstrBot，或到 WebUI → 插件页重载。
3. 在插件页确认版本、按需填写配置。

`astrbot_plugin_ncd_patch` 是**可选**的源码补丁管理器；不装也能用基础功能。
配置在 `data/config/`，运行数据在 `data/plugin_data/`，升级插件时不要覆盖它们。

## 两个包：电脑版 / 手机版

插件代码是通用的，但**分发包**分两种，别装错：

| | 电脑版 | 手机版 |
|---|---|---|
| 文件名 | `AstrBot插件分享包.zip` | `AstrBot插件分享包-手机版.zip` |
| 插件数 | 11 个 | 9 个（去掉 comfy、awake） |
| 目标环境 | Windows / Linux 桌面 | 泡泡版 AstrBot（proot + Ubuntu 容器） |
| 源码补丁 | `源码补丁\` | `源码补丁-手机\P5-help.py.patch` |
| 安装说明 | 通用 | 手机专用（容器路径、保活、平台配置） |

**手机版为什么去掉两个插件**

- `ncd_comfy`：手机容器里没有 ComfyUI、没有 NVIDIA 显卡。实测在手机上调用会先回
  「已经交给后台画了」，后台随即报「LoRA 目录里没有文件」——承诺和结果是两回事。
  要出图得另配局域网远程 ComfyUI。
- `ncd_awake`：代码里写死 `sys.platform == "win32"`，手机上自动失效。

**手机版建议在 agent 辅助下安装**：要穿过 proot 容器、端口转发、源码补丁和保活设置，
手动容易卡在权限/路径上（`/sdcard` 在容器里是 `700 root:root`，电池和温度读不到）。

**monitor 自动适配两端**：`_is_mobile()` 读 `/proc/version` 里的 `proot-distro` 标记。
手机上 `/设备状态` 只报 CPU / 内存 / 存储 / 运行时长，不显示 GPU 和 ComfyUI 队列这些空指标；
电脑上保持原来的完整输出。

## 里面有什么

| 目录 | 插件 | 一句话 |
|---|---|---|
| `astrbot_plugin_ncd_monitor/` | 链路监控 | QQ / AstrBot / 本地模型 / 宿主机 四段链路面板，含上下文压缩摘要、并发输出、搜索统计 |
| `astrbot_plugin_ncd_toolbox/` | 链路工具箱 | 本机 Edge 搜索与正文抓取、批量并发读、收藏表情、头像、历史回溯、工具菜单 |
| `astrbot_plugin_ncd_patch/` | 链路补丁 | 上游源码补丁管理器：状态自检 / 手动应用 / 回滚，**绝不自动打** |
| `astrbot_plugin_ncd_extra/` | 链路扩展 | `/compact` `/tooldisplay` `/token` `/gettime`，外加 `extend_turns`、`read_attachment` |
| `astrbot_plugin_ncd_comfy/` | 链路本地生成 | 本地 ComfyUI 出图 + IndexTTS2 语音：预设工作流、LoRA/图生图、克隆音色、显存换手 |
| `astrbot_plugin_ncd_session/` | 链路会话 | `/会话` `/切换` `/命名` `/删除`（补 AstrBot 原生只有 `/new` 的缺口） |
| `astrbot_plugin_ncd_catchup/` | 链路补台 | 服务重开后，把停机期间没被回复的**私聊**补回来 |
| `astrbot_plugin_ncd_inject/` | 对话提示注入 | 面板里给指定会话塞一句“念头”，只在下一轮回复里生效一次，不单独发气泡 |
| `astrbot_plugin_ncd_awake/` | 链路醒着 | 面板一键阻止 Windows 空闲睡眠/熄屏，让 bot 常驻在线；不改电源计划 |

> 2026-10-22 起，绘图改走 ComfyUI（`ncd_comfy`），原先基于 diffusers 的独立绘图插件 `ncd_draw` 已从仓库移除。

## 链路监控（monitor）

- 面板：链路健康、上下文用量与压缩摘要、每次请求的完整上下文、并发输出并排展示
- 按会话类型分流人格：`persona_private` / `persona_public`
- 搜索/抓取统计卡片（数据由工具箱落盘）
- 新增卡片：本地生成（ComfyUI 节点进度 / 缩略图 / 队列位次）、工具调用、思考内容、事件流、审核明细
- `/设备状态`（所有人）：CPU / 内存 / GPU / 显存 / 温度 / 功耗 + ComfyUI 在线、模型、队列、进度
- 与本地模型互斥：只有本地模型才等 ComfyUI；云端 API 自动跳过

## 链路工具箱（toolbox）

- **搜索与抓取**：默认 DeepSeek 官方 Search/Fetch API（按次计费），默认不回退（失败直接报错），可开 `web_fallback_browser` 才用本机 Edge（CDP）；只回标题+网址，正文按关键词取窗口再总结
- **批量读**：`web_read_many(urls, query)` 一次并发读最多 4 个网页
- **收藏表情**：列表 / 预览 / 发送（分页、序号稳定、打乱顺序）
- **头像 / 历史**：头像只给模型看；历史回溯 30/50/70/90 条
- **工具菜单 + 沉浸模式**：收窄首轮工具数，剥掉机械工具说明
- **消息合并**：同一人 3 秒内连发合并成一条，只回一次；支持连续图片/图文混发（一次看到全部图）

## 链路本地生成（comfy）

- 预设工作流（SDXL / KREA2 / ANIMA / MOSS / TTS），参数注入、LoRA 链、图生图动态接线
- **ControlNet**：sdxl / anima / moss / krea2 四族；普通照片自动预处理（depth/pose/canny/lineart 等），Krea2 也支持 pose
- 文生图是主线；图生图分普通降噪（`reference`）和结构控制（`control`）两类，由 bot 按意图选
- 语音：IndexTTS2 克隆音色，支持情绪预设与自然语言情绪描述、停顿标记
- **显存换手**：自动判断模型来源 —— 本地模型才卸/释放，云端 API 不换手
- **自查回图**：出图后把图压到 1024px 一起回传给模型，让她能看见自己画的东西
- **群聊审核**：只在群聊里判，只有明确露三点才拦；被拦的图留 96px 小图在面板核对
- **反风控**：发送随机延迟、全局回复间隔、群聊/私聊分档的图片限流
- **按会话存图**：每会话一个目录，`comfy_output` 只取本会话，`reference=last` 也只认本会话
- 工具：`comfy_tools` / `comfy_generate` / `comfy_models` / `comfy_status` / `comfy_say` / `comfy_output` / `comfy_gallery`
- `comfy_prompt_help`：按关键词检索本地提示词法典（sfw/nsfw/all），写 prompt 前查 tag
- 命令：`/comfyui off|on|status`（只关自己拉起的进程）
- 绘图额度：`/排名` 看自己的额度/名次；`/全部排行`（仅管理员）看四榜前 10；额度面板可直接调阈值、给赞助者单独额度

## 链路扩展（extra）

- `/compact [保留轮数]`：把对话压成摘要 + 最近几轮
- `/tooldisplay`：工具调用气泡开关
- `/token`：当前对话 token 用量
- `/gettime`：让模型拿到真实时间
- 工具 `extend_turns(rounds=5)`：一轮里追加工具调用额度（需要配套源码补丁，见下）
- 工具 `read_attachment`：读消息/引用里的文件（txt / pdf / docx / xlsx / xls / zip 类），支持分段读

## 链路补台（catchup）

- 启动后检查一次：只补**停机期间**、历史末尾连续且后面没有机器人回复的私聊
- 同一人多条合并成一条；一次最多补 10 个；处理点落盘，不重复
- 首次运行只看最近 10 分钟；`data/state.json` 别删

## 对话提示注入（inject）

- 面板里选会话、填一句内容、点注入，**只在下一轮回复里生效一次**
- 实际文本固定为 `嗯，或许我可以{内容}`，不加标记
- 默认作为独立 assistant 上下文插入：不并进用户消息、不单独发气泡；回复后消费
- 旧念头会在下次注入前从上下文剔除，不累积
- 配置 `mode` 可切 `assistant`（默认）/ `user` / `system`

## 安装

1. 把插件目录复制到 `AstrBot/data/plugins/`
2. 重启 AstrBot（或在面板重载插件）
3. 面板里按需填配置

**ComfyUI 不在仓库里**（便携版解压就 7~8 GB）。需要出图/语音就自己下官方便携版：
<https://github.com/comfyanonymous/ComfyUI/releases> ，解压后把 `comfy_dir` 指过去即可。
语音还需装 `ComfyUI-Easy-IndexTTS2` 节点与 IndexTTS-2 模型。

其它要求：Edge（工具箱搜索）、`ffmpeg` 在 PATH（发语音）。

## 源码补丁（可选）

`astrbot_plugin_ncd_patch/patches/` 里是给 AstrBot 本体的几处小改动（内置指令限权、`/help` 列出全部指令、Agent 循环支持 `extend_turns`）。
补丁按「文件 + 校验值」匹配，AstrBot 升级后版本对不上会拒绝应用，不会硬改。

## 许可

见 [LICENSE](LICENSE)。
