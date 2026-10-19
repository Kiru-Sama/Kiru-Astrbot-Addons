# 链路绘图（astrbot_plugin_ncd_draw）

从「链路工具箱」里拆出来的独立 SDXL 绘图插件（2026-10-19 整理）。

**不依赖 ComfyUI**：自带 `sd_worker.py`（diffusers 子进程，按需拉起、空闲自退），
只要一个 Python 环境 + `.safetensors` 底模就能画。

## 工具（3 个）

| 工具 | 作用 |
|---|---|
| `drawing_tools` | **画图入口，必须先调**：参数范围、提示词规则、可选画风/底模、当前默认值 |
| `generate_image` | 画一张图并发到会话（参数全开） |
| `switch_model` | 换底模（不带参数 = 只列可选项） |

**没先调 `drawing_tools` 就直接调画图工具 → 返回错误并指正**，不是静默跳过。

## `generate_image` 参数

| 参数 | 说明 |
|---|---|
| `prompt` | 英文短标签 10~15 个 |
| `negative` | 负面词；留空 = 配置里那套 |
| `size` / `width`+`height` | `832x1216` / `竖` / `横` / `方`，或直接给像素 |
| `steps` / `cfg` / `seed` | 1~150 / 0~30 / 整数（不给=随机） |
| `sampler` | `euler`（欧拉+普通）/ `dpmpp2m_karras` |
| `model` | 底模名字；留空 = 当前底模 |
| `style` | 画风 LoRA，可叠加 `cunnyfunky:0.7` |
| `reference` | `message`=这条/被引用消息里的图；`last`=上一张；留空=文生图 |
| `strength` | 图生图强度 0~1 |
| `force` | 画幅超过警告线、用户已同意时才 true |

画幅上限：单边 ≤1440 直接画；1440~1920 先警告并要用户同意；单边 >1920 或
总面积 >1920×1440 直接拒绝（12 GB 卡实测 1920×1440 峰值 11.6 GB）。

## 生图页面

`/ncd-draw/` 前缀四个接口：`status` `generate` `progress` `last`
（页面文件在 `pages/imggen/index.html`，前端用 `AstrBotPluginPage.apiGet/apiPost` 调）。

## 配置（29 项，都有默认值）

`sd_python` `sd_checkpoint` `sd_model_dir` `sd_model_min_gb` `sd_width` `sd_height`
`sd_steps` `sd_cfg` `sd_sampler` `sd_max_side` `sd_max_mp` `sd_arch` `sd_device`
`sd_offload` `sd_port` `sd_idle_timeout` `image_timeout` `sd_negative` `sd_lora`
`sd_lora_dir` `sd_lora_scale` `sd_allow_nsfw_lora` `sd_lora_status` `img2img_strength`
`sd_progress_in_chat` `sd_progress_interval` `sd_load_grace` `disable_image_on_local`
`local_model_keywords`

## 测试

```bash
python 测试-画图接线.py     # 参数 → payload 接线
python 测试-画图菜单.py     # 入口内容 / 闸门 / 画幅
python 测试-换模型.py       # 底模解析、切换、换完生效
python 测试-采样器.py       # 两档采样器
python 测试-SDXL.py 测试-LoRA.py 测试-画风.py 测试-出图健壮性.py
python 测试-独立后端.py 测试-加载进度.py 测试-本地模型.py
```
## 标签库（`draw_kb_search`）

`kb/` 下两套，来自 `标签库-核心.zip` / `标签库-全量.zip`：

| 目录 | 内容 | 体量 | 何时加载 |
|---|---|---|---|
| `kb/core/` | 动作 / 表情 / 身体特征 / 服饰 / 物件 / 场景 / 风格 / 其他-常用 | 9 文件 · 3.4 MB · 5.9 万行 | **默认**，首次检索时建索引（约 0.3~0.4 秒） |
| `kb/full/` | 在上面的基础上加 角色 / 作品 / 画师 / 梗 / 元数据 / 长尾 | 16 文件 · 16 MB · 28.5 万行 | **按需**：只有模型显式 `scope="full"` 才建（约 1.5 秒） |
| `kb/full/tags.csv` `tags.jsonl` | 程序用的全量表格 / JSONL | 50 MB | 检索时跳过（体积大、和 txt 重复） |

每条标签一行：`tag ｜ 中文名 ｜ 帖数 ｜ 释义`，所以中英文查询都能命中；
检索按**行**打分，直接返回命中的那几行，不会整段塞进上下文。

| 配置 | 默认 | 说明 |
|---|---|---|
| `kb_enabled` | 开 | 总开关 |
| `kb_default_scope` | `core` | 默认查哪套（core / full） |
| `kb_allow_full` | 开 | 关掉后模型就算写 `scope="full"` 也只能查核心 |
| `kb_default_limit` | 8 | 一次返回几条标签（1~20） |
| `kb_max_chars` | 600 | 一次返回的总字数上限 |
| `kb_catalog_limit` | 20 | 空 query 时最多列几个文件 |

> 分享包里**不带** `kb/full`（16 MB），只带核心库；想用全量就把两个 zip 解压到 `kb/core`、`kb/full`。
> 全量库在本地存在时，核心库查不到的查询会自动提示「可以带 scope="full" 再查」。

