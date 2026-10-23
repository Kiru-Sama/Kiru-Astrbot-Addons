# 链路扩展（astrbot_plugin_ncd_extra）

补 AstrBot 原生没做的四个「对话层」快捷开关。全部只依赖 AstrBot 公开 API，
没有本机路径、没有密钥、没有私有服务，拿走就能用。

| 命令 | 作用 |
| --- | --- |
| `/compact [轮数]` | 把当前对话压成「摘要 + 最近 N 轮」，默认 5 轮 |
| `/tooldisplay on\|off\|status` | 工具调用气泡开关（`🔨 调用工具: xxx`） |
| `/token consumption` | 当前对话的 token 用量（累计 + 上下文估算） |
| `/get time [on\|off]` | 让模型拿到真实时间（每轮注入 `system_reminder`） |

---

## 1. `/compact` —— 压缩对话，保留最近 5 轮

```
/compact          # 保留最近 5 轮（可在配置里改）
/compact 8        # 这次保留 8 轮
```

**做了什么**：按「一条 user 消息 = 一轮」切分历史 → 早于最近 N 轮的部分丢给模型压成
一段摘要 → 写回数据库，变成：

```
[user]      【此前对话摘要】……
[assistant] 好的，我已了解此前的对话内容，我们接着继续。
[user]      最近第 1 轮……
[assistant] ……
……
```

几个刻意的选择：

* **摘要必须成对 user + assistant**。只塞一条 `system` 进去，多数 provider 中途会忽略它，
  模型等于失忆。
* **整轮保留，不切半轮**。最近 N 轮是原文，工具调用（`tool_calls` / `tool` 消息）跟着
  它所属的轮一起留下，不会出现「有 tool 结果但找不到 tool_call」的非法上下文。
* **太长就分批压**。单批喂给模型的上限是 `compact_chunk_chars`（默认 12000 字符），
  超了就分批摘要，再对分批结果做一次合并压缩（最多两级）。
* **摘要失败不退化成失败**：模型报错/超时（120s）时，退化成「只保留最近 N 轮」，
  并在回复里明确写「摘要生成失败」，不会把用户卡在报错上。
* **拦掉收益太小的压缩**：轮数 ≤ 保留轮数直接拒绝；少于 `compact_min_rounds`
  （默认 8 轮）也拒绝，并告诉你「真想压就敲 `/compact 7`」。
* **`token_usage` 归零**：那列是「上一次请求回报的总 token」，压缩后已经不成立了，
  留着会误导 `/token consumption`。

**限制**：只对本地 Agent（`agent_runner.runner_type == "local"`）有效。
用 Dify / Coze / DeerFlow 时历史在对方服务器上，会直接告诉你压不了。

权限：跟 AstrBot 的 `/reset` 一致（群聊要群管/管理员，私聊不拦）。

---

## 2. `/tooldisplay` —— 工具调用气泡

```
/tooldisplay on       # 之后每调一次工具先发一条气泡
/tooldisplay off
/tooldisplay          # 看状态
```

气泡样式对齐 AstrBot 原生：

```
🔨 调用工具: comfy_generate
   ⤷ 参数: {"prompt":"一只猫","width":1024}
```

* `tooldisplay_show_args`：带不带参数（默认带，截断 90 字）。
* `tooldisplay_show_result`：开启后改成「等工具返回再发一条」，带结果前 70 字
  （`tooldisplay_result_chars` 可调），和 AstrBot 原生的「显示工具调用结果」一致。

### 为什么是插件自己发，而不是改 AstrBot 的开关？

AstrBot 有 `provider_settings.show_tool_use_status`，但它在 **pipeline 初始化时**就被拷进
`InternalAgentSubStage.show_tool_use` 属性里了。运行时改配置文件**不会生效** —— 只有
WebUI 保存配置（走 `reload_pipeline_scheduler` 重建 pipeline）才行，而插件拿不到那个句柄。

所以本插件走 `on_using_llm_tool` / `on_llm_tool_respond` 钩子自己发消息：改完下一轮立即
生效，重载插件也不用。

**不会重复发**：如果哪天在 WebUI 里把原生 `show_tool_use_status` 打开了，本插件检测到
就自动让路（`status` 里也会标出来）。

权限：管理员（`admins_id`）。

---

## 3. `/token consumption` —— token 用量

```
📊 当前对话 token 用量
· 对话 cid-abcd… · 4 轮 / 8 条消息
· 上下文估算 ≈ 100 tokens（上一次请求回报 5,231）
· 累计 7 次请求：输入 15,000（其中缓存 3,000）｜输出 900｜合计 15,900
· 平均每次 2,271 tokens
· 模型 fake-model
· 太长就压：/compact
```

数据来源：

* **累计**：AstrBot 的 `provider_stats` 表，按当前 `conversation_id` 聚合
  （只算 `agent_type == "internal"` 的本地请求，和内置 `/stats` 口径一致）。
* **上下文估算**：按 AstrBot 自己的估算公式（中文 0.6、其他 0.3 token/字符）算现存历史，
  不是精确 tokenizer，只用来判断「要不要压」。
* **上一次请求回报**：`conversations.token_usage`。

表不存在或查询失败时，会降级成「累计用量：查不到」，其余照常显示。

---

## 4. `/get time` —— 让模型知道现在几点

```
/get time            # 看状态 + 当前时间
/get time on
/get time off
```

开启后每轮在系统提示词末尾追加：

```
<system_reminder>当前时间：2026-10-21 12:33:05（周三，Asia/Shanghai）</system_reminder>
```

* 时区取 AstrBot 主配置的 `timezone`（当前 `Asia/Shanghai`）；解析失败退回本机时区。
* AstrBot 自己也有 `provider_settings.datetime_system_prompt`，**开着的 话本插件自动让路**，
  不会注入两份。
* 同样的原因：原生那个值也是在 pipeline 初始化时读走的，运行时改不生效，所以这里自己注入。

---

## 配置项

`data/config/astrbot_plugin_ncd_extra_config.json`（也可在 WebUI 插件页里改）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `tooldisplay` | `false` | 工具气泡开关状态（`/tooldisplay` 也会写这里） |
| `tooldisplay_show_args` | `true` | 气泡里带参数 |
| `tooldisplay_show_result` | `false` | 等返回后发一条带结果的 |
| `tooldisplay_result_chars` | `70` | 结果保留多少字 |
| `compact_keep_rounds` | `5` | 默认保留最近几轮 |
| `compact_min_rounds` | `8` | 少于这么多轮不做压缩 |
| `compact_chunk_chars` | `12000` | 单次摘要请求最大字符（下限 2000） |
| `compact_summary_chars` | `1200` | 摘要目标字数 |
| `time_inject` | `false` | 时间注入开关状态（`/get time` 也会写这里） |

---

## 已知限制

* **`/compact` 之后的历史无法自动还原**。摘要是有损的 —— 想留原文，先备份
  `data/data_v4.db`，或者先在 WebUI 里复制一份对话。命令本身不提供撤销。
* **压缩期间不要同时让这个会话在跑 LLM**：AstrBot 的会话锁在 agent 阶段才加，
  命令处理不走那把锁，极端情况下可能互相覆盖历史。正常手动敲命令基本碰不到。
* **工具气泡的到达顺序**：NapCat 侧流式回复是「整段缓存、最后一次性发」，所以气泡会先到、
  正文后到。要「先正文后气泡」需要改成 AstrBot 原生的 break 分段机制，代价是要改它的配置并重启。
* **`/tooldisplay`、`/get time` 是全局开关**，不是按群/按人。需要按会话区分的话得再改。

---

## 自测

```
cd <AstrBot>\data\plugins\astrbot_plugin_ncd_extra
<AstrBot>\.venv\Scripts\python.exe 测试-扩展.py
```

76 项检查，覆盖：切轮/摘要拼装/分批、压缩的 5 种拦截、气泡的 4 种发送条件、
用量报告的字段、时间注入的让路逻辑、指令参数解析（含 `/tokenizer` 这类前缀不误抢）、
配置 schema 落盘读回。
