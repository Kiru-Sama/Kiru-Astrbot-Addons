# 链路监控插件（astrbot_plugin_ncd_monitor）

把四个环节的监控整合到一个页面，并显示**上下文压缩后的摘要文字**。

```
宿主机  →  LM Studio  →  AstrBot  →  SnowLuma(QQ)
```

位置：`<AstrBot>\data\plugins\astrbot_plugin_ncd_monitor\`

---

## 一、它显示什么

### 1. 链路健康（4 个灯）

| 灯 | 数据来源 | 说明 |
|---|---|---|
| SnowLuma（QQ 协议端） | HTTP 探测 `127.0.0.1:5099` | 顺带显示它连了几个 WS 客户端 |
| AstrBot（框架） | 插件自身 | 显示已运行时长 |
| LM Studio（模型） | `GET 127.0.0.1:1234/v1/models` | 显示加载了几个模型 |
| GPU | `nvidia-smi` | 显存 / 利用率 / 温度 / 功耗 |

### 2. 上下文用量条 ⭐

```
[██████████░░░░░░░░░░░░░░]  12,345 / 32,768  (37.7%)
              ↑ 82% 压缩触发线
```

- 绿 → 黄（≥60%）→ 红（≥82%）
- 红线位置 = AstrBot 的压缩触发阈值（`compressor.py` 里的 `0.82`）

### 3. 上下文压缩摘要 ⭐⭐ 这是重点

**显示压缩器实际产出的摘要文字原文。**

AstrBot 压缩上下文时，会调用一次 LLM 把旧对话总结成一段话，然后注入成：

```
user:      "Our previous history conversation summary: <摘要文字>"
assistant: "Acknowledged the summary of our previous conversation history."
```

本插件在**运行时包装** `LLMSummaryCompressor.__call__`，直接从它返回的消息列表里把这段文字抠出来展示。

每条记录还带：第几次压缩、发生时间、消息条数变化、省下多少 token。

**★ 还会判定这一次压缩到底有没有用**，没用就**红标 + 报警**。两种"没用"：

| 判定 | 什么情况 | 面板/日志 |
|---|---|---|
| `no_summary` | **压缩器没产出摘要** —— 总结那次 LLM 调用失败或返回空时，`compressor.py:282` 会**原样 `return messages`**，一个轮次都没替换。紧接着 AstrBot 就退化成「减半截断」 | 红框 + 红标「⚠ 这次没生效」+ 说明；日志打 `★第 N 次压缩【没有生效】：上下文被原样保留…` |
| `no_saving` | 摘要生成了，但 **token 没降**（`after >= before`）。通常是被保留的最近几轮本身就很大 —— 比如一轮里塞了十几张图 | 同上 |

**为什么值得单独做这个判定**：这两种失败**全程零报错**。日志里只有一行
`Compress completed. 13597 -> 13597 tokens`，面板上只看得出"压缩了一次"，
而实际上**白烧了一次完整的 LLM 调用**，下一轮还会再来一次。不主动判出来的话，
只有翻日志才能发现（这次就是这么发现的）。

> 措辞也特意按 verdict 分开：`no_summary` 时上下文是**被原样保留**的，
> 这时写 "X → Y token" 会自相矛盾（真实失败时两边本来就相等），只会把以后排查的人带偏。

#### ⚠️ 压缩空转：摘要写了、消息数没变、下一轮立刻又压一次

实测踩到过，值得单独记一笔。日志形状：

```
17:20:26  压缩流式收完：546 个 chunk / 965 字 / 107.0 秒
17:20:26  捕获到第 1 次上下文压缩，摘要 965 字，消息 41 → 9      ← 有效
17:22:34  压缩流式收完：546 个 chunk / 965 字 / 96.3 秒         ← 摘要一字不差
17:22:34  捕获到第 2 次上下文压缩，摘要 965 字，消息 12 → 12     ← 没减少
```

**两次摘要完全相同**说明第二次拿到的上下文和第一次是同一批内容。根因在
`compressor.py:190-204` 的保留预算：

```python
budget = max(1, int(total_tokens * self.keep_recent_ratio))   # keep_recent_ratio=0.15
for idx in range(len(rounds) - 1, -1, -1):
    if used > 0 and used + round_tokens > budget: break
    used += round_tokens
```

★ `used > 0` 这个条件意味着：**最新一轮无条件保留，不管它多大**
（面板上 `keep_recent_ratio` 的提示原文也写了「比例大于 0 时至少保留最后一轮」）。

于是当"最新一轮"里塞着几千 token 的工具结果（搜索 / 抓网页）时：

```
总量 7140  →  budget ≈ 1071
最新一轮 ≈ 3200 > budget，但 used==0 时被无条件收下 → 下一次循环立刻 break
⇒ 可被总结的只剩"最新一轮之前"的少量内容
⇒ 近千字的摘要去替换那点内容 → 总量不降反升
⇒ 下一轮仍然越线 → 立刻再压 → 每次烧掉一次完整 LLM 调用（实测 96~107 秒）
```

**缓解办法**（两处，都不在这个插件里）：

1. **别让单轮的工具结果太大** —— 这是主因。参见「链路工具箱」插件的
   `web_search_bing` / `fetch_page`：搜索只回标题+网址、抓正文先总结，
   把一次搜索的成本从 ≈3221 token 压到 ≈550。
2. **给压缩器一个长度约束** —— `agent_runner.config.compression.instruction`
   默认是空的，也就是**摘要长度完全没有约束**，实测在 700~1100 字之间飘且缓慢变长。
   面板「上下文压缩提示词」里写一句「摘要不超过 300 字」即可。

#### 压缩期间的「施工中」实时视图

压缩的时候模型会**暂时不回复**。以前只能靠 GPU 占用或上下文占用去猜
"它是不是正在压缩"，现在右栏那块**实时输出**会在压缩期间：

- 套一圈**黄色描边**（呼吸式脉冲）+ 标题变「⛏ 正在压缩中」
- **逐字显示压缩器正在写的摘要**，而不是等压缩完才一次性出现
- 压缩一结束，黄框**自动摘掉**，恢复正常样式

##### 为什么以前看不到

压缩器用的是 `provider.text_chat(contexts=...)`（`compressor.py:276`）—— **非流式**，
一次调用拿完整结果，中间过程没有任何可观测的输出。

##### 怎么做到不改 AstrBot 源码

压缩器只取 `completion_text`（`compressor.py:279`），
而 `text_chat_stream` 的 docstring 明确写着「**在生成的最后会返回一次完整的结果**」
（`provider.py:154`）—— 所以那次调用**换成流式是等价的**：

```
LLMSummaryCompressor.__call__   包装处：_COMPRESSING.set(True)（try/finally 保证复位）
        ↓
Provider.text_chat              包装处：看到标记 → 改走 text_chat_stream
        ↓                                 边消费边把 delta 写进实时缓冲
Provider.text_chat_stream       包装处：看到标记 → 实时块以 mode="compress" 开
        ↓
前端 /sysinfo 每秒拿到 live.mode === "compress"
```

★ 两个容易踩的点，都写在代码注释里了：

1. **`text_chat_stream` 的包装也会开实时块**。它如果不知道这是压缩，
   就会用 `mode="reply"` 把我刚开的压缩块**覆盖掉** —— 黄框永远不亮。
   所以那个包装也要读 `_COMPRESSING`。
2. **标记必须用 `ContextVar` 而不是模块级布尔量**。两个会话可能一个在压缩、
   另一个在主回复，全局量会串（压缩的流写进别人的实时块）。
   `ContextVar` 是每个任务一份，asyncio 里天然隔离。

##### 失败必须退回非流式

整段流式替换包在 `try` 里，**一旦流式抛异常就退回原来的 `text_chat`** ——
监控代码绝不能把被监控的压缩搞挂（和 `测试-压缩器不阻断.py` 是同一个原则）。
`测试-压缩流式.py` 用例 2 就守这条。

##### 前端为什么用 `outline` 而不是 `border`

加 `border` 会改变盒子宽度，套上去的瞬间整栏会**抖一下**。`outline` 不占布局空间。

### 3.5 并发与排队：两个状态胶囊 ⭐

实时输出那块标题右边，会在**真的发生**时才出现两个可点的小胶囊：

| 胶囊 | 什么时候出现 | 点它做什么 |
|---|---|---|
| **并发 N/M** | 同时在生成的会话 **> 1** | 切换到另一个会话的实时输出（副行会标「第 i/N 路」） |
| **排队 N** | 队列非空 | 展开/收起队列：谁在等、等了多久、排第几 |

没有并发、没有排队时**两个胶囊一个都不显示**。

#### 并排显示（多路同时生成时）

右栏会自动加宽（25% → 34%，左边相应窄一点），把每一路各占一列**并排**显示：
每列有自己的群名/昵称、状态（生成中 / 已发出 / 已生成完）、耗时、字数和正文。
生成中的那列自动跟到底部；已结束的保持你读到的位置（每秒重绘不跳回顶部）。
只有一路时还是原来的单栏大字，不分列。

#### 思考块（🧠）的展开行为

实时输出那块里，模型如果有思考过程，会在正文**上方**多一个折叠块：

- **生成中**：默认**展开**，能实时看它在想什么；
- **生成完**：默认**收起**（思考是内心活动，不该跟真正发出去的话抢视觉重量）；
- **★ 只要手动点过一次，之后一律听你的** —— 点开就一直开、收起就一直收，换代也不覆盖。

> 第三条是**修出来的**。原来只按"生成中 / 生成完"决定开合，而
> `/ncd-monitor/sysinfo` 是 **1 秒**轮询 → `renderLive` → `paintLive`，
> `paintLive` 每次都用 `body.innerHTML = ...` 把整块**重建** ——
> `<details>` 的 open 状态不会被浏览器保留，于是**「生成完后点开思考」
> 会在 1 秒内被下一拍收回**。
> 修法：把用户的选择记在 `_thinkUserOpen`（三态，`null` = 没表过态，才走默认）。
> 回归测试 `node 测试-思考块展开.js`（含"连续重绘 10 次仍保持"和负向对照）。
>
> 注：相同的坑在**消息卡片**上早就解决过（`_expanded` + `rememberToggle`），
> 思考块当时漏了。两者的区别在于缺省语义：消息卡片"不在集合里 = 关"，
> 思考块缺省要落到**默认行为**，所以是三态，不能直接复用那个集合。

> 顺带：面板里能看到思考**不等于**思考会进聊天。AstrBot 的
> `display_reasoning_text` 管的是聊天侧要不要显示，这里是监控侧的观察，
> 两者互不影响。

#### 滚动跟随（往上翻就停）

「最近一次输出」这块每 **1 秒**重绘一次，还会把 `innerHTML` 整个重建。规则：

- **默认跟随底部**（生成中能一直看到新字）；
- **用户往上滚 → 立刻松开**，之后重绘不再拽你；
- **滚回底部 → 重新咬合**；
- **新的一次生成开始 / 切换并发路 → 重新咬合**。

> 原来这段是坏的：判据写成 `atBottom || _liveWasAtBottom`，而
> `_liveWasAtBottom` 初始化 `true` 之后**全文再没被赋值过一次**，条件恒为 true
> —— 每秒把视口拽回底部，用户根本没法往上翻（"一直往下拽"那个报障）。
> 注释写的意图是对的（"不打断用户往上翻"），闩是死的。
>
> 现在用真正的闩 `_liveFollow`（容器上挂 `scroll` 监听按离底距离重新判定），
> 并且**不跟随时要把 `scrollTop` 还回去** —— 重建 `innerHTML` 会把位置打回 0，
> 只"不拽"是不够的，否则直接跳到最上面，一样没法看。
> 回归测试 `node 测试-滚动跟随.js`（含"往上翻后连续重绘 10 次稳住"和
> "没上翻过仍贴底"的负向对照 —— 少了后者，可能是"永远不跟随"的假通过）。

#### 为什么会有「并发」这回事

AstrBot **只有按会话的锁**（`session_lock.py` 的 `acquire_lock(umo)`，
在 `internal.py:220`）—— 同一个会话的消息会串行，但**不同会话是完全并行的**。

本地只有一个模型时，并行数一高会有两个后果：

1. 每次生成都被其他人抢算力，**每轮都变慢**；
2. 面板上原来那块实时输出是**全局唯一一份**，两个会话同时生成时
   B 一开口就把 A 那块**重置**掉，之后两人的字**逐字交替混在一起**。

所以做了两件事：

- **全局闸门**：最多 `max_concurrent_replies`（默认 **0 = 不限制**，即各走各的、不排队）个会话
  同时往下走，其余 **FIFO 排队**。插在 `OnWaitingLLMRequestEvent`（`internal.py:217`），
  这个事件的注释写着「等待调用 LLM（在**获取锁之前**，仅通知）」—— 位置正好。
- **实时输出按会话分路**：各攒各的，前端靠胶囊切换着看。

#### ★ 为什么默认是 1 而不是 2

因为**这台机器的上下文预算不支持两个请求同时进去**。实测：

```
LM Studio:  load_model: n_slots = 4, n_ctx_slot = 8192, kv_unified = 'true'
                                                     ↑ KV 池是【共享】的

18:16:51  会话 A 抓到上下文 #0 · 消息 27 条 · 图片 5 张     ← 5×765 图 ≈ 3800 token
18:16:52  会话 B 抓到上下文 #1 · 消息 12 条 · 图片 3 张     ← 3×765 图 ≈ 2300 token
18:17:10  A 报错：Context size has been exceeded
18:17:11  B 报错：Context size has been exceeded
```

两个各带几千 token 的请求同时进去，**合计顶穿**。而同样的模型此前
**单发 7140 token 是能过的** —— 所以这是"并发把预算分掉了"，
不是"上下文本来就超"。

`kv_unified='false'`（每个 slot 独立 KV）或上下文窗口明显更大时，
才适合把默认调大。调大前请先确认。

#### ★★ 闸门是 fail-open 的（这条最重要）

这道闸插在**回复链路正中间**，它出错就等于机器人哑巴。所以三条兜底：

| 情况 | 行为 |
|---|---|
| 闸门内部抛异常 | **直接放行** + WARNING（宁可并发超限，也不能不回复） |
| 排队等太久（`gate_hold_timeout`，默认 300s） | **直接放行** + WARNING |
| 某个会话的响应始终没回来、一直占着槽 | 下一条消息进来时**先回收僵死槽位**；面板每秒轮询 `/sysinfo` 时也会顺手回收一次 |

以上三条是**兜底**，不是常规路径。注意第三条的回收条件是
「占用时间 > `gate_hold_timeout`」—— 所以它给出的上限是 `gate_hold_timeout`
（默认 300 秒），**不是 1 秒**。这正是下面那个事故里哑了 5 分钟的原因：
常规释放点没触发，就只能等兜底。`测试-并发与多流.py` 专门守这几条。

#### ★★ 释放点有两个，而且释放必须幂等

这里踩过一次**可用性事故**，值得记下来：

```
18:17:10  两个会话各带几千 token 并发 → LM Studio 超限，请求失败
18:22:23  并发槽超过 300s 未释放，强制回收：…FriendMessage:100000002
18:22:23  并发槽超过 300s 未释放，强制回收：…GroupMessage:100000001
          ↑ 这 5 分钟里机器人是哑的
```

根因：**`on_llm_response` 在模型报错时根本不会触发** —— 于是唯一的释放点永远不执行，
槽位一直占着。上限是 1 的时候，一个泄漏的槽等于把机器人停掉。

修法是加第二个释放点：

| 释放点 | 什么时候触发 |
|---|---|
| `on_llm_response` | 拿到模型回复（正常路径） |
| `after_message_sent` | 回复真的发出去了 —— **模型失败时 AstrBot 会发一条错误消息给用户，这个钩子照样触发** |

两个释放点都会执行，所以**释放必须是幂等的**。这就是闸门用**令牌制**
（`acquire` 发一个 token、`release` 交回 token，交回不存在的 token 直接忽略）
而不是"按会话减计数"的原因 —— 减计数被调两次会把槽提前放掉，
令牌制下第二次调用找不到 token，天然无害。

#### ★ 令牌制与兜底回收的接缝（改这块时最容易错的地方）

令牌制引入了一个新的耦合点，**我自己就在这里漏过一次**：

```
① 会话 X 请求      → token 1，插件侧 _gate_tokens[X] = [1]
② 响应丢了         → 两个释放点都没触发
③ 超时兜底回收     → 槽位清了，但如果【没清 token 归属】，
                     _gate_tokens[X] 还是 [1]
④ 会话 X 新请求    → token 2，_gate_tokens[X] = [1, 2]
⑤ 新请求完成       → _release_gate 按 FIFO 取出【token 1】（陈旧的）
                     → release(1) 找不到槽位 → 空操作
                     → token 2 永远不被释放 → 【槽位又漏了】
```

也就是说**"被兜底救过一次的会话，下一次会再漏一次"**。所以回收僵死槽位时
必须**连带清掉那个会话的 token**（`reap_stale` 清 `_token_owner`，
调用方清 `_gate_tokens`）。`测试-并发与多流.py` 用例 5.8 守这条，
并且验证过：拆掉清理它就会失败（`x 新请求完成后 active=1（应为 0）`）。

另外这个测试套了 **60 秒整体超时**：它会真的建 task、真的排队，
万一"该释放的没释放"就会永久死等 —— 死等本身就说明闸门不对，
应该快速失败而不是让人白等。

#### 压缩请求不能被记成两条快照

让压缩改走流式之后，压缩那一次调用会**连着穿过两个包装**：
先从 `text_chat` 包装进（`_run_compress_stream` 就是从那儿出发的），
再进 `text_chat_stream` 包装。两边都抓的话面板上会同时出现**两条一模一样的
`compress` 快照**（实测同一毫秒两条）。所以 stream 包装在 `_COMPRESSING`
为真时**跳过捕获** —— 由 `text_chat` 那边抓、响应也挂在它身上。
`测试-流式捕获.py` 用例 10 守这条（且验证过：拆掉守卫它就会失败）。

想完全关掉并发限制：把 `max_concurrent_replies` 填 **0 或负数**。


### 4. 模型 / AstrBot 信息 + 宿主机资源

当前模型、模型能力（modalities）、上下文上限、**是否关闭思考**（`reasoning_effort=none`）、压缩策略与阈值、群聊上下文条数。

右边是**宿主机资源**：CPU / GPU / 显存 / 内存四条，**每秒刷新**，每条带一条 3 分钟迷你趋势曲线（固定 0~100% 量程，末端有点标出当前位置）。副行显示 GPU 型号、温度、功耗和显存/内存的绝对值。

#### 宿主机资源为什么单独走 1 秒的接口

占用率是「看瞬时抖动」的东西，混在 5 秒的 `tick()` 里会漏掉尖峰；但 `/overview` 要探测 LM Studio 和 SnowLuma，1 秒拉一次太重。
所以单开 `GET /sysinfo`，它**只读后端内存** —— 采样在后端按固定 1 秒节奏跑，前端只是把序列画出来。
附带好处：**刷新页面不会把曲线清零**，后端还留着最近 180 个点。

**★ GPU 为什么不能每秒 spawn 一次 `nvidia-smi`：**

```
实测 nvidia-smi 单次调用 ≈ 100 ms（8 次采样：131/89/111/97/96/90/94/107）
每秒一次 = 常驻吃掉约 10% 单核
```

所以改成一个**常驻的流式进程** `nvidia-smi -lms 1000`，它每秒自己吐一行，我们读 stdout：

```
NVIDIA GeForce RTX 5070 Ti Laptop GPU, 11127, 12227, 7, 57, 33.08
NVIDIA GeForce RTX 5070 Ti Laptop GPU, 11121, 12227, 9, 57, 33.87
```

实测间隔 1.01s / 1.01s，且只有一个进程。psutil 那侧只有约 **0.9 ms**，随便调。

`/overview` 里那份 `system` 会**优先复用采样器刚拿到的 GPU 值**，避免再 spawn 一次。

#### ⚠️ 三个坑（改这块之前一定要看）

**① `_system_info()` 不能是 `@staticmethod`。**
它现在要读 `self._gpu_latest`（复用采样器拿到的 GPU 值，省一次 `nvidia-smi`）。
在 `@staticmethod` 里写 `self.` 是 `NameError: name 'self' is not defined` ——
而这个异常发生在 `asyncio.gather` 的 executor 里，**整个 `/overview` 直接 500**，
页面顶上一条红字「拉取失败: Internal server error」，所有数据变成 `—`。

> 🔥 **这次真踩了**：改完之后我只测了新写的 `/sysinfo`（它好好的、73 个点、四条曲线都画出来了），
> 没测被顺手改动的 `/overview` —— 直到看到截图才发现。
> **教训：动了哪条路由，就把【所有】路由都跑一遍回归**，
> `测试-所有路由.py` 就是为此写的。

**② 取消「读 stdout 的任务」并不会结束子进程。**
`task.cancel()` 只是不再读管道，`nvidia-smi` 照跑 —— 不显式 `proc.kill()` 就是**孤儿进程**，每重载一次插件漏一个。
所以 `_gpu_stream_loop` 的 `finally` 和 `terminate()` 里都要 kill。

**③ `await` 一个被 cancel 的 task 抛的是 `CancelledError`，它继承自 `BaseException`。**
```python
try:
    await task
except Exception:      # ❌ 接不到 CancelledError！
    pass
```
这个坑很隐蔽：`terminate()` 会在这一行直接炸出去，**后面的 kill 全部跳过** —— 而日志上只看到一句 CancelledError，看起来像"正常取消"。
必须显式 `except asyncio.CancelledError: pass`。

**④ 「只抓 `contexts`/`system_prompt`/`prompt`」会漏掉 AstrBot 的自动注入。**
`_capture_text_chat` 里原来是
```python
if not contexts and not system_prompt and not prompt:
    return None          # ❌ 把"只有注入块"的请求整条丢掉
```
加注入捕获时我把守卫条件一起改了 —— 否则纯 @ + 空文本那种请求会被静默丢弃。
**这个 bug 是 `测试-自动注入捕获.py` 的用例 3 抓出来的**，不是我读代码看出来的。
教训：改了哪个抓取函数的**前置判据**，就补一个"只有新字段"的用例，
不然新字段永远是"看起来接上了、其实进不来"。

**怎么验证 terminate 真干净（别数总数）：**

```
基线 PIDs : [39016]            ← 正在运行的 AstrBot 里那个实例的，是【正常的】
采样后    : [39016, 20608]     ← 本次新建的
terminate : pid=20608 returncode=1，系统里只剩 [39016]
新增且残留: 无                  ✅
```

> ⚠️ **判据必须是「增量」而不是「总数」。**
> 我一开始用 `Get-Process nvidia-smi | Stop-Process` 清场再数总数，
> 结果把**活的 AstrBot 实例那个正常进程**也杀了 ——
> 它的重试逻辑 5 秒后又拉起来，于是读数忽 0 忽 1，白折腾两轮。
> 正确做法：记下基线 PID 集合，只看有没有**新增且残留**的。

#### 样本字段

```json
{"t": 1791694884.7, "cpu": 15.7, "mem": 88.7, "mem_used_gb": 27.7, "mem_total_gb": 31.2,
 "gpu": 8, "vram": 90.8, "vram_used": 11104, "vram_total": 12227,
 "gpu_temp": 58, "gpu_power": "33.61", "gpu_name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU"}
```

> `gpu_power` / `gpu_name` 是 `nvidia-smi` 的**原始字符串**，只当文本显示，不参与绘图。
> 第 1 个样本可能还没有 GPU 字段（`nvidia-smi` 第一行要 ~50ms 才到），
> 前端用**前值填充**而不是 `filter` 掉 —— 否则曲线的时间轴会被压缩、看起来像数据错位。

### 5. SnowLuma 端点安全巡检（已收进「其他」折叠区）

> 这块属于低频/次要信息，为了不占主流程的地方，**折叠进了页面底部的「其他」卡片**（默认收起）。

读 `onebot_<QQ号>.json`，列出每个监听端点的 `host`、`port`、**有没有 Token**。

如果发现「监听 `0.0.0.0` 且无 Token」会红字警告 —— 这是实际存在的安全风险（局域网内可无认证操作机器人）。

### 6. 最近的 LLM 请求表

每次请求的时间、**来源（群名 + 说话人昵称，或私聊昵称+QQ号）**、**消息内容预览 / 回复预览**、
输入/输出/缓存 token、**耗时（从发出到收到响应）**、调用了哪些工具。

主动回复（群里没 @ 它、它自己插的话）会打一个黄色「主动」标签。

### 7. 统计卡 + 上下文用量曲线

- **LLM 请求总数**（分群聊/私聊）
- **主动回复次数**
- **平均耗时 / 最长耗时**
- **累计 token**（输入 / 输出）

曲线：最近 120 次请求的上下文用量折线图，带 82% 压缩线；
**带压缩摘要的点用绿色标出**，一眼看出哪次触发了压缩。

> 曲线用**原生 Canvas** 画的，**不依赖 Chart.js 等外部库** ——
> 面板是受限 iframe，引外网 CDN 可能加载不出来。

---

### 8. ⭐ 发给模型的完整上下文（纵向聊天形式）

**这是最有用的一块** —— 把每次【真正发出去】的消息列表按聊天气泡纵向展开：

```
#42  10:52:31   🏠 示例群聊  👤 示例群友A 300000001   18 条  5,284 token  [web_search_tavily]
────────────────────────────────────────────────────────
系统提示（人设/规则）· 1,240 字                  [展开全部 ▾]
────────────────────────────────────────────────────────
📌 上一轮对话的压缩摘要（模型看到的原文）
用户在讨论软件更新体积问题，已对比 200m 和 12g。
────────────────────────────────────────────────────────
用户 / 群友  👤 示例群友A 300000001
不是说好更新两百m吗？怎么变成十二g了
📋 Current datetime: 2026-11-11 10:52 (CST), Weekday: Sunday
🧩 AstrBot 自动注入 · 412 字 · 并进这条消息发给模型        [折叠 ▸]
机器人
哈哈，估计是里面装了更多东西……
────────────────────────────────────────────────────────
↳ 回复：哈哈，估计是里面装了更多……  输出 27  缓存 200

#41  10:52:00  [🔻 上下文压缩请求]  24 条  3,000 token
    ...被压缩的旧对话（平铺）...
    📝 压缩器产出的摘要：用户在讨论软件更新体积问题...
```

**能看出什么：**
- **模型到底看到了哪些消息**（不是猜的，是实际发出去的）
- **第一条压缩摘要的原文**在哪、长什么样
- 每次请求带了**哪些工具**、**系统提示有多长**
- 回复是基于什么产生的（回答"回复覆盖"问题的依据）
- **🧩 AstrBot 自动注入块**：这段**不在 `contexts` 里** —— 是 AstrBot 在请求发出前
  自己塞进 `extra_user_content_parts` 的（「群聊上下文感知」就走这条），
  最终被**并进这条 user 消息**。只对群聊出现。机制见 §9 那一节

**设计细节：**
- `system` 提示很长 → 单独样式 + 默认折叠 + 「展开全部」
- 压缩摘要标记消息 → **绿色左边框**，一眼看到
- 消息**平铺**：没有气泡、没有左右对齐（早期版本试过气泡，空白太大、图看不清，弃用）
- **身份标牌**：请求头是 `🏠 群名` + `👤 昵称 QQ号`，每条用户消息的标题行也挂同一套
- **5 秒自动刷新不会把展开的内容收回去**（`_expanded` 记住展开的块）
- 滚动位置也会保持

#### 身份标牌（谁、在哪个群）

原来「谁说的」只藏在那行 `system_reminder` 里，而提醒被压成**一行**小灰字加省略号：

```
📋 User ID: 100000001, Nickname: 示例用户 · Current d…      ← 时间被截掉了
```

**Nickname 后面紧跟的就是 `Current datetime`**，一行截断正好把群名和时间全吃掉，
只剩个名字孤零零挂着，还看不出是哪来的、在哪个群。

所以拆成两步：**把身份抠出来单独显示，剩下的留给提醒行**。

来源有两处，格式还不一样，正则必须都认：

| 来源 | 格式 |
|---|---|
| `astr_main_agent.py:935`（`"\n".join`） | `User ID: N, Nickname: X`<br>`Group name: 群名`（受 `group_name_display` 开关控制）<br>`Current datetime: …` |
| `astrbot_message.MessageMember.__str__` | `User ID: N,Nickname: X` ← **逗号后没有空格** |

- 每条**用户消息**：`parseIdentity(m.reminder)` → 标题行右边挂 `👤 示例用户 100000001`
- 每个**请求头**：`👤` 用快照里的 `sender_name`/`sender_id`，
  `🏠` 用快照里的 `group_name`（来自 SnowLuma 身份库），**不依赖 AstrBot 那个开关**；
  群名没解析出来就退化成 `群 <群号>`，私聊显示 `💬 私聊`
- `stripIdentity()` 把身份从提醒里删掉，避免同一处显示两遍；
  删完为空的提醒行直接不渲染（省一行空间）

> 实测：7 个样例（标准两行 / 带群名三行 / 逗号紧贴 / 昵称 N/A / 只有时间 / 空 / undefined）
> 全部无重复、无残留，退出码 0。
> 复现：在本插件目录下 `node 测试-身份解析.js`（脚本抽的是 index.html 里**真实的**
> 函数，不是手抄副本，所以改了页面它就会跟着测）。

---

## 二、它怎么实现（关键设计）

### 压缩摘要的捕获 —— 不改任何源码文件

```
AstrBot 官方没有暴露"压缩完成"的事件钩子。
OnLLMRequestEvent 在压缩【之前】触发，抓不到压缩结果。
压缩器调用 provider.text_chat() 是直接调用，不经过事件系统。

所以本插件在运行时给 LLMSummaryCompressor.__call__ 包一层：
    original = cls.__call__
    async def patched(self, messages):
        result = await original(self, messages)   # 压缩照常进行
        ...从 result 里抠出摘要文字...              # 顺手记录
        return result

好处：
  ✅ 不修改任何 AstrBot 源码文件（升级不会覆盖）
  ✅ 插件卸载/禁用即自动恢复原样
  ⚠️ 依赖类名 LLMSummaryCompressor 和方法名 __call__；
     如果 AstrBot 大版本改了这两个名字，捕获会失败（但不影响主流程，
     只是页面上看不到摘要，日志里会有 [ncd-monitor] 包装压缩器失败）
```

### token 用量

- `@filter.on_llm_response` → `LLMResponse.usage`（input / output / cached）
- `@filter.on_llm_request` → `ProviderRequest`，用 `system_prompt + contexts + prompt` 粗估上下文用量

> ⚠️ 上下文用量是**估算值**（中文按 1 字≈1 token，英文按 4 字符≈1 token），
> 用于看趋势和比例，绝对值不必精确。压缩检测同理。

### 完整上下文的捕获 —— 包 Provider.text_chat（关键设计）

```
为什么不挂在 on_llm_request 上：

  OnLLMRequestEvent 在 internal.py 里触发时，真正的消息列表还在 agent runner
  内部（astr_main_agent.py 的 agent_runner.reset），还没组装好。
  实测：那个时机估出来的 token 数比真实值小一个数量级。

真正发出去的地方是：
  tool_loop_agent_runner.py:529
      payload = {"contexts": self._sanitize_contexts_for_provider(self.run_context.messages), ...}
      resp = await self.provider.text_chat(**payload)

所以本插件在运行时包装 Provider.text_chat。

额外好处：
  ✅ 上下文压缩也是调 provider.text_chat 完成的
     （compressor.py:276），所以压缩请求的【输入=被压缩的旧对话】
     和【输出=摘要】也一起抓到了。
  ✅ 压缩请求靠提示词里的 "Generate a summary of our previous
     conversation history." 识别，标记成 kind="compress"。
```

#### ★★★ 三个坑（改这块之前一定要看）★★★

**坑 1：只包基类不管用**

```python
Provider.text_chat = patched        # ❌ 没用
```

真正被调用的是**子类覆写**的版本：

```python
# openai_source.py:1179
class ProviderOpenAIOfficial(Provider):
    async def text_chat(self, ...):     # ← 实际走的是这个
```

基类的包装**根本不会被走到**。

**坑 2：插件加载时子类还不存在**

插件是在 AstrBot **启动过程中**加载的，那时 provider source 模块可能还没导入，
`Provider.__subclasses__()` 是空的。

```
实测日志：已包装 1 个 Provider 类的 text_chat: ['Provider']
                  ↑ 只有基类，抓不到任何东西
```

**修法（两层）**：

```python
# ① 先主动 import 所有 source 模块
from astrbot.core.provider import sources as src_pkg
for mod in pkgutil.iter_modules(src_pkg.__path__):
    importlib.import_module(f"{src_pkg.__name__}.{mod.name}")

# ② 并且改成【惰性补包】—— 每次 on_llm_request 前再遍历一次
#    （那时 provider 一定加载完了；遍历很便宜，而且幂等）
@filter.on_llm_request()
async def on_llm_request(self, event, req):
    self._ensure_providers_patched()      # ← 幂等，可反复调
    ...
```

**坑 3：`getattr` 会【继承】父类属性 ★ 最隐蔽**

守卫写成这样是错的：

```python
if getattr(cls, "_ncd_monitor_patched", False):
    return                              # ❌ 子类全被跳过
```

**基类 `Provider` 被包上后设了 `_ncd_monitor_patched = True`，
而子类会【继承】这个属性** → `getattr` 对每个子类都返回 `True` → 全部跳过。

实测现场：

```
预导入后: ['Provider','ProviderAnthropic','ProviderGoogleGenAI','ProviderOpenAIOfficial']
新包装  : Provider（只有它一个）
累计    : 1 个
```

**修法：只看类自己的属性，不看继承来的：**

```python
if cls.__dict__.get("_ncd_monitor_patched", False):
    return                              # ✅ 正确
```

#### 坑 4：类上的包装捕获了实例 → **重载后静默失效** ★★★ 最严重

前三个坑是"包装装不上"，这一个是"装上了，但数据写进了没人看的地方"。

包装写在**类**上，闭包却捕获了**实例**：

```python
original = cls.__dict__["text_chat"]
plugin = self                    # ← 闭包捕获【当前实例】
async def patched(self_, *args, **kwargs):
    sid = plugin._capture_text_chat(kwargs, args)     # ← 永远打到这个实例
```

而 `star_manager.reload()` 的流程是：

```
reload()  →  _terminate_plugin()     (star_manager.py:1041 → :1963)
          →  清空 star_map / star_registry
          →  load()                   → 【新建插件实例】
```

标记 `_ncd_monitor_patched` **只写不删**，所以新实例在 `__init__` 里
看到标记就直接 `return`、不再包装；而类上活着的包装仍把数据写进**旧实例**。

**后果链**：

```
新实例 B 注册了 6 条路由
  → 但 B 的 context_snapshots / summaries 永远是空的
  → /contexts 恒返回 0 条
  → 面板右栏「发给模型的完整上下文」空白、「压缩摘要」不再记录
  → 【零报错】。页面照常打开，看起来只是"还没抓到请求"
  → 只有重启 AstrBot 进程能恢复
```

> 🔥 **最难受的是它就藏在推荐路径上**：README 和安装说明都写
> 「改完点面板插件卡片的刷新图标重载」—— 走这条就会中招。

**修法（两道措施，互补）**：

```python
# ① 包装不捕获实例，改【模块级 _ACTIVE 派发】
_ACTIVE: "Main | None" = None

async def patched(self_, *args, **kwargs):
    plugin = _ACTIVE                       # ← 运行时取当前活着的那个
    if plugin is None:
        return await original(self_, *args, **kwargs)
    ...

# __init__ 末尾：把自己登记进去
global _ACTIVE
_ACTIVE = self

# ② terminate() 里还原包装，让"禁用即恢复原样"这句成立
#    （以前它其实不还原，是句假话）
def _unwrap_all() -> int:      # 模块级，记录也必须是模块级
    while _WRAPS:
        cls, attr, original = _WRAPS.pop()
        setattr(cls, attr, original)
        if "_ncd_monitor_patched" in cls.__dict__:
            delattr(cls, "_ncd_monitor_patched")   # ← 见下面的坑
```

> ⚠️ **记录 `_WRAPS` 必须放模块级，不能放实例上。** 包装是**类级别**的
> 全局状态；放实例上的话，重载时新实例的 `terminate()` 根本还原不了
> 旧实例打上的包装（它自己的 `_WRAPS` 是空的）。

> ⚠️ **清标记不能用 `cls.__dict__.pop()`。** 类的 `__dict__` 是
> `mappingproxy`（只读），对它 `pop` 会抛
> `'mappingproxy' object has no attribute 'pop'` —— 而那句在 `try` 里，
> 表现成"还原失败但标记还在"，新实例继续跳过包装。
> 必须用 `delattr`。**这个错只有真跑一遍才会暴露**，读代码看不出来。

**回归护栏**：`测试-重载失效.py`。

它**不能**靠"检查闭包捕获了谁"来判断 —— 修好之后闭包根本不捕获实例了，
那种检查会找不到东西、把"没捕获"误判成"已修复"，也就是**假通过**。
所以它走**行为验证**：真的调一次包装函数，看快照**落进了哪个实例**
（而且必须看**增量**，不能看"是否非空"，否则上一轮的旧实例会被算进来）。

#### `_WRAPS` 里还带着另外两个坑

**压缩器包装的前置估算原本裸在 `try` 之外：**

```python
async def patched(self_, messages):
    before_tokens = plugin._estimate_messages_tokens(messages)   # ← try 之外 ❌
    before_count = len(messages)
    result = await original(self_, messages)                     # ← 真正的压缩
    try:
        plugin._on_compressed(...)
    except Exception: ...
```

前置估算一抛异常，**真正的压缩 `original()` 根本不会被调用**，异常直接冒进
AstrBot 的压缩流程 —— 也就是**监控代码把被监控对象搞挂了**，性质比"少一块数据"
严重得多。（同一个文件里 Provider 包装的捕获是在 `try` 内的，两处保护级别不一致。）

**回归护栏**：`测试-压缩器不阻断.py`。手法是**在插件加载前**把
`LLMSummaryCompressor.__call__` 换成一个哨兵函数，于是包装把哨兵当成
`original` 捕获 —— 之后只要看哨兵有没有被调用，就知道"真正的压缩有没有执行"。

#### 坑 5：开了流式之后，主对话走的是 `text_chat_stream` ★ 会静默变空

`tool_loop_agent_runner.py:515-516` 按开关分流：

```python
if self.streaming:
    stream = self.provider.text_chat_stream(**payload)   # ← 绕过 text_chat 的包装
else:
    resp = await self.provider.text_chat(**payload)
```

所以**只包 `text_chat` 是不够的**：一旦打开 `provider_settings.streaming_response`，
面板右栏「发给模型的完整上下文」会直接变空 —— 而且**零报错**，
看起来和"还没抓到请求"一模一样。这就是报告里的 4.6。

**修法：两个属性都包，而且 `text_chat_stream` 的包装体必须是 async generator。**

```python
async def patched_stream(self_, *args, **kwargs):
    plugin = _ACTIVE
    if plugin is None:
        async for chunk in original(self_, *args, **kwargs):
            yield chunk                        # 原样透传
        return
    ...
    async for chunk in original(self_, *args, **kwargs):
        if _is_delta_chunk(chunk):
            plugin._live_append(_chunk_text(chunk))   # 旁边攒一份（供实时显示）
        else:
            final = chunk                             # 最后那个完整结果
        yield chunk                                   # ★ 原样透传
    ...
```

> ⚠️ **三条铁律**：不能吞异常；不能改变 `yield` 的节奏与顺序；
> 不能把 chunk 对象攒起来事后统一读（原因见下）。

**两个绕不开的细节（代码注释里也写了）：**

1. **增量文本在 `result_chain` 里，不在 `completion_text` 里。**
   `openai_source._query_stream` 把 `llm_response` 造成**一个对象反复复用**
   （`:631` 造、`:664` 每轮开头把 `completion_text` 清成 `""`、
   `:671` 把增量塞进 `result_chain`）。只读 `completion_text` 的话，
   每个增量片段拿到的都是空串。
   —— 同一个事实还有个后果：**不能攒对象**，攒到最后大家是同一个对象，
   必须在 `yield` 的那一刻就把文本抠出来。

2. **最后那个"完整结果"不参与累积。**
   依据 `LLMResponse.is_chunk`（增量 `True` / 完整 `False`），
   `provider.py:154` 的 docstring 也写明了「在生成的最后会返回一次完整的结果」。
   把它也当增量累加，缓冲里的文字会**翻倍**。

3. **收尾必须写在 `finally` 里，不能写在 `async for` 之后。** ★ 真踩过
   消费者可以**提前 `return`** 或调 `aclose()`，这时会在 `yield` 处抛
   `GeneratorExit`，**循环后面的代码一行都不会执行**：

   ```python
   # tool_loop_agent_runner.py:515-527
   stream = self.provider.text_chat_stream(**payload)
   try:
       while True:
           resp = await self._await_or_stop(anext(stream))
           ...
           if resp is None:
               return                    # ← 提前退出
           yield resp
   finally:
       await self._close_executor(stream)   # ← 把生成器关掉 → GeneratorExit
   ```

   表现就是**「模型明明回了、QQ 也收到了，面板上这条却没有回复」**。
   （这个问题是用户反馈后查出来的：日志显示 23:11:03 抓上下文、23:11:45 才
     `Prepare to send`，回复确实产生了。）修法：

   ```python
   try:
       async for chunk in original(self_, *args, **kwargs):
           ...
           yield chunk
   except GeneratorExit:
       st["closed"] = True
       raise                    # ★ 绝不能吞掉，也绝不能再 yield
   except Exception:
       plugin._mark_snapshot_error(sid)
       raise
   finally:
       _finish()                # ← 挂响应 + 结束实时缓冲 + 打诊断，都在这儿
   ```

   **回归护栏**：`测试-流式捕获.py` 的用例 8（消费两个 chunk 后 `aclose()`，
   断言响应仍被挂上、诊断日志标出「被提前关闭」）。它**自带自检** ——
   用旧写法跑同样场景，断言必须是 False，否则说明这条用例测不出东西。

**回归护栏**：`测试-流式捕获.py`（8 个用例：透传同一性 / 累积正确 / 不重复累加 /
异常原样抛 / 快照挂上真实 token / **常驻不消失** / 空流 / **提前关闭也不丢回复**）。

#### 日志特征（一眼判断 R2 有没有生效）

加载后每次请求前会补齐，日志里应该逐个子类都有记录：

```
[ncd-monitor] 预导入 provider source: 成功 41 个，失败 3 个: [...]
[ncd-monitor] 新包装 Provider 子类: Provider
[ncd-monitor] 新包装 Provider 子类: ProviderAnthropic
[ncd-monitor] 新包装 Provider 子类: ProviderGoogleGenAI
[ncd-monitor] 新包装 Provider 子类: ProviderOpenAIOfficial
```

> ⚠️ **3 个失败的 source 模块是正常的**：
> `edge_tts_source` / `sensevoice_selfhosted_source` / `whisper_selfhosted_source`
> 都是 TTS/STT，缺依赖，和聊天 provider 无关。

#### 代价

```
⚠️ 依赖 Provider.text_chat 这个方法名；AstrBot 大版本改名会失效
   （不影响主流程，只是页面上看不到上下文）
✅ 插件卸载/禁用即恢复原样，不改任何源码文件
```

### 文本清洗 —— 拆开，而不是硬塞

**AstrBot 往上下文里塞的占位文本，一条带图消息有 3 个 text part：**

```
[0] text        "[图片]"
[1] text        "[Image Attachment: path <AstrBot>\data\temp\media_image_20261011103041287_38a1.jpg]"
[2] text        "<system_reminder>User ID: 100000001, Nickname: 示例用户, 2026-11-11 10:30 (CST), Weekday: Sunday</system_reminder>"
[3] image_url   "data:image/jpeg;base64,..."
```

**`_split_content()` 把一条消息拆成三个独立字段，而不是拼成一坨：**

| 字段 | 内容 | 前端怎么显示 |
|---|---|---|
| `content` | 真正的正文 | 正常气泡文字 |
| `reminder` | `<system_reminder>` 里的东西 | **一行小灰字**，悬停看全文 |
| `images` | `[{"k": 缓存键, "n": 文件名, "q": 是否引用图}]` | **缩略图**（引用图加橙框） |

**实测：正文从 243 字符降到 6 字符。**

```
修复前（挤在一坨）:
  [1 张图] [图片][Image Attachment: path F:\...\media_image_...jpg]<system_reminder>User ID: ...</system_reminder>

修复后（拆开）:
  content   = '这是我截的图'
  reminder  = 'User ID: 100000001, Nickname: 示例用户\nCurrent datetime: ...'
  images    = [{'k': '45d054a21742c62a', 'n': 'media_image_....jpg', 'q': False}]
```

> **为什么 `system_prompt` 开头那个换行很要命**：实测它是
> `'\n# Persona Instructions\n\n我是群猫娘「小助手」...'` —— 以换行开头。
> 配上 `white-space: pre-wrap`，那几行空白就占满了截断区域，
> 结果「显示不全 + 大片空白 + 点了展开还是空白」。

### ⭐ 引用的图片，模型到底看不看得见？

**结论：看得见。** 而且**正文里出现那段标记，本身就是"图进去了"的证据。**

引用一张图时，上下文里长这样（没有 `image_url` part，只有文字）：

```
[Image Attachment in quoted message: path F:\...\media_image_20261011124118103_cfb8.jpg]<Quoted Message>
(示例群友A): [Image]
</Quoted Message>
```

看到这段文字很容易以为"模型只拿到一个路径字符串"，其实**不是**。证据链（全部可回查）：

| # | 位置 | 事实 |
|---|---|---|
| ① | `astr_main_agent.py:1446` | 引用组件是 `Image` 时 → `req.image_urls.append(image_path)` |
| ② | `astr_main_agent.py:1512` | 兜底路径（引用链只剩 `[Image]` 占位符时，本截图就是这种）→ **也** `req.image_urls.append(image_ref)` |
| ③ | `astr_main_agent.py:1458` / `:1514` | 那句 `[Image Attachment in quoted message: ...]` **紧接着** append 之后才写 |
| ④ | `astr_main_agent.py:1442-1444` | 取图**失败**时写的是 `[Image unavailable]`，不是上面那句 |
| ⑤ | `openai_source.py:1404-1408` | `assemble_context()` 把 `req.image_urls` 逐个变成 `{"type":"image_url"}` 内容块 |

**所以：③ 那句标记一旦出现，就说明 ①② 已经成功把图塞进了 `req.image_urls`，
再经 ⑤ 变成真正的图片块发给模型。** 反之只会看到 `[Image unavailable]`。

本机配置也满足前提：
- 启用中的 provider `modalities` 含 `"image"`（`cmd_config.json:110-113`）
  → `sanitize_context_by_modalities` 不会把图剔掉
- `default_image_caption_provider_id` 为空（`:123`）
  → 不会把图转成文字描述
- `max_quoted_fallback_images = 10`（`:153`）→ 兜底提取的上限

> ⚠️ **为什么面板上看不出来**：图片本身在 `req.image_urls` 里，**不在消息 `content` 里**，
> 而那句标记在 **`req.extra_user_content_parts`** 里（见下）。
>
> 🔥 **这里我先前写错了一版，必须记下来。** 我当时以为标记在消息 `content` 里，
> 于是把 `_QUOTED_IMG_RE` 接在 `_split_content()`（处理 content）上，
> 还写了"已修：引用图照样给缩略图、带橙框"。**结果是假的 —— 那条路根本看不到这个标记。**
> 用户实报"引用图没送到模型"，查下来才发现：
>
> | 标记 | 产生处 | 写进哪里 |
> |---|---|---|
> | `[Image Attachment: path …]` | `astr_main_agent.py:1402-1404` | **`extra_user_content_parts`** |
> | `[Image Attachment in quoted message: path …]` | `astr_main_agent.py:744-747` | **`extra_user_content_parts`** |
> | （真正进模型的是）| `:1388` / `:1446` / `:1512` | `req.image_urls` |
>
> **两种标记都只在 extra parts 里**，`content` 里一个都没有。
> 所以"只在 content 里找标记"在 4.28.2 上等于什么都没找。
>
> **修法**：抽出一个共用 `_extract_images(raw)`，**content 和注入块两条路都过一遍**，
> 挖出来的图片合并（按路径 hash 去重）挂到那条消息上，并计入 `image_count`。
> 这样就不再依赖"标记到底躺在哪个参数里"这个会随版本变的细节。
>
> ⚠️ 顺带纠正一个说法：**模型一直是收到了引用图的**（`req.image_urls` → `assemble_context`
> 变成真正的 `image_url` 块）。用户看到的"没送到"其实是**面板没显示**。
> 判别依据依然是那句标记 —— 它只在 `req.image_urls.append()` 成功之后才写。

**两个正则不会互相误伤**（已实测）：
`[Image Attachment:` 后面直接是 `:`，而引用版是 ` in quoted message:`，互不匹配。
两种标记同时出现时也能各自只抓自己的路径、清洗后无残留，
且引用的**文字内容**（`(示例群友A): [Image]`）不会被误删。

**回归护栏**：`测试-自动注入捕获.py` 的用例 5（引用图 + 直发图标记都要挖出来、
挂到消息上、计入 `image_count`、登记缩略图索引），并且**自带自检** ——
把 `_extract_images` 换成空操作后必须挖出 0 张，否则说明这条断言测不到提取逻辑。

### 图片缩略图 —— 为什么不能直接内嵌原图

**实测数据：**

```
data\temp 下 524 个图片文件，合计 294.6 MB，平均 576 KB
最大的一个 gif 有 1.7 MB

24 张图内嵌 base64 ≈ 18 MB / 每次请求  → 页面直接卡死
```

**所以服务端先缩到 96px：**

```
原图 158 KB  →  缩略图 2.2 KB    （缩小 72 倍，生成耗时 24 ms）
缓存后再次读取：0.00 ms
```

**实现：**

```python
THUMB_SIZE = 96
THUMB_QUALITY = 70

def _make_thumb(self, key, path):
    # ① 磁盘缓存命中直接读（<data/temp> 的原图可能被 AstrBot 清掉，
    #    但缩略图留在插件目录里，所以历史快照的图还能看）
    # ② 未命中就用 PIL 缩，写到 <插件>/thumbcache/<hash>.jpg
    return "data:image/jpeg;base64,...."
```

**前端按 hash 缓存在 JS Map 里** —— 同一张图无论在多少个快照里出现，都只向后端要一次。
并发限制 4，避免一次打 24 个请求。

#### ⚠️ 为什么不用 `<img src="/api/...">`（试过，行不通）

插件页面跑在 **iframe** 里，凭证由父窗口持有、靠 `postMessage` 转发
（`dashboard/plugin_page_bridge.js` 里**根本没有 token**）。
`<img src>` 是 iframe 直接发起的请求，走不到那个桥。

**实测：**
```
GET /api/v1/plugins/extensions/astrbot_plugin_ncd_monitor/ncd-monitor/overview
    → HTTP 401
GET .../page/monitor
    → HTTP 401
```

**结论：只能把图片当数据、走桥传。**

#### ⚠️ 缓存键必须白名单化（`key` 会拼进文件路径）

`/thumb/<key>` 的 key 直接参与拼路径：

```python
def _thumb_file(self, key):
    return os.path.join(self._thumb_dir, f"{key}.jpg")     # ← key 是调用方给的
```

而 `api_thumb` 原本只校验「非空 + `len<=64`」，`_make_thumb` 又是
**先按客户端给的 key 读缓存、再查有没有登记过**。于是：

```
key = "..\secret\shot"
  → <插件目录>\thumbcache\..\secret\shot.jpg
  → 命中缓存分支，直接返回该文件的 base64
```

**这是「已认证的任意 `.jpg` 读取」**（需面板凭证、需知道目标路径，
不是匿名 RCE，但不该存在）。而且 `os.path.join` 遇到**绝对路径**会整个替换掉
前缀，`C:\...\x` 这种 key 也一样能打。

**修法两层**：

```python
# ① 字符集白名单 —— 合法键只可能是 sha1(path) 的前 16 位十六进制
_THUMB_KEY_RE = re.compile(r"[0-9a-f]{16}")

if not self._THUMB_KEY_RE.fullmatch(key):
    return json_response({"error": "bad key"}, status_code=400)
```

```python
# ② _make_thumb 里【先查登记、再碰磁盘】，并加 realpath 前缀校验做纵深防御
if not path or not os.path.exists(path):
    return None                                    # ← 未登记就直接返回
cache = self._thumb_file(key)
real_cache = os.path.realpath(cache)
if os.path.dirname(real_cache) != os.path.realpath(self._thumb_dir):
    return None                                    # ← 越界就拒，且【不进入生成分支】
```

> ⚠️ realpath 那道校验必须 `return`（而不是继续往下走），
> 否则虽然读不到，却会**往缓存目录之外写**缩略图。

**回归护栏**：`测试-缩略图越界.py`。它带一个**正对照** —— 先证明那条越界路径
**确实能解析到目标文件**，再证明请求被挡下来了。没有正对照的话，
万一路径根本没拼对，"被挡住了"就什么都没验证到。

### 折叠 —— 用浏览器原生 `<details>`，不自己写 JS

**之前的做法（错误）：**
```html
<div class="bubble clip">...<button onclick="toggleClip(this)">展开</button></div>
```
自己用 `classList.toggle("clip")` + `max-height` + 渐变遮罩来控制。
问题：**要跟 5 秒自动刷新抢状态**，太脆，而且内容会被渐变切掉、下面留白。

**现在的做法：**
```html
<details class="sysblock" data-key="3:0" ontoggle="rememberToggle(this)">
  <summary>系统提示 · 1240 字  <span class="hint">展开 / 收起</span></summary>
  <div class="txt">...</div>
</details>
```

- **点击开合由浏览器负责** —— 必然生效，不依赖我的 JS
- `ontoggle` 只做一件事：**把开合状态记进 `_expanded`**，这样刷新后能保持
- **普通气泡默认全文显示**（实测消息大多 77~226 字符，根本不需要折叠）
- 只有超过 **1200 字**的才折叠


### 压缩摘要的捕获 —— 两层保险

1. **包装 `LLMSummaryCompressor.__call__`** —— 拿压缩前后的消息条数、省下多少 token
2. **包装 `Provider.text_chat`** —— 拿压缩请求的输入（旧对话）和输出（摘要原文）

### ⭐ 让图片的 token 开销参与压缩判断（第三个运行时补丁）

**这是三个补丁里最"必要"的一个** —— 不加的话图片永远清不掉。

#### 问题：AstrBot 的两段代码互相打架

**① `EstimateTokenCounter` 本来是算图片的：**

```python
# core/agent/context/token_counter.py
IMAGE_TOKEN_ESTIMATE = 765      # 一张 1024x1024 图的开销（= 85 + 4×170，OpenAI 算法）
AUDIO_TOKEN_ESTIMATE = 500
# 注释：「这里取一个保守中位数，宁可偏高触发压缩也不要偏低导致 API 报错。」

elif isinstance(part, ImageURLPart):
    total += IMAGE_TOKEN_ESTIMATE       # ← 确实会算
```

**② 但 `count_tokens()` 开头一句短路把它绕过去了：**

```python
def count_tokens(self, messages, trusted_token_usage=0):
    if trusted_token_usage > 0:
        return trusted_token_usage      # ← 直接返回，765 的估值一行都跑不到
```

**而这个 `trusted_token_usage` 只含文本：**

```
tool_loop_agent_runner.py:816
    token_usage = self.req.conversation.token_usage     # 上次响应的 usage.total
    → context_manager.process(messages, trusted_token_usage=token_usage)

tool_loop_agent_runner.py:870
    self.req.conversation.token_usage = llm_response.usage.total
```

**实测**：60 条消息含 **24 张图**，LM Studio 报的 `prompt_tokens` 仍然只有 **~7,500** ——
图片走视觉编码器，**不进 `prompt_tokens`**。

#### 后果

```
图片不断累积
    ↓ 视觉编码负担越来越重（每次请求变慢）
    ↓ 但 usage.total 不涨
    ↓ 永远到不了 82% → 压缩永远不触发
    ↓ 图片无限堆积
```

**而且面板上看不出任何征兆** —— 曲线不涨、压缩次数一直是 0。

#### 修法：估算值和 API 值取较大者

```python
original = EstimateTokenCounter.count_tokens

def patched(self_, messages, trusted_token_usage=0):
    estimated = original(self_, messages, 0)          # 强制走估算（含图片 765/张）
    if trusted_token_usage > 0:
        return max(trusted_token_usage, estimated)    # 取大的
    return estimated

EstimateTokenCounter.count_tokens = patched
```

**这正好实现了 AstrBot 注释里写的"宁可偏高"**：

| 场景 | API 值 | 估算值 | 取 | 结果 |
|---|---|---|---|---|
| 纯文字 | 7500 | 1472 | 7500 | 精确，不受影响 |
| 12 张图 | 7500 | 10786 | **10786** | 开始反映图片 |
| 24 张图 | 7500 | 20110 | **20110** | 61.4% |
| 40 张图 | 7500 | 32542 | **32542** | 🔴 触发压缩 |

#### 实测数据（离线模拟，压缩线 = 26869）

```
 张图数   现状（用 API 值）      补丁后（取 max）
   0     7500  (22.9%)  ⚪      7500  (22.9%)  ⚪
   5     7500  (22.9%)  ⚪      7500  (22.9%)  ⚪
  12     7500  (22.9%)  ⚪     10786  (32.9%)  ⚪
  24     7500  (22.9%)  ⚪     20110  (61.4%)  ⚪
  40     7500  (22.9%)  ⚪     32542  (99.3%)  🔴
         ↑ 永远不变
```

**单张图 = 765 token，约 35 张图就能单独推过压缩线。**

#### 代价

```
⚠️ 依赖 EstimateTokenCounter.count_tokens 这个方法名；
   AstrBot 大版本改名会失效 —— 失效后只是回到"图片不计入压缩判断"
   的老行为，不影响主流程。检查方法：看日志有没有
       [ncd-monitor] 已包装 EstimateTokenCounter.count_tokens
✅ 插件禁用/卸载即恢复原样
```

### 三个运行时补丁一览

| # | 包装的目标 | 作用 | 失效后果 |
|---|---|---|---|
| 1 | `LLMSummaryCompressor.__call__` | 抓压缩前后的消息条数与省下的 token | 摘要统计缺失 |
| 2 | `Provider.text_chat`（**4 个类都要包**） | 抓真正发给模型的完整上下文 | 右侧栏空白 |
| 3 | `EstimateTokenCounter.count_tokens` | 让图片开销计入压缩判断 | 图片永远清不掉 |

**共同点**：都不修改任何 AstrBot 源码文件，插件禁用即恢复；
**共同风险**：依赖 AstrBot 内部类名/方法名，大版本升级后要重新确认。

### 后端 API

| 路由 | 作用 |
|---|---|
| `GET /<插件目录名>/ncd-monitor/overview` | 页面全部数据（一次拿完，含 `sessions` 各会话占用） |
| `GET /<插件目录名>/ncd-monitor/history` | 上下文用量序列 + 最近请求 |
| `GET /<插件目录名>/ncd-monitor/summaries` | 压缩摘要列表 |
| `GET /<插件目录名>/ncd-monitor/contexts` | ⭐ 每次发给模型的完整上下文 |
| `GET /<插件目录名>/ncd-monitor/thumb/<key>` | 图片缩略图（96px，base64） |
| `GET /<插件目录名>/ncd-monitor/sysinfo` | 宿主机资源最新值 + 趋势序列（**前端 1 秒拉一次**） |

> 调用顺序：**先拉 `/contexts`（它会登记 hash → 原图路径），再拉 `/thumb/<key>`。**
> 反过来 `/thumb` 会 404，因为还没登记。

#### ⚠️ 路由参数必须用尖括号 `<key>`，而且 query 传不进来

**三个坑，都会静默失败：**

**① AstrBot 只把【路由捕获的 path 参数】传给 view_handler：**

```python
# dashboard/api/plugins.py:217
return await run_maybe_async(lambda: view_handler(**path_values))
#                                        ↑ 只有 path_values，query 参数【根本不传】
```

所以 `view_handler(self, **kwargs)` 里用 `kwargs.get("k")` 读 query **永远是空** ——
函数照常被调用、返回 400，前端 `.catch()` 一吞，**页面上什么都看不出来**。
（实测就是这样：日志显示"图片 21 张 · 缩略图索引 20 个"一切正常，
  但 `thumbcache` 目录死活不出现。）

**② 路径参数只有尖括号写法才会生成捕获组：**

```python
# dashboard/api/plugins.py:157
re.finditer(r"<(?:(path):)?([A-Za-z_][A-Za-z0-9_]*)>", normalized)
```

| 写法 | 转出的正则 | 结果 |
|---|---|---|
| `/thumb/<key>` | `/thumb/(?P<key>[^/]+)` | ✅ 能用 |
| `/thumb/{key}` | `/thumb/\{key\}` | ❌ 字面量，匹配不上 |
| `/thumb/:key` | `/thumb/:key` | ❌ 字面量，匹配不上 |

**实测：**
```
/astrbot_plugin_ncd_monitor/ncd-monitor/thumb/45d054a21742c62a
    -> ✅ 匹配，参数 {'key': '45d054a21742c62a'}
/astrbot_plugin_ncd_monitor/ncd-monitor/thumb
    -> ❌ 不匹配
```

**结论：带参数的插件 API 一律写成 `/xxx/<name>`，函数签名用 `async def handler(self, name="")`。**

> ⚠️ **路由必须带插件目录名前缀！**
> 前端桥接 SDK 拼出来的路径是
> `/api/v1/plugins/extensions/<插件目录名>/<endpoint>`，
> 而后端 `_match_registered_web_api()` 拿**整段**去和注册的路由做 `fullmatch`。
> 所以注册时写 `/ncd-monitor/overview` **匹配不上**，会返回「未找到该路由」。
>
> 代码里用 `PLUGIN_DIR_NAME = os.path.basename(os.path.dirname(os.path.abspath(__file__)))`
> 自动推导，插件目录改名也不会失效。

前端用桥接 SDK 调用：

```javascript
const P = window.AstrBotPluginPage;
const data = await P.apiGet("/ncd-monitor/overview");
```

**③ 桥会把你的响应体【再剥一层】—— `apiGet` 拿到的不是整个 body：**

父窗口把结果回传给 iframe 时用的是这个表达式：

```javascript
// dashboard/dist/assets/PluginPagePage-*.js
h(n, true, ((r.data) == null ? void 0 : r.data.data) ?? r.data);
//              r.data = 整个 HTTP body
//              ↑ 如果 body 里有 .data，就只把 .data 交出去
```

而 `json_response()` **不加** `{status, message, data}` 信封 —— 它直接
`JSONResponse(data)`：

```python
# astrbot/api/web.py:358
return JSONResponse(jsonable_encoder({} if data is None else data), ...)
```

两者叠在一起：后端 `json_response({"k": key, "data": data_url})`
→ body 就是 `{"k":…,"data":…}` → 桥取 `body.data` → **前端拿到的 `r`
就是那个 `data_url` 字符串本身，不是对象。**

```javascript
// ❌ 错的：r 已经是字符串了，r.data 永远 undefined
const r = await P.apiGet("/ncd-monitor/thumb/" + k);
if (r && r.data) { ... }

// ✅ 对的：两种形状都认
const dataUrl =
  typeof r === "string" ? r : (r && typeof r.data === "string" ? r.data : "");
```

**当时的症状很有误导性**：控制台里明明打出了完整的 `data:image/jpeg;base64,…`，
看着像"后端没返回数据"，实际是**返回值对了、前端取错了字段**。
因为缓存永远填不上，每 5 秒重刷就重新请求一遍，还每次都把整个 base64
打进控制台 —— **日志里绝不能打印响应对象**，只打 `typeof r` 就够定位了。

> 错误路径不受影响：`error_response()` 是真的带 `{status:"error"}` 信封的，
> 父窗口会 `throw`，落到 `.catch()` 分支。

**为什么只有缩略图坏了、其它面板都正常？** —— 因为桥只在 body 里**恰好有
`data` 这个键**时才剥一层。逐个体检：

| 接口 | 返回的顶层键 | 会不会被剥 |
|---|---|---|
| `/overview` | `astrbot` `system` `lm_studio` `snowluma` … | 不剥 ✅ |
| `/history` | `context_series` `llm_calls` | 不剥 ✅ |
| `/summaries` | `items` | 不剥 ✅ |
| `/contexts` | `items` `count` | 不剥 ✅ |
| `/thumb/<key>` | `k` **`data`** | **剥掉一层** ⚠️ |

（后来我把前端改成两种形状都认，所以就算以后别的接口也叫 `data` 也不会再踩。）

### 按会话分流人格（`persona_public` / `persona_private`，默认都留空）

**需求**：群聊（公开）和私聊（私密）用**两套不同的人格**。

**为什么不能靠"在一条提示词里写两个模式"**：实测人设里写「两个模式，请先判断自己身份：
模式1：私聊里…」**不生效**。因为 AstrBot 只给模型一行 `Group name: xxx` 藏在
`<system_reminder>` 里，要模型自己推理出"有群名 → 我在群里 → 该用模式 2"。
**间接线索赢不过直接指令**，不是模型笨。

**做法：不碰提示词，在人格解析那一步换人。**
AstrBot 解析人格的唯一入口是 `PersonaManager.resolve_selected_persona()`，
**签名里就带 `umo`**（`平台:MessageType:会话`）：

```python
async def resolve_selected_persona(self, *, umo, conversation_persona_id,
                                   platform_name, provider_settings=None)
```

本插件包装它，按 umo 里的 `MessageType` 决定用哪个人格：

| umo 中的 MessageType | 用哪个 |
|---|---|
| `FriendMessage` | `persona_private` |
| `GroupMessage` / `OtherMessage` / 其它一切 | `persona_public`（**兜底**） |

- **人格内容仍然在 AstrBot 的人格库里维护**（面板 → 人格），本插件只存"名字"；
- **留空 = 完全不干预**，走 AstrBot 原本的默认人格；
- **会话被手动指定过人格 → 尊重**：只要 `conversation_persona_id` 不是 `None`
  （包括 `"[%None]"` 这种"明确不设人格"）或被 session rule 强制，一律原样返回。
  实现上是**先让 AstrBot 自己算完再决定要不要换**，所以不需要复刻它那套优先级逻辑。
- 人格名写错 / 人格被删 → 记一条 WARNING 并**保持原人格**（不会把对话搞没）。

**为什么不在 `on_llm_request` 里改提示词**：那个钩子跑在人格**已经拼进
`system_prompt`** 之后，那时只能做字符串替换 —— 脆，而且容易切坏别人的提示词。

> 这一项是**运行时包装**，所以 AstrBot 升级不受影响（跟思考剥离同一套技术）。
> `测试-人格分流.py` 守着 6 件事：分流（私聊/群聊/其它兜底）/ 尊重手动 /
> 留空不干预 / 人格名不存在回退 / fail-open / umo 是对象。

### 按会话分流知识库（`kb_public` / `kb_private`，默认都留空）

**需求**：和人格分流同一个思路 —— 私聊用一套知识库，群聊用另一套（或者干脆不用）。

**为什么不用 AstrBot 原生的两条路**（都试过，各有硬伤）：

| 原生做法 | 硬伤 |
|---|---|
| 会话级 `kb_config`（面板 → 会话管理 → 知识库） | 按 umo **精确匹配**，没有"所有私聊"这种粒度。新加的私聊好友不会自动生效，得一个个去绑 |
| 配置方案（umop 路由，支持 `*:FriendMessage:*` 通配） | 它指向的是**一整份配置快照**（独立的 `AstrBotConfig` 文件）。选了这条路，以后每次改主配置（换模型、改人格池……）都得记着同步一份到方案里，是维护陷阱 |

**做法：包装 `astr_main_agent.retrieve_knowledge_base()`。**

```python
async def retrieve_knowledge_base(query: str, umo: str, context: Context) -> str | None
```

- 它是 `astr_main_agent._apply_kb` 取知识库的**唯一入口**；
- `_apply_kb` 在**调用点**才解析模块全局名，所以换掉模块属性就能生效；
- 签名里就带 `umo`，能判会话类型。

> ⚠️ **不能包装 `kb_mgr.retrieve()`** —— 它的签名里只有 `kb_names`，
> **没有 umo**，判不了会话类型。（这是选接缝时第一个要排掉的对象。）

| umo 中的 MessageType | 用哪个 |
|---|---|
| `FriendMessage` | `kb_private` |
| `GroupMessage` / `OtherMessage` / 其它一切 | `kb_public`（**兜底**） |

- **知识库内容仍然在 AstrBot 的知识库里维护**（面板 → 知识库），本插件只存"名字"；
- **留空 = 完全不干预**，走 AstrBot 原本的全局 `kb_names`；
- **用逗号分隔可以填多个**：`常识库, 表情包索引`（跟底层 `kb_names: list[str]` 同语义），
  其中名字对不上的会被自动过滤掉；
- **会话被手动绑定过知识库 → 尊重**：只要会话的 `kb_config` 里存在 `kb_ids` 键
  （包括 `[]` 这种"明确不用知识库"），一律不插手。判定的是**键存不存在**，不是列表空不空；
- **检索条数沿用 AstrBot 全局配置**：`kb_final_top_k` / `kb_fusion_top_k`，
  本插件不另起一套，免得同一个数字要在两处调；
- 名字全对不上 → 记一条 WARNING 并**回退原逻辑**；没检到内容 → 老老实实返回 `None`，
  **不能把原逻辑的结果顶上来**（那会在"检索为空"时行为悄悄变样）。

> 这一项同样是**运行时包装**，AstrBot 升级不受影响。
> `测试-知识库分流.py` 守着 10 件事：分流（私聊/群聊/其它兜底）/ 尊重手动 /
> 留空不干预 / 库名不存在回退 / 多库逗号分隔 / 检索参数沿用全局 /
> 空结果返回 `None` / fail-open / umo 是对象 / 幂等 + 可还原。

#### 知识库相关度到底是什么意思（`kb_score_threshold`）

**先说结论：那个「相关度」是相对分，不是绝对分，所以它不能用来判断"这次检索好不好"。**

AstrBot 的融合分（`retrieval/rank_fusion.py:143`）：

```python
fusion_scores[id] = 0.9 * normalized_dense[id] + 0.1 * normalized_sparse[id]
```

关键是这两个 `normalized_*` 都是 **min-max 归一化**：

```python
normalized_dense[id] = (similarity - min) / (max - min)   # rank_fusion.py:118
```

**min-max 归一化保证「每次检索的第一名」都被顶到满分。** 实测：

| 查询 | [1] | [2] | [3] |
|---|---|---|---|
| `你好啊`（垃圾查询） | **0.90** | 0.61 | 0.54 |
| `你可以干什么`（垃圾查询） | **0.90** | 0.79 | 0.69 |
| `支配方羞辱对方的台词`（好查询） | **0.99** | 0.43 | 0.41 |

看第一列 —— **三条查询的第一名都是 0.9**，无论查询本身有没有意义。

> 0.90 这个数字本身就是证据：`0.9 × 1.0 + 0.1 × 0 = 0.90`，
> 意思是「它在稠密检索里排第一，且完全没出现在稀疏检索里」。
> 而不出现在稀疏结果里，恰恰是因为这句话**没有任何实词能匹配**。

**所以这个阈值能做什么、不能做什么：**

- ✅ **能**：砍掉第 2、3 名的弱命中。阈值 0.65 时，「你好啊」从 3 块降到 1 块。
  推荐 **0.6 ~ 0.7**。
- ❌ **不能**：判断「这次检索整体不行」—— 第一名永远过关。想连第一名都不要，
  唯一办法是把 AstrBot 全局的 `kb_final_top_k` 调成 **1**。

> 另：AstrBot 的检索接口里有个 `threshold` 字段，但**整条链路上没有任何地方读它**
> （`retrieve_knowledge_base` → `kb_mgr.retrieve` 都没这个参数）。
> 所以阈值这件事只能由本插件在拿到结果之后自己筛。

**实现上的两个约定：**

- **全部被筛掉 → 这一轮干脆不注入**，而不是退回未过滤的结果
  （否则"过滤"在极端情况下会静默失效）；
- **没筛掉任何东西就不重新排版**，直接用 AstrBot 自己生成的 `context_text` ——
  只有真的筛掉了才调用它的 `_format_context` 重排，避免注入格式跟 AstrBot 分叉。
  万一那个内部函数改了名/改了签名，异常会被吞掉并**退回不过滤**
  （宁可有噪声，也不能因为一个私有方法就把知识库整个搞没）。

#### 附：用 AstrBot 知识库前必踩的一个上游 bug

**并发上传多个文档会丢向量。** 实测一次提交 12 个 `.md`：

```
doc.db  documents 表 = 1968 行
index.faiss ntotal   =  522        ← 丢了 73%
```

根因在 AstrBot 自身（4.28.2）：

```python
# kb_helper.py:178  _ensure_vec_db() 每次调用都新建实例
vec_db = FaissVecDB(..., index_store_path=str(self.kb_dir / "index.faiss"), ...)

# embedding_storage.py:73  构造时从磁盘读一份索引
if path and os.path.exists(path):
    self.index = self._read_index(path)
...
async def insert_batch(self, vectors, ids):
    self.index.add_with_ids(vectors, ...)
    await self.save_index()          # 整个文件写回，全程无锁
```

每个上传任务各自「**读一份索引 → 在私有内存副本里加向量 → 整个文件覆盖写回**」，
后写的把先写的冲掉。索引里剩下的是一段段连续 id 区间，正是覆盖的痕迹。

**绕法：严格串行上传** —— 一个文件的导入任务真正 `completed`（索引已写回磁盘）之后，
再提交下一个。串行后索引单调增长，一个都没丢：

```
6 → 16 → 32 → 53 → 84 → 100 → 131 → 272 → 553 → 1003 → 1452 → 1968
```

> 面板上**多选文件一起传**会踩同一个坑。传完记得核对：
> `index.faiss` 大小 ≈ 块数 × 维度 × 4 字节（768 维即 ×3072）。

### 群名的来源

群号 → 群名 从 **SnowLuma 的本地身份库**读：

```
<NapCat>\DATA\components\SnowLuma\data\<QQ号>\snowluma_identity.db
    groups 表：group_id, group_name, member_count, active
```

**读不到不影响其它功能**（会退回显示群号）。

#### ⚠️ 换过 QQ 号的人必看：挑库与刷新

这里踩过两个坑，都是"面板上群名一直显示不出来（只有「群 <群号>」）"的原因：

**① 必须挑【当前账号】那个库。**
`data\` 下每个登录过的账号一个目录（`旧号\`、`新号\`）。原实现用的是
`_first_match(data\*, snowluma_identity.db)`，而它是 `sorted()[0]` —— 于是**换号之后
永远读到旧账号那个库**（旧号登出后就不再更新，实测只剩 13 个群），新账号进的群
一个都查不到名字。

现在按**文件 mtime 挑最新改动的那个**（活跃账号的库一直在写），也可以在插件配置里
用 `snowluma_identity_db` 显式指定。

> 实测那次：旧库 10-13 冻结、13 个群；新库当天 14:33 还在更新、里面就有那个群的
> `某群名`。**只读打开没问题**——新库带 4MB 的 WAL 也照样读得到。

**② 改名要能自动跟上。**
原来群名表是「只读一次、永久缓存」（`_group_names_loaded`），群改名后**必须重载插件**
才看得见。现在按库文件的 **mtime** 判断要不要重读：

- 刷新只在**面板轮询**那条路上做（`api_overview`，而且丢进线程池）；
- **绝不能放进 `_gate_label` / `on_llm_request`** —— 那是回复链路，读盘会阻塞事件循环
  （`_gate_label` 的注释里写着这条纪律，历史上有过教训）；
- 库被临时锁住导致读失败时**保留上一次的结果**，不清空（不能因为一次读失败让面板
  突然全掉名字）。

`测试-群名解析.py` 钉着这五件事：挑库 / 群名来源 / **改名后刷新（带负面对照：
不调刷新就还是旧名）** / 显式配置优先 / 库缺失降级。

### 主动回复怎么识别

在 `@filter.on_llm_request` 里看 `event.is_at_or_wake_command`：
- 群聊 且 不是 @/唤醒 → 判定为**主动回复**，计数 +1

### 前端

`pages/monitor/index.html` —— 单文件（CSS/JS 内联），跟随面板深浅色主题。

**两条刷新节奏（别混在一起）：**

| 循环 | 周期 | 拉什么 |
|---|---|---|
| `tick()` | 5 秒 | `/overview` + `/history` + `/summaries` + `/contexts`（LM Studio / SnowLuma 的探活结果**缓存 20 秒**，不是每 5 秒真去连） |
| `refreshSys()` | 1 秒 | `/sysinfo`（只读后端内存，轻） |

> 资源卡**不能**在 `tick()` 里渲染 —— 那里再写一次 `#resBox` 会把趋势图一起冲掉。

### 9. 各会话上下文占用 ⭐

**同一个模型上下文上限，每个会话各算各的。** 顶部那条「上下文用量」只反映
**最近一次请求**的会话；这台机器上同时跑着 QQ 群、私聊和 WebUI 的 demo-bot 通道，
历史长度完全不同 —— 只看一条，看不出"到底是哪个会话在逼近压缩线"。

所以左栏下部这张卡按 `unified_msg_origin` 分开列，每行一个会话：

```
🏠 示例群聊          [████░░░░░░░░░░░░]   18 条 · 5,284 tok · 16.1%
👤 示例用户（私聊）     [██░░░░░░░░░░░░░░]    6 条 · 1,900 tok ·  5.8%
```

- **进度条** = 该会话**最近一次**请求的估算 token ÷ 模型上限
- **数值** = `消息条数 · token（估算） · 占用%`
- 达到压缩线（`context.max × 0.82`）时，条和数字**同时变橙**
- **按占用降序** —— 这张卡的用处就是一眼看出谁快爆了
- 悬停看 `umo / 消息数 / token / 字数 / 最后时间 / 累计请求次数`
- 认不出 `umo` 的快照单独成一档「❓ 未识别会话」，不和真私聊混在一起

**两个口径上的坑（都处理了）：**

1. **取「最近一次」，不是累加。** 上下文是**滚动窗口**，把同一会话的多次请求
   累加没有意义。聚合逻辑见 `main.py` 的 `_session_usage()`。
   > 🔥 测试见 `测试-会话占用.py` —— 它专门钉了"取最近一条"和"降序"两条。
   > 这两处写反了，页面上只是**数字不对**、不报错，肉眼根本看不出来。
   > 第一次跑还把「umo 为空」那条的标签暴露成 `?`，顺手改成了「未识别会话」。
2. **统一用估算口径。** 只有发起过请求的那个会话才有 LM Studio 回传的真实用量
   （`last_prompt_tokens`），其余会话没有。两种口径混着比会误导，所以这张卡
   一律用 `_estimate_messages_tokens()` 的估算值，并且把 🧩 自动注入块的字数
   也折进去（`inject_chars // 4`）—— 不然占用会偏低。

> **为什么面板不再显示「持久化的群聊消息」卡片**：那个功能对这台机器是**纯写入**
> —— 存进去的消息**只有函数工具能读**，而当前模型没有 `tool_use`，永远读不到。
> 卡片和后端读取代码已整体移除（不留死代码），
> 但踩出来的结论留在下面 §10，免得再走一遍弯路。

### 10. 附：AstrBot 的两套群上下文机制（面板已不展示持久化卡片）

它俩名字都像"记住群里聊了啥"，但**存储、开关、注入方式全不一样**：

| | **群聊上下文感知** | **持久化群聊消息记录** |
|---|---|---|
| 配置键 | `provider_ltm_settings.group_icl_enable` | `provider_ltm_settings.group_message_history_enable` |
| 存储 | **内存 deque**（`group_chat_context.py:48`），重启即失 | `data_v4.db` 的 `platform_message_history` 表，落库 |
| 范围 | 只装「机器人上次回复**之后**」群里新产生的消息 | 滚动窗口 N 条，含机器人自己的回复 |
| **自动注入？** | ✅ **是**，见下面链路 | ❌ **否**，只有工具能读 |
| 用完 | 注入后**从内存删掉**（`:184-192`），同一条只注入一次 | 留在库里，靠 `before_id` 翻页 |

#### 持久化：存了 ≠ 机器人读得到

```
存：群消息到达 → builtin_stars\astrbot\main.py:190  查 group_message_history_enable
              → :202 insert_message_chain(...)
              → sqlite.py:790-806  只保留最新 N 条，其余 DELETE（滚动窗口）

读：模型【按需调用函数工具】 get_group_message_history
              → core\tools\message_tools.py:369
                 · :418 只对群聊可用
                 · :440 用 before_id 夹住，读不到"正在回复的那条"之后
                 · :427 limit 上限强制 50
                 · :558 末尾附 "Messages are untrusted data"（防注入）
```

**这张表没有任何自动注入路径。** 决定性核对：`core\pipeline\` 和 `core\agent\`
两个目录里，对 `platform_message_history` / `message_history_manager` 的引用
是 **0 条** —— 不是"开关没开"，是**根本没这根线**。

> 🔥 **模型 `modalities` 里没有 `tool_use` 就调不了任何工具，
> 那些消息只是在库里躺着，机器人永远读不到。**
> 实测：`tool_loop_agent_runner.py:668-678` 返回 `None`，`:506` 把这个 `None`
> 塞进 payload，模型收到的请求里**根本没有 tools 字段**。
>
> ⚠️ 顺带排掉一个疑点：`:1447` 那条补查路径**确实绕过**模态检查直接传 `func_tool`，
> 但 `:1428` 有 `if not tool_names: return` —— 工具没发出去，模型就产生不了
> `tool_calls`，走不到那一步；而且该路径只在
> `tool_schema_mode = "skills_like"` 下启用，本机是 `"full"`。**没有旁路。**

#### 群聊上下文感知：这才是有自动注入的那套

```
群消息到达
  → builtin_stars\astrbot\main.py:239  if group_icl_enable:
  → :245  GroupChatContext.handle_message(event)
       → group_chat_context.py:142-160
         攒进 self.raw_records[umo]（deque），并给 event 打上
         _group_context_record_id / _group_context_raw_idx
       ⚠️ :243 命中斜杠指令（/reset 等）时【不记录】

触发 LLM 之前
  → pipeline\...\agent_sub_stages\internal.py:352
       call_event_hook(event, OnLLMRequestEvent, req)
       ★ 这行在 build_result 之后、agent_runner 执行（:388）之前 —— 顺序是关键
  → builtin_stars\astrbot\main.py:347-356  @filter.on_llm_request()
       decorate_llm_req() → GroupChatContext.on_req_llm(event, req)
  → group_chat_context.py:194-197
       req.extra_user_content_parts.append(TextPart(群聊上下文块))
       块 = GROUP_HISTORY_HEADER/FOOTER 包住的
            "<system_reminder>You are in a group chat. ..."

进入 provider
  → tool_loop_agent_runner.py:508
       payload["extra_user_content_parts"] = req.extra_user_content_parts
  → provider\sources\openai_source.py:1386-1401  assemble_context()
       ★ 这些块被【并进最后一条 user 消息】的 content_blocks，
         排在用户原话【之后】、图片之前
```

**所以模型确实会自动收到群上下文 —— 收到的是内存那份 deque，不是数据库那份。**
而且这条路径**完全不依赖工具**：工具被摘了，群上下文照样注入。

旁证：AstrBot 给那个工具写的 `description` 自己就承认了
（`core\tools\message_tools.py:372-376`）：

> "Use it when the user refers to an earlier discussion ... or
> **automatically supplied group context** is insufficient."

即：**存在"自动提供的群上下文"，工具只是补充。**

#### 面板怎么显示这段自动注入

它不走 `contexts` / `system_prompt` / `prompt` 任何一个，而是 `text_chat(**kwargs)`
里一个**独立的参数**（`main.py` 的 `_split_extra_parts`）。
现在挂在**最后一条 user 消息**下面：

- 折叠块标题：`🧩 AstrBot 自动注入 · N 字 · 并进这条消息发给模型`
- **只在群里出现** —— `group_chat_context.py:143` 明确只处理群消息，私聊不注入
- 和消息上的 `📋` 提醒**不是一回事**：
  - `📋`（`m.reminder`）= 内嵌在正文里的 Identifier 提醒，`_split_content` 用正则剥出来的
  - `🧩`（`m.inject`）= 独立的 `extra_user_content_parts`，物理上并进同一条消息

**⚠️ 未实测**：复核日志时最近的请求都是 `umo=demo-bot_...:FriendMessage:...`（私聊），
私聊不触发注入，所以"注入块非空"目前只是**源码级确认**。要看实物：
重启插件 → 在群里 @ 机器人一次 → 面板该条 user 消息下应出现 `🧩` 块。

#### 持久化里没有图（顺带记一笔）

写入端就把媒体降级成了占位文本（`platform_message_history_mgr.py:64-74`，
注释写的是 **"path-free"**）：

| 原组件 | 存成 |
|---|---|
| `Plain` | `{"type":"plain","text":…}` |
| **`Image`/`Record`/`Video`/`File`** | **`{"type":"plain","text":"[Image]"}` —— 内容不存** |
| `At` | `{"type":"at","user_id","name"}` |
| `Reply` | `{"type":"reply","message_id","sender_name","text"}` |

所以持久化里**没有本地图片路径**，不存在"能还原缩略图"的可能。
这正好是「压缩会不会把图清掉」的**另一面**：压缩会清图，而持久化压根没存图。

---

## 三、怎么用

1. **重启 AstrBot**（新插件需要重启才会被发现；之后改插件代码可以点面板的刷新图标热重载）
2. 打开 AstrBot 面板 → **插件** 页 → 应该能看到「链路监控」
3. 插件卡片上会有一个自定义页面入口 → 点进去就是监控面板

### 3.1 环境配置（不用改代码）

插件目录里有 **`_conf_schema.json`**，所以 AstrBot 面板 → 插件 → 链路监控 →
**配置** 里会出现这几项。**全部可以留空** —— 留空时按「自动探测 → 内置缺省」兜底：

| 字段 | 留空时的行为 |
|---|---|
| `persona_public` | **留空**（不干预）。公开场合（群聊等）用的人格名，见《按会话分流人格》一节 |
| `persona_private` | **留空**（不干预）。私聊用的人格名 |
| `kb_public` | **留空**（不干预）。公开场合（群聊等）用的知识库名，见《按会话分流知识库》一节 |
| `kb_private` | **留空**（不干预）。私聊用的知识库名；用逗号分隔可填多个 |
| `kb_score_threshold` | **0**（不过滤，全都要）。低于这个相关度的块不注入，推荐 0.6 ~ 0.7，详见《知识库相关度到底是什么意思》一节 |
| `keep_reasoning_in_context` | **开**（= AstrBot 原行为）。关掉后本插件会在**压缩决策之前**把**历史**里的思考剥掉，详见 §3.2.2 |
| `max_concurrent_replies` | **1**（串行 + 排队）。最多几个会话同时生成；填 **0 或负数 = 不限制**。★ 别随便调大，先看 §3.5 的 kv_unified 那段 |
| `gate_hold_timeout` | **300** 秒。并发槽最长占用时间，超时强制回收（兜底，别调太小） |
| `lm_studio_base` | `http://127.0.0.1:1234` |
| `snowluma_base` | `http://127.0.0.1:5099` |
| `snowluma_dir` | **自动就近探测**（见下）。填了它，下面两项自动推导 |
| `snowluma_onebot_config` | 从 `snowluma_dir` 找 `config\onebot_*.json` |
| `snowluma_identity_db` | 从 `snowluma_dir` 找 `data\<QQ号>\snowluma_identity.db` |

> ⚠️ `max_concurrent_replies` 会**改变机器人的响应行为**：超过上限的会话是真的要等。
> 想回到"完全不干预"，填 0。详见 §3.5。

**自动探测**只试固定几个候选位置（`_detect_snowluma_root()`），
**不做全盘遍历** —— 全盘搜会把事件循环拖死，而且手工填一下比等它扫盘快得多。
覆盖的布局就是本机这种「AstrBot 与 DATA 同级」：

```
<base>\ASTRBOT                       ← AstrBot 根（= 插件文件往上四层）
<base>\DATA\components\SnowLuma       ← 探测这里
```

**设计的三个要点**（都有测试钉着，见 `测试-环境解析.py`）：

1. **配置优先于探测。** 填了 `snowluma_dir` 就一定用它，不会被探测值盖掉。
2. **路径里的引号会被剥掉。** 从资源管理器「复制为路径」拿到的是
   `"F:\...\SnowLuma"`，直接 `open()` 会失败且报错难看；`_conf_str()` 统一
   去空白、剥成对引号。
3. **读不到不算错。** 很多人不用 SnowLuma，或装在别处。身份库读不到时群名
   退化成「群 123456」，OneBot 配置读不到时页面显示「未配置」并告诉你
   去哪儿填 —— 而不是一片红。

> ⚠️ 改完配置要**重载插件**（面板上插件卡片的刷新图标，或重启 AstrBot）才生效；
> 解析结果缓存在 `self._env_cache`，不热更新。

> 🔥 **两个函数因此从 `@staticmethod` 变成了实例方法**（`_lm_studio_info`、
> `_snowluma_info`）—— 因为它们要读 `self._env()`。
> 这正是第二节 ① 记的那个坑：在 `@staticmethod` 里写 `self.` 是 `NameError`，
> 而它发生在 `asyncio.gather` 的 executor 里，**整个 `/overview` 直接 500**。
> 改这一类函数之后，**必须把全部路由跑一遍**（`测试-所有路由.py` 就是为此写的）。

### 3.2 想边生成边看？开流式输出

开了之后右栏底部会多一块「**模型正在输出**」：模型逐字生成的内容实时出现在那里，
**QQ 那条消息仍然等流结束才整条发出**，发出去之后那块自动消失。

#### 3.2.1 思考过程（reasoning）也能看到 ⭐

推理型模型（Qwen3 系等，LM Studio 里很多模型可以开「思考」）会在正文之前先输出一段思考。
**它默认不进聊天**（AstrBot 的 `display_reasoning_text` 管这个，默认关）——
但**监控面板里能看到**，因为那是观察，跟聊天是两条路：

| 位置 | 展示方式 |
|---|---|
| 右栏实时块 | 正文**上方**一块 `🧠 思考过程 N 字`，**生成中默认展开**（能实时看它想什么），生成完自动收起；样式是斜体灰字 + 左侧虚线，刻意做得比正文弱 |
| 副行统计 | 有思考时多一段 `🧠 N 字`，一眼看出"这次它想了很多" |
| 请求历史 | 每条请求下面也有一个 `🧠 思考过程 N 字`（默认收起）—— **实时那块会被下一次生成覆盖，想回看就得靠这里** |

##### 实现上必须踩对的一个坑：思考是【逐块】来的

`openai_source.py:660-666`：

```python
reasoning = self._extract_reasoning_content(chunk)
llm_response.reasoning_content = None            # ← 每轮先清空
if reasoning is not None:
    llm_response.reasoning_content = reasoning   # ← 只留这一轮的增量
```

**跟 `completion_text` 是同一个"对象复用"陷阱。** 所以事后从最后那个完整结果对象上读
`reasoning_content`，**只能拿到最后一轮的残片**（很多时候是 `None`）。
必须像正文一样，在 `yield` 的那一刻就把增量抠出来逐块累积。

`测试-流式捕获.py` 用例 13 专门守这条，并且带负面对照
（断言"最终结果的 `reasoning_content` 就是 `None`"，证明不逐块攒就什么都拿不到）。

##### 一条安全红线

累积到的思考过程**只进监控自己的记录，绝不写回响应对象**。原因：那是 AstrBot 真正在用的对象，
而 `openai_source.py:479-480` 明写「有 `reasoning_content` 但没有 content 和 tool_calls
**不能丢**（推理模型需要 reasoning 历史）」——**它会被写进会话历史**。
监控往那儿写一个字都是污染对话。

#### 3.2.2 让思考【不进上下文】（`keep_reasoning_in_context`，默认开）

先说事实：**思考默认是会进上下文的，而且会被 token 计数器算进去。**

`tool_loop_agent_runner.py` 两个地方都把思考塞进了 `run_context.messages`：

```python
# :186-198（普通回复）和 :1028-1043（工具调用）都是这个形状
if llm_resp.reasoning_content is not None or llm_resp.reasoning_signature:
    parts.append(ThinkPart(think=llm_resp.reasoning_content or "", ...))
self.run_context.messages.append(Message(role="assistant", content=parts))
```

而那份列表既是 `ContextManager.process()`（压缩决策）的输入，又是最后**落盘**的内容；
`token_counter.py:61-62` 还会把思考折算成 token。所以它会**随每轮重发**。

**实测代价**（翻 `conversations` 表）：

| 会话 | 思考段数 | 思考总字数 | 最长一段 |
|---|---|---|---|
| A | 1 | 726 | 726 |
| B | 6 | 8,216 | 2,035 |
| **C** | **10** | **13,673** | 2,368 |
| D | 4 | 4,856 | 1,649 |

**工具链会成倍放大**：工具调用那条路是**每个 LLM 轮次存一段**，所以一圈里走
`list → preview → send` 三次调用就塞三段进去。

---

**关掉这个开关后，本插件怎么做的：**

包装 `ToolLoopAgentRunner.step_until_done()`，在每圈对话**开始**时先剥一遍历史：

```python
async def patched_step_until_done(self_, *args, **kwargs):
    plugin._strip_history_reasoning(self_.run_context.messages)   # 压缩决策之前
    async for item in original(self_, *args, **kwargs):
        yield item                                                # 原样透传，不改节奏
```

选这个点是因为 AstrBot 的层级：

```
step_until_done()   ← 一圈对话跑一次（工具循环在这）   ★ 挂这里
  └── step()        ← 一个 LLM 轮次跑一次（压缩决策在这）
```

**为什么不能挂 `step()`**：那就成了"每次 LLM 调用都剥"，模型在一圈内的多轮工具调用之间
会看不到自己刚才的推理——而**那正是开了思考之后工具链变顺的原因**（见 §3.2 的 4B 实测）。
挂 `step_until_done()` 则只在圈首剥一次：**跨轮不累积，圈内不失忆**。

**为什么不在 Provider 包装里剥**：`ContextManager.process()` 跑在插件看到请求**之前**，
那时压缩早就判完了——在 Provider 层剥救不了压缩。

**剥离面**（`测试-思考剥离.py` 用例 1 守着）：

- 只摘 `ThinkPart`；**正文一字不动**；
- **`tool_calls` 必须留着**——摘了工具链就断；
- 摘完什么都不剩的纯思考空壳**整条丢弃**，免得给供应商一个空消息；
- `messages[:] = …` **原地改**，因为压缩和落盘用的就是同一个列表对象。

**fail-open**：整段包 try，监控出错只记一行错误，**绝不影响对话**（用例 4 用"取消息列表就炸"
的假 runner 证明 agent 照常跑）。

**面板上能看到账**：「上下文用量」卡片里多一行 **思考剥离 = 已剥离 N 段 / M 字**；
开关开着时那行显示**「保留（思考会进上下文）」并标黄**，提醒你现在这份上下文里含着思考。

> **代价**：推理模型失去跨轮的 reasoning 历史（AstrBot 原注释认为它需要）。
> 关掉后如果发现回答质量下降，就把它打开。

**在 AstrBot 面板里改两个开关**（服务提供商设置）：

| 开关 | 设成 | 为什么 |
|---|---|---|
| **流式输出**（`streaming_response`） | **开** | LM Studio 才开始真的逐字吐 |
| **不支持流式回复的平台**（`unsupported_streaming_strategy`） | **「关闭流式回复」** | 见下 |

**为什么第二项必须选「关闭流式回复」**：`respond/stage.py:222-230` 把这个值
变成 `send_streaming(stream, realtime_segmenting)` 的第二个参数，而
`aiocqhttp_message_event.py:205-235`（QQ 走这个）：

```python
if not use_fallback:                    # ← 「关闭流式回复」走这条
    async for chain in generator:       #   一路收，不发送
        buffer.chain.extend(chain.chain)
    await self.send(buffer)             # ← 只发一次，在流结束后     ✅
else:                                   # ← 「实时分段回复」走这条（默认值！）
    ...按 。？！~… 分段，边收边发          # ← QQ 里会连刷好几条        ❌
```

> ⚠️ **默认值是「实时分段回复」** —— 只开流式、不动这一项的话，
> QQ 里会被刷成好几条。两个开关在面板里都有，第二项只在流式打开后才出现。

**几个已知的实际情况：**

- **生成结束后这块【常驻】**，标题变成「最近一次输出」，右下角标出「已发出 HH:MM:SS」。
  这样错过了实时过程也能回头看模型刚才输出了什么。要腾地方点右上角**「收起」**
  （纯前端状态，下次开始生成会自动再出现）；**下次开始生成时内容才会被覆盖**。
- **1 秒一段一段跳**，不是逐字动画。数据挂在 `/sysinfo` 上（前端本来就每秒拉一次，
  而且它只读后端内存），零新增路由。1 秒粒度就是流式进度的真实反映，
  硬做逐字反而要插值、会骗人。
- **真实 token 用量不会丢**。`openai_source.py:626` 发了
  `stream_options={"include_usage": True}`，`:675-676` 从流末尾那个
  `choices=[]` 的 chunk 里取 usage。
- **压缩摘要不受影响** —— 压缩器走的是 `text_chat`（非流式）。
- 关着流式时那块**一直不显示**，这是正常的，不是坏了。
- 生成状态一变化，面板会**立刻补刷一次**右栏的上下文列表（`nudgeContexts()`），
  不再干等 `tick()` 的 5 秒节奏 —— 否则会出现"QQ 都收到了、面板上这条还没有回复"。
- 日志里每次流结束会打一行**诊断**，一眼看清这次流式收了多少：
  ```
  [ncd-monitor] 流式结束：37 个 chunk / 214 字 / 4.2 秒 · 完整结果=有
  ```
  （加这行是因为：先前用"日志里没有 `Applying streaming output`"判断流式没生效 ——
  而那条日志在 `turn_off` 下**永远不会出现**，是个坏判据。这行才是可靠信号。）

---

## 四、限制（要知道的）

| 限制 | 说明 |
|---|---|
| **AstrBot 挂了，页面也挂** | 插件跑在 AstrBot 进程内。它看不到"AstrBot 自己崩溃"这种情况。 |
| 数据在内存里 | 重启 AstrBot 后历史清空（做的是实时监控，不是历史归档） |
| 上下文用量是估算 | 见上文 |
| 压缩摘要首次要等压缩发生 | AstrBot 默认 `max_turns` 轮之后才压缩，之前页面显示"还没有触发过上下文压缩" |
| **右栏底部「模型正在输出」平时是空的** | 它只在**开了流式输出**时才有内容。`streaming_response` 关着的话主对话走非流式的 `text_chat`，那块就一直不显示 —— 不是坏了 |
| 实时输出是 1 秒一段一段跳 | 它挂在 `/sysinfo` 上（前端本来就每秒拉一次、只读后端内存），不是逐字动画。这是流式进度的真实粒度 |
| **两个会话同时压缩时，面板的压缩块会混字** | 压缩天然发生在会话信息之外，两个压缩共用同一块实时缓冲。**只影响面板显示** —— 压缩的**结果**来自 `_run_compress_stream` 自己捞的 final，不经过实时缓冲（`测试-包装鲁棒性.py` 用例 4 专门守这条）。默认 `max_concurrent_replies=1`，压缩不可能重叠。**为什么不给压缩流单独编 key**：试过，`_live_clear()` 是按"当前 key"删的，压缩块换 key 之后非压缩时的清理就删不掉它（`测试-压缩流式.py` 用例 3 会红）。代价与收益不成比例，详见 `_live_key` 的 docstring |
| GPU 依赖 `nvidia-smi` | 没装 NVIDIA 驱动会显示"未检测到 NVIDIA GPU"，不影响其他功能 |

### 加固记录（2026-10-13）

一次外部代码审查提出 8 条，逐条验证后的处置：

| # | 结论 | 处置 |
|---|---|---|
| ① `count_tokens` 补丁签名写死 | 结论对**理由说错**（异常其实在 try 里；真会漏出去的是**参数绑定** TypeError，它发生在进入函数体之前） | 已加 `*args/**kwargs` 透传；失败兜底从「返回 0」改成粗略量级估计（0 的含义是"上下文是空的"，会让压缩永不触发） |
| ④ 流式 final 会被中途的非 delta chunk 覆盖 | 对 | 已改为**只认真的带文本的**非 delta chunk。**同一处判据 `_run_compress_stream` 里也漏了**，一并补上 —— 那里后果更重：压缩器只取 `completion_text`，空壳顶掉真摘要等于把上下文换成空白 |
| ⑤ 压缩流 `_live_key` 不可靠 | **前提被实测推翻** | 把压缩事件与「请求诊断」（`_CURRENT_UMO.set` 之处）按时间排出来：压缩发生在它**之后** 63 秒，拿到的是当轮正确 umo，不是残留值。影响面也只在面板显示。改成按任务编 key 会弄坏清理路径，故**保持现状**并记录为已知限制 |
| ⑥ 网页总结灌进面板 | 结构上成立 | toolbox 的总结 prompt 开头加 `[内部·网页总结]` 标记，本插件识别后不计入会话统计。`测试-包装鲁棒性.py` 用例 3 校验两侧字面量一致 |

> 教训一句话：**"这个 try 包住了没有"要靠读代码确认，不能靠描述**；而"改一处"的代价往往落在**另一个**调用点上（⑤ 的清理路径）。

---

## 五、升级 AstrBot 后要做什么

**这个插件不是"补丁"，它整个躺在 `data\plugins\` 里，正常升级不会动它。** ✅

但有两个情况要注意：

1. **升级后插件被禁用/没加载** → 去面板插件页确认它是启用状态
2. **AstrBot 大版本改了压缩器类名** → 摘要捕获失效。检查方法：
   - 看 AstrBot 日志有没有 `[ncd-monitor] 包装压缩器失败`
   - 或页面「压缩摘要」一直为空但上下文明显被压缩过（用量条突然掉下来）

   修法：把 `main.py` 里 `_patch_compressor()` 的
   `from astrbot.core.agent.context import compressor as compressor_mod`
   和 `compressor_mod.LLMSummaryCompressor` 改成新版对应的类。

---

## 六、出问题怎么排查

| 现象 | 检查 |
|---|---|
| 面板里看不到这个插件 | AstrBot 日志搜 `ncd_monitor`；确认 `main.py` + `metadata.yaml` 都在；重启过没有 |
| 页面打不开 / 白屏 | 页面会自己提示"桥接 SDK 未加载"；检查 `metadata.yaml` 里 `name` 和目录名是否一致 |
| 页面显示"拉取失败" | AstrBot 日志搜 `[ncd-monitor]`；多半是某个 API 抛异常 |
| 摘要一直是空的 | 确认 AstrBot 真的压缩过（上下文用量条有没有突然下降过）；日志搜 `ncd-monitor` |
| 数据全是 — | LM Studio / SnowLuma 没开；它们不开就只有 AstrBot 和宿主机数据 |

---

## 七、自测脚本（改完就跑，别靠眼睛）

全部用 AstrBot 自己的 venv：`<AstrBot>\.venv\Scripts\python.exe`
（GBK 终端要先 `set PYTHONIOENCODING=utf-8`，否则 ✅/❌ 打不出来）

| 脚本 | 盯什么 | 为什么值得单独有一个 |
|---|---|---|
| `测试-所有路由.py` | 6 条路由全真调一遍 | 只测新路由会漏掉被顺手改动的老路由（真踩过：`@staticmethod` 里写 `self.` 让 `/overview` 整个 500） |
| `测试-自动注入捕获.py` | `extra_user_content_parts` → `inject` 字段 | 它是**独立参数**，不进 `contexts`；**用例 3 抓出过真 bug** —— 原守卫条件会把"只有注入块"的请求整条丢掉 |
| `测试-会话占用.py` | `_session_usage()` 聚合 | 「取最近一条」和「降序」写反了页面只是**数字不对**、不报错 |
| `测试-环境解析.py` | 配置 → 探测 → 缺省 三级解析 | 配了却不生效、探测找错目录、路径带引号 —— 三类错都**不报错只悄悄不对** |
| `测试-重载失效.py` | 重载后包装是否仍指向**当前活实例** | 走"点刷新图标重载"会让右栏静默变空。**必须行为验证**（看快照落进谁 + 看增量），查闭包会假通过 |
| `测试-压缩器不阻断.py` | 监控代码出错时压缩是否照常执行 | 前置估算裸在 `try` 外 → **监控把被监控对象搞挂**。用哨兵替换 `original` 来验证"到底调没调" |
| `测试-缩略图越界.py` | `/thumb` 的 key 白名单与路径越界 | 带正对照：先证明越界路径**真能**拼到目标文件，再说挡住 |
| `测试-流式捕获.py` | 流式 chunk 透传 + 实时缓冲 | 开流式后主对话走 `text_chat_stream`，**只包 `text_chat` 会让右栏静默变空** |
| `测试-压缩流式.py` | 压缩改走流式 / 增量可见 / 失败退回非流式 / 标记复位 | ① 流式的包装会**覆盖**压缩块（黄框永不亮）② 标记用全局量会在并发会话间**串** ③ 流式失败**必须退回**非流式，否则监控把压缩搞挂 |
| `测试-会话交错配对.py` | 两个会话交错时「请求 ↔ 响应」是否配对正确 | `_pending` 原本是**单槽**，会被后一条覆盖 —— 表现是群里的主动回复**丢了「主动」标记、来源只剩群号**，耗时还会算成"自己的响应时刻 − 别人的请求时刻"（实测 10.1 秒）。测试带负对照，证明旧逻辑确实会配错 |
| `测试-并发与多流.py` | 并发闸门（2 跑 1 排 / 释放即进 / FIFO / 超时放行 / 回收僵死 / **异常放行**）+ 多路实时输出不混字 | 闸门挂在回复链路正中间，**它出错等于机器人哑巴**，所以专门守 fail-open；多流则守"两个会话的字不能逐字混在一起" |
| `测试-身份解析.js` | 从 `system_reminder` 里抽昵称/QQ/群名 | 直接抽 `index.html` 里的**真实**正则来跑，不是抄一份 |
| `测试-思考块展开.js` | 思考块默认开合（生成中展开/生成完收起）/ **手动点开后重绘必须保持**（含连续 10 次轮询）/ 主动收起被尊重 / 跨代仍听用户的 / 无思考不渲染空块 / 思考在正文之前 | 用户报的"刚点开就被收回"就是这个：`/sysinfo` **1 秒**轮询 → `paintLive` 用 `innerHTML` **重建**整块，`<details>` 的 open 不保留。**负向对照**（没点过的那页必须仍是收起）是关键，否则"保持展开"可能是恒真的假通过。<br>★ 文件名**不能超 11 字符**：实测 13 字符的 `测试-思考块展开状态.js` 会让 SnowLuma 自带那个 node.exe 5/5 崩 `0xC0000005`，改短即好（详见脚本头注释） |
| `测试-滚动跟随.js` | 默认跟随底部 / **往上翻后连续 10 次重绘位置不动** / 负向对照（没上翻过仍贴底）/ 滚回底部重新咬合 / 24px 阈值边界 / 新一代生成重新跟随 | 用户报的"一直往下拽，看不了上面的东西"就是这个：`_liveWasAtBottom` 声明为 true 却从未被赋值，条件恒真。**两个坑都只有跑起来才知道**：① 只"不拽"不够 —— 重建 `innerHTML` 会把 `scrollTop` 打回 0，必须把原位置还回去，否则从"被拽到底部"变成"直接跳到顶部"；② 负向对照不能省，否则"位置不动"可能是"永远不跟随"的假通过。DOM 桩里刻意让 `innerHTML` 置值时把 `scrollTop` 归零，就是为了逼出① |
| `测试-包装鲁棒性.py` | 补丁签名容错 + 兜底不为 0 + 内部请求不污染统计 + 并发压缩结果不串味 | 四件事都**不报错、只悄悄不对**：签名写死要等 AstrBot 升级才炸；兜底返 0 会让压缩永不触发；网页总结混进面板会让"快压缩了吗"虚高；串味只会让面板显示错的字。带负面对照（旧签名确实 `TypeError`、两侧标记不一致会红） |
| `测试-思考剥离.py` | 思考剥离的**剥离面** + 开关 + **策略 A（只剥历史、保住轮内）** + fail-open | 四件事都只有跑起来才知道对不对：`tool_calls` 被误删＝工具链断；把**本轮新产生的**思考也剥了＝模型轮内失忆（那正是开思考的收益来源）；监控抛异常挡住 agent＝真挂对话。用例 3b 断言各轮入口剩余段数 `[0, 1]`，专门钉死策略 A |
| `测试-群名解析.py` | 挑身份库（当前账号优先）/ 群名来源 / **改名后刷新** / 显式配置 / 库缺失降级 | 换过 QQ 号的人必踩：挑库若按名字排序会一直读**旧账号**那个冻结的库（新账号的群一个都没有）；改名后不刷新则永远显示旧名。用例 1 自带负面对照（证明旧逻辑确实挑错），用例 3 的负面对照证明"不调刷新就还是旧名" |
| `测试-人格分流.py` | 按会话类型分流人格 / 尊重手动指定 / 留空不干预 / 人格名不存在回退 / fail-open / umo 是对象 | 五件事都只有跑起来才知道：覆盖了别人手动选的人格、留空反而改了行为、人格名打错导致对话没人格、插件自己出错把人格解析带崩、umo 是对象时判定炸掉 |
| `测试-知识库分流.py` | 按会话类型分流知识库 / 尊重手动绑定 / 留空不干预 / 库名不存在回退 / 多库逗号分隔 / 检索参数沿用全局 / 空结果返回 `None` / fail-open / umo 是对象 / 幂等 + `_unwrap_all` 能还原模块属性 | 和人格分流同一形状，但多两个只在这条接缝上才会出现的坑：① 知识库入口是**模块级函数**（不是一个类方法），`_WRAPS` 里存的是模块对象，还原路径和类不一样，得单独验；② **检索为空时必须返回 `None` 而不是把原逻辑结果顶上来** —— 不然"检不到"这种正常情况会静默变成另一种行为 |
| `测试-探活缓存.py` | TTL 归零的负向对照 / TTL 内只探一次 / 过期重新穿透 / 两个探针不串味 / 返回值是副本 / 8 线程并发只探一次 | 面板每 5 秒刷一次 `/overview`，每次都真去 HTTP 探 LM Studio 和 SnowLuma —— 实测**一天 1.7 万次 `GET /v1/models`、7 MB 日志**。加缓存后又多四个"不报错只是悄悄不对"的坑：该穿透时不穿透（状态变了面板不更新）、该命中时不命中（缓存白加）、缓存被调用方改脏、两个探针串味。用例 1 是负向对照（TTL 归零必须穿透两次），没有它"只探一次"可能是假的 |

**另外两条不靠脚本、但每次都要跑的：**

```powershell
# ① 页面 JS 语法（两个 <script> 块分别 --check）
$html = Get-Content .\pages\monitor\index.html -Raw -Encoding UTF8
$m = [regex]::Matches($html,'(?s)<script[^>]*>(.*?)</script>')
$i=0; foreach ($x in $m) { $i++; Set-Content "$env:TEMP\c$i.js" $x.Groups[1].Value -Encoding UTF8; node --check "$env:TEMP\c$i.js" }

# ② Python 语法
<AstrBot>\.venv\Scripts\python.exe -m py_compile .\main.py
```

> ⚠️ **页面 HTML/CSS 改动不需要重载插件**（每个请求都从磁盘读），
> 但 **`.py` 改动必须重载插件**（点面板的刷新图标，或重启 AstrBot）。

---

## 八、两个"踩了才知道"的工具链坑

### 8.1 `.ps1` 必须存成 **UTF-8 带 BOM**

Windows PowerShell **5.1**（就是系统自带的那个 `powershell.exe`）读 `.ps1`
源码时，**没有 BOM 就按 ANSI/GBK 解码**。脚本里只要有中文，就会被解成乱码，
然后报一堆莫名其妙的语法错误：

```
Unexpected token 'main.py"  = "鎻掍欢涓讳綋"
```

`鎻掍欢涓讳綋` 就是「插件主体」的 UTF-8 字节被当成 GBK 读的结果。

> 🔥 **这个坑真的踩了**：`验证插件.ps1` 一直没 BOM，
> 也就是说**它在 PowerShell 5.1 下从来没跑起来过** ——
> 之前只用 `pwsh`（PowerShell 7，默认按 UTF-8 读，所以看着是好的）跑过，
> 就没发现。发出去之前用 `powershell.exe` 试了一次才暴露。

**修法**：存成「UTF-8 带 BOM」。命令行加 BOM：

```powershell
$utf8bom = New-Object System.Text.UTF8Encoding($true)
$txt = [System.IO.File]::ReadAllText($f, [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText($f, $txt, $utf8bom)
```

**自查**：文件开头三个字节应是 `EF BB BF`。
```powershell
[System.IO.File]::ReadAllBytes($f)[0..2] | % { $_.ToString('X2') }
```

> 只有 `.ps1` 需要这个。`.py`（Python 3 默认 UTF-8）、`.json`（AstrBot 用
> `utf-8-sig` 读 schema，兼容 BOM）、`.html`（浏览器按 `<meta charset>`）、
> `.md` 都不受影响。

### 8.2 用 `git apply` 打补丁时，必须显式关掉换行转换

AstrBot 的源码是**纯 LF**。而 `git apply` 在**不在 git 仓库里的目录**上工作时，
会按 `core.autocrlf` 的默认行为把 LF 写成 **CRLF** ——
补丁内容确实打对了，但**文件字节全变，SHA256 校验必然对不上**。

> 实测：不加 `-c core.autocrlf=false`，一个 1783 行的文件会变成
> **1783 个 CRLF、0 个 LF**（源文件本来是 0 个 CRLF、1783 个 LF）。
> 诡异的是 `git apply --check` 依然返回 0 —— 干跑根本发现不了。

**修法**：

```powershell
git -C <包目录> -c core.autocrlf=false -c core.safecrlf=false -c core.eol=lf apply -p1 <补丁>
```

> 这也是为什么「验证补丁」不能只看 `git apply` 的退出码，
> **必须比 SHA256**（`生成源码补丁.py` 会把每个文件打完应有的哈希写进
> 补丁已拆到独立插件 `astrbot_plugin_ncd_patch`（`patches\校验值.json` 与 `补丁说明.md` 在那里）。

---

## 九、把补丁导出成 `.patch`（分享 / 重打用）

`生成源码补丁.py` 能把 P1~P4 导出成标准 unified diff：

```powershell
<AstrBot>\.venv\Scripts\python.exe 生成源码补丁.py  [输出目录]
```

它做的事：

1. 拿 AstrBot 原地留的 `.bak-before-xxx` 当"修改前"，当前文件当"修改后"，
   用 `difflib.unified_diff` 生成补丁 —— **不是手抄的**，是逐行比对出来的。
2. `.patch` 里**只有补丁本身**（`diff --git` + hunk）。注释不能塞在
   `diff --git` 和 `---` 之间，否则 `git apply` 直接拒收 ——
   标题/原因/校验值全部放到配套的 `补丁说明.md`。
3. 额外产出 `校验值.json`（机器可读），给一键脚本判断
   "还没打 / 已经打过 / 状态不对"。

> ⚠️ 这个脚本**只能在做过补丁的那台机器上跑**（依赖那些 `.bak`）；
> 别人拿到的是 `.patch`，不需要它。

**验证过的结果**：四个补丁用 `git apply` 和 `patch -p1` **两条路径**分别打到
`.bak` 上，结果与当前文件 **SHA256 逐字节一致**：

| 补丁 | 文件 | 打完的 SHA256 前 16 位 |
|---|---|---|
| P1 | `core/tools/cron_tools.py` | `6ad4e420b6cd2d2d` |
| P2 | `builtin_stars/astrbot/main.py` | `ea4f0b68b030ed32` |
| P3 | `core/utils/media_utils.py` | `e4396c67558f8de1` |
| P4 | `core/astr_main_agent.py` | `185173192800a368` |

