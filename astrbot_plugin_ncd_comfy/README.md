# 链路本地生成（astrbot_plugin_ncd_comfy）

把一份**最小 ComfyUI 便携版**接成 AstrBot 的绘图后端：模型（LLM）只填参数，
ComfyUI 按需拉起、空闲先卸模型再关进程。

**出图用的是你自己调好的四个工作流**（`workflows/*.json`，API 格式），
插件不另拼节点图 —— 只往里填参数。

## 四个预设

| preset | 说明 | 底模 | 默认参数 | 正常耗时 |
|---|---|---|---|---|
| `sdxl` | SDXL 单文件底模，最快 | `SDXL\novaAnimeXL_ilV140` | 720×960 · 30 步 · CFG 5.5 · euler/simple | ~25 s |
| `krea2` | KREA2（UNET + qwen3vl） | `Krea2-MuseByStable_v30Turbo_fp8` | 720×960 · 10 步 · CFG 1.0 | ~30 s |
| `anima` | Anima（UNET + Qwen3-0.6B） | `anima\novaAnimeAM_v6029B` | 720×960 · 22 步 · CFG 5.5 | ~40 s |
| `moss` | MOSS-Image（带 SkipLayerGuidance） | `MOSS\mossImage_sft_pruned_int8` | 720×960 · 50 步 · CFG 4.5 · er_sde | ~60 s |

四个工作流的模型文件全部走 `extra_model_paths.yaml` 外挂，插件里不写死任何本机路径。

## 工具

| 工具 | 作用 |
|---|---|
| `comfy_tools` | **入口，必须先调**：四个工作流、各自底模/默认参数、可选底模清单 |
| `comfy_generate` | 出图：prompt / negative / preset / size / steps / cfg / seed / model / lora / reference / denoise |
| `comfy_models` | 列底模（`kind=model`）或 LoRA（`kind=lora`） |
| `comfy_status` | 起没起来、端口、显存、队列、模型是否已卸载 |
| `comfy_say` | 克隆音色发语音（IndexTTS-2） |
| `comfy_output` | 把本会话画过的图直接发出来（`index` / `file`） |
| `comfy_gallery` | 按提示词/时间翻本会话历史图 |

没先调 `comfy_tools` 就直接调其它工具 → 返回错误并指正。

## 云端 API 异步出图

`comfy_async_api`（默认开）只在**云端 API** 下生效：

- 模型调 `comfy_generate` 后立即拿到“已交给后台”，bot 继续回聊天；
- 图在后台画，完成后**直接发到会话**，不再经过模型确认；
- 异步图发送后会作为 **user 角色的系统记录**写进会话历史（DeepSeek 只接受 user 消息带图，assistant 带图会 400），下一轮模型能直接看到；回合上限/压缩按 AstrBot 正常处理；
- `comfy_self_view=false` 可关掉这个历史回传，省视觉 token；
- 云端出图**不再等本地 LLM 空闲**；本地模型仍会等，避免抢显存；
- 群聊仍然先过 NSFW 审核，没过审不发图；
- 审核失败默认**合成一条系统通知重新走一轮**（`comfy_audit_nudge`），让模型自己回复“没发，要改描述还是取消”；
  这条通知会作为用户消息进会话历史，每会话 10 分钟最多拉起一次，防止循环；
- 本地模型（LM Studio）自动保持同步：它和 ComfyUI 抢显存，不能异步。

异步任务在插件重载/退出时会被取消，并向对应会话发一句“图画被中断了”；失败也会发原因。后台任务有开始/取消/完成日志。

## 设备繁忙检测

`comfy_busy_detect`（默认开）在出图前判断游戏/桌面是否在抢显存：

- `nvidia-smi` 读总显存、空闲显存、GPU 利用率；
- 用 Windows 按进程 GPU 显存计数器读 ComfyUI 进程的 `DedicatedUsage`，算 ComfyUI 自己占用；
- 拿不到进程计数器时，才回退到 torch 统计 / 模型大小估算；
- `外部占用 = 总显存 - 空闲 - ComfyUI 自己占用`；
- 外部占用 ≥ `comfy_busy_external_gb`（默认 3GB）、或可用显存 ≤ `comfy_busy_free_gb`（默认 2GB）且外部 ≥1GB、或 GPU ≥ `comfy_busy_util`（默认 70%）且外部 ≥1GB，就判繁忙。

繁忙时**不阻塞出图**，只在 `comfy_generate` 的工具结果里加一句：

> ⚠ 设备繁忙（外部占用约 5.2GB，GPU 82%）：这次出图速度会明显降低，可以告诉用户稍等。

模型会自己决定怎么转告；异步照常立即返回、后台照常画。扣掉 ComfyUI 自己的占用是为了避免“它自己加载 SDXL”被误判成游戏占用；连续两次采样都繁忙才提示，避免 unload/load 时的一拍滞后误报。

另外有独立的**排队提示**：`comfy_busy_queue`（默认 3，按总任务数算）。1 个生成中 + 3 个排队 = 总共 4 个，超过 3，新请求会追加：

> ⏳ 排队较多（当前共有 4 个出图任务在画/排队）：这次出图会慢一些，可以告诉用户稍等。

总共 3 个（1 生成 + 2 排队）不提示。排队提示和设备繁忙提示是两回事，可以同时出现。

两个显存条件的区别：

- **可用显存低 + 外部≥1GB**：游戏/桌面占着显存，即使 GPU 暂时空闲，ComfyUI 也可能被挤到共享内存。
- **GPU 利用率高 + 外部≥1GB**：游戏正在跑，算力被抢，即使显存还没满也会变慢。

两者不重复，可能只中一个，也可能两个都中；两个都中时提示会一起列出原因。

## 每会话历史图

每个会话一个目录：`data/plugin_data/ncd_comfy/out/<sha1(umo)[:16]>/`，
里面除了图还有 `history.json`（提示词/工作流/时间/大小）。

- 保留 **30 天 / 每会话 200 张**（`comfy_history_days` / `comfy_history_per_session`）；
- `comfy_gallery(query, limit, days)` 列历史，`comfy_output(index/file)` 发旧图；
- `reference` 新增 `index:3` / `file:xxx.png`，可以把旧图当参考图；
- 插件重启后 `reference="last"` 会从会话目录读最新一张，不依赖内存。

## AI 能调什么

| 参数 | 说明 |
|---|---|
| `prompt` / `negative` | 正面 / 负面提示词 |
| `preset` | 换工作流（sdxl / krea2 / anima / moss） |
| `size` / `width` / `height` | 画幅；图生图时留空 = 跟参考图一样大（等比缩到上限内） |
| `steps` / `cfg` / `seed` | 采样参数；留空/0 = 用工作流里的 |
| `model` | 换底模（写文件名或其中一段，大小写不敏感） |
| `lora` | 加 LoRA，如 `"cunnyfunky:0.8"`，多个逗号分隔 |
| `reference` | 图生图：`message`=这条/被引用消息里的图，`last`=上一张，`index:3`/`file:xxx.png`=本会话历史图 |
| `denoise` | 图生图重绘强度 0.05~1，默认 0.6 |

### 画幅三档

| 画幅 | 行为 |
|---|---|
| 单边 ≤ 1440（上限的 3/4） | 直接画 |
| 1440 < 单边 ≤ 1920，且总面积 ≤ 2.7648 MP | **不画**，先返回提醒；用户同意后带 `force=true` 再调 |
| 单边 > 1920，或面积 > 2.7648 MP | 直接拒绝 |

宽度/高度会对齐到 8 的倍数（向下取整，不会往上顶破上限）。
图生图不走这套拒绝逻辑：参考图太大时按比例缩进上限（`fit_within`）。
两个上限在配置里可改：`comfy_max_side` / `comfy_max_mp`（2.7648 = 1920×1440 整）。

### LoRA 是动态插的

你导出的四个工作流里**没有 LoraLoader 节点**（导出前应该静音了）。
AI 要加 LoRA 时，插件在提交前把 `LoraLoader` 串进 model / clip 链：

```
原 model 来源 → lora_1 → lora_2 → KSampler.model
原 clip 来源  → lora_1 → lora_2 → 正面/负面编码器的 clip
```

只改内存里的图，磁盘上的工作流文件一个字节都不动。

### 图生图也是动态插的

不传 `reference` 时**一个节点都不增删**（就是纯文生图，denoise=1）。传了才变成：

```
删掉 EmptyLatent
LoadImage → ImageScale → VAEEncode → KSampler.latent_image
KSampler.denoise = 0.6（可调）
```

VAE 复用工作流里 `VAEDecode` 那个，所以和模型本来就是配套的。
参考图通过 ComfyUI 的 `/upload/image` 传进去（不写死 input 目录，远端服务也能用）。

## 空闲与显存

| 时机 | 动作 |
|---|---|
| 空闲 5 分钟（`comfy_unload_timeout`） | 调 `POST /free` 卸载模型，**进程留着**（下次出图不用重新冷启动） |
| 空闲 30 分钟（`comfy_idle_timeout`） | 关掉 ComfyUI 进程，连内存一起还 |
| 出图 / 冷启动途中 | 两种回收都不触发 |

只回收**插件自己拉起的**进程；你自己手动开的 ComfyUI 不会被杀。

## 加载提示

换工作流、换底模、第一次出图时，先往会话发一条提示（`comfy_notify` 可关）：

```
⏳ 正在启动 ComfyUI（首次约 20~50 秒）…
🔁 切换工作流：MOSS（约 60 秒一张）
稍等，出图后直接把图发你。
```

## 安装

1. 整个目录放进 `AstrBot\data\plugins\astrbot_plugin_ncd_comfy\`
2. 配置两项：`comfy_dir` = 最小版根目录（如 `F:\ComfyUI_min`）；
   `comfy_extra_paths` 留空即用 `<comfy_dir>\ComfyUI\extra_model_paths.yaml`
3. 重载插件，对话里说「画一张猫娘」

## 构建最小便携版

```bat
python 构建最小版.py --source "F:\ComfyUI_portable\ComfyUI_windows_portable-G314" ^
                     --target "F:\ComfyUI_min"
```

复制 `python_embeded`（去掉 `__pycache__`/`*.lib`）+ ComfyUI 核心（跳过 `.git`/
`custom_nodes`/`models`/`output`/`user`），建空骨架，写 `extra_model_paths.yaml` 和
`run_min.bat`。**不复制任何模型。**

> ⚠ 排除项必须用绝对路径：robocopy 的 `/XD` 只写名字会匹配任意层级的同名目录，
> 把 `comfy\ldm\models`、`comfy_api\input` 这类核心目录一起排掉，启动时报
> `No module named 'comfy.ldm.models'`。脚本里已经处理。

## 实测（RTX 5070 Ti Laptop 12 GB，2026-10-21）

| 项目 | 结果 |
|---|---|
| 最小版体积 | **7.87 GB**（原完整安装 35.09 GB） |
| 冷启动 | 26.3 秒 |
| sdxl 720×960 / 6 步 | 30.9 秒（含首次加载模型） |
| krea2 720×960 / 6 步 | 72.8 秒（含换模型加载） |
| anima 720×960 / 6 步 | 30.8 秒 |
| moss 720×960 / 6 步 | 46.2 秒（输出在 temp） |
| 卸载模型 | `/free` 后显存空闲 2.3 GB → **10.7 GB** |
| 离线测试 | `测试-接线.py` **16 组全过** |

## 测试

```bat
:: 纯逻辑，不碰 GPU（16 组）
python 测试-接线.py

:: 端到端：四个预设各出一张 + 图生图 + 卸载验证
python 测试-出图.py --preset all --fast --img2img --free-test
```

## 语音（IndexTTS-2 克隆音色）

工具 `comfy_say(text, speed)`：用参考音频的音色说一句话，发成 QQ 语音。

- 工作流：`workflows/tts.json`（从 `zIndexTTS2 AI语音克隆-情绪控制(带语速调节).json` 裁成单条链路）
- 参考音频：`comfy_voice_reference`，默认 `参考音频.wav`（放 `<comfy_dir>\ComfyUI\input\`）
- 语速：`comfy_voice_speed`（0.5~2.0）
- **束搜索宽度** `comfy_voice_beams`：默认 **1 = 约 6 秒/句**；改成 3 质量略好但会慢到 **80 秒**（实测）

模型放在 `<ComfyUI>\models\IndexTTS-2\`（`gpt.pth` 3.3 GB、`s2mel.pth`、`qwen0.6bemo4-merge`、
`w2v-bert-2.0`、`semantic_codec`、`campplus_cn_common.bin`、`bigvgan/bigvgan_v2_22khz_80band_256x`）。
最小版 ComfyUI 需要这个节点：

1. 把 `ComfyUI-Easy-IndexTTS2` 复制进 `F:\ComfyUI_min\ComfyUI\custom_nodes\`
2. `F:\ComfyUI_min\ComfyUI\models\IndexTTS-2` 用目录联接指到完整版的模型目录（`mklink /J`）

> 为什么不做成"每条回复都配语音"：一次合成要 6 秒（束搜索 3 时 80 秒），
> 挂在回复链路上会把聊天拖慢。要做全量语音，建议用系统自带的 Edge TTS（1~2 秒）。

## 已知限制

- **单卡**：和完整版 ComfyUI 同时出图会抢显存
- 换底模只换工作流里那个加载节点的名字；CLIP/VAE 不跟着换（换错模型 ComfyUI 会报错，原话会带回来）
- 图生图固定插 `ImageScale(lanczos)`，不做裁切；要 crop / ControlNet 得另加
- 四个工作流是 API 格式（`nodes` 那套 UI 格式不能用）

## 回滚

停用插件即可。`F:\ComfyUI_min` 是独立目录，删掉不影响原来那套 ComfyUI。
旧的独立生图插件已移到 `AstrBot\data\plugins_disabled\astrbot_plugin_ncd_draw\`，随时可以搬回来。
- `comfy_prompt_help(query, category=sfw|nsfw|all)`：在本地提示词法典里按关键词查 tag（只读，按需检索，不占常驻上下文）
