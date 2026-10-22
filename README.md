# Kiru-Astrbot-Addons

给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 写的插件与补丁合集。都是先给自己用、再把私人配置剥干净后放上来的：
**能不碰上游源码就不碰**，必须动的地方单独记成补丁；每个结论都尽量附可复现的证据（日志、测试用例）。

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

> 2026-10-22 起，绘图改走 ComfyUI（`ncd_comfy`），原先基于 diffusers 的独立绘图插件 `ncd_draw` 已从仓库移除。

## 链路监控（monitor）

- 面板：链路健康、上下文用量与压缩摘要、每次请求的完整上下文、并发输出并排展示
- 按会话类型分流人格：`persona_private` / `persona_public`
- 搜索/抓取统计卡片（数据由工具箱落盘）
- 与本地模型互斥：ComfyUI 出图时挡住新的 LLM 请求

## 链路工具箱（toolbox）

- **搜索与抓取**：本机 Edge（CDP）真浏览器，不依赖搜索 API；只回标题+网址，正文按关键词取窗口再总结
- **批量读**：`web_read_many(urls, query)` 一次并发读最多 4 个网页
- **收藏表情**：列表 / 预览 / 发送（分页、序号稳定、打乱顺序）
- **头像 / 历史**：头像只给模型看；历史回溯 30/50/70/90 条
- **工具菜单 + 沉浸模式**：收窄首轮工具数，剥掉机械工具说明
- **消息合并**：同一人 3 秒内连发合并成一条，只回一次

## 链路本地生成（comfy）

- 预设工作流（SDXL / KREA2 / ANIMA / MOSS / TTS），参数注入、LoRA 链、图生图动态接线
- 语音：IndexTTS2 克隆音色，支持情绪预设与自然语言情绪描述、停顿标记
- **显存换手**：出图前 `lms unload --all` 卸掉本地模型，生成后 `POST /free` 释放显存
- 工具：`comfy_tools` / `comfy_generate` / `comfy_models` / `comfy_status` / `comfy_say` / `comfy_output`
- 命令：`/comfyui off|on|status`（只关自己拉起的进程）

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
