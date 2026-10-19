# Kiru-Astrbot-Addons

给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 写的插件与补丁合集。都是先给自己用、再把私人配置剥干净后放上来的：
**能不碰上游源码就不碰**，必须动的地方单独记成补丁；每个结论都尽量附可复现的证据（日志、像素差、测试用例）。

## 里面有什么

| 目录 | 插件 | 一句话 |
|---|---|---|
| `astrbot_plugin_ncd_toolbox/` | 链路工具箱 | 给模型 11 个外部工具 + 一个人也能直接用的生图页 |
| `astrbot_plugin_ncd_monitor/` | 链路监控 | 把 QQ / AstrBot / LM Studio / 宿主机 四段链路的状态汇总到一个页面 |

### 链路工具箱（toolbox）

- **搜索与抓取**：走本机 Edge 的真浏览器（CDP），不依赖任何搜索 API；只回标题+网址，正文先总结再给模型，省上下文。
- **收藏表情**：列出 / 预览 / 发送 QQ 收藏表情包（分页、序号、打乱顺序）。
- **生图**：自带一个 **不依赖 ComfyUI** 的 SDXL worker（独立 python 环境 + diffusers），支持
  多底模热切换、画风（LoRA）叠加与强度、两档采样器（欧拉+普通 / DPM++ 2M Karras）、
  画幅三档（超过阈值先问用户）、图生图、进度回报，以及一个可直接出图/下载的网页。
- **其它**：看头像（含机器人自己）、历史回溯、工具菜单（两级：`ncd_tools → draw_tools`）。

### 链路监控（monitor）

把一个请求从 QQ 端到模型端的四段链路拼起来看：谁卡住、上下文压缩产生了什么摘要、当前用的是哪个人格/模型。

## 安装

1. 把 `astrbot_plugin_ncd_*` 目录整个放进 `<AstrBot>/data/plugins/`。
2. AstrBot 面板 → 插件 → 重载（或重启 AstrBot）。
3. 按需配置：
   - **toolbox 生图**：填 `sd_python`（一个装了 torch + diffusers 的独立环境）、`sd_checkpoint`（SDXL 底模）；画风放进 `sd_lora_dir`，文件名用 `SFW_名字` / `NSFW_名字`。
   - **toolbox 搜索**：需要一个可被 CDP 驱动的 Edge（默认端口 9222，插件会自己拉起）。
   - **monitor**：无额外依赖，装好即用。
4. 配置项都在插件设置里，每一项的说明里写了「为什么」和「踩过的坑」。

## 群聊指令限权（`patches/`）

AstrBot 原生只给 `/reset`、`/new` 加了「群聊需管理员、私聊不限」的限制。
`patches/group_admin_commands.py` 把同样的限制补给 `/stop`（别让人随手停掉你的生成）
和 `/stats`（别让人看到本会话用量），`/help`、`/sid` 保持对所有人生效。

```bash
python patches/group_admin_commands.py --check   # 看有没有打上
python patches/group_admin_commands.py           # 打补丁（幂等）
python patches/group_admin_commands.py --revert  # 撤掉
```

细节（为什么这么改、升级后要不要重打）见 [`patches/README.md`](patches/README.md)。

## 已知限制

- 生图**只支持 SDXL**（Illustrious / NoobAI / WAI 这类单文件底模）。SD1.5 会被明确拒绝而不是画出废图。
- 画幅默认按 **12 GB 显存**封顶：单边 ≤1920、面积 ≤1920×1440（约 2.76 MP）。
  显存更大就把 `sd_max_side` / `sd_max_mp` 调大，代码不用动。
- 画风文件名**必须带** `SFW_` / `NSFW_` 前缀才会被扫描到；**中文名可用**（`SFW_像素风.safetensors`）。
- 模型文件、LoRA、底模**不在本仓库**里（体积太大，也各有各的许可）。
- 这些插件是按「单人自建、和朋友共用」的规模写的，没有做多租户和权限体系。

## 目录结构

```
astrbot_plugin_ncd_toolbox/
  main.py            插件主体（工具、菜单、生图页 API）
  sd_worker.py       独立 SDXL worker（纯 diffusers，单独进程）
  sd_local.py        worker 客户端：按需拉起、探活、换底模
  lora_catalog.py    画风扫描/解析（SFW_/NSFW_ 命名）
  avatar.py          头像抓取与缓存
  history.py         历史回溯
  stickers.py        收藏表情
  browser.py         Edge/CDP 驱动
  pages/imggen/      生图页面
  测试-*.py          离线回归测试（30 个，失败即非零退出）

astrbot_plugin_ncd_monitor/
  main.py            监控页与 API
  pages/index.html   监控面板
  测试-*.py          回归测试
  patches/           对上游的可复现补丁（按需）
```

## 测试

两个插件目录里都有 `测试-*.py`，用 AstrBot 自己的 venv 跑（一个文件一个进程）：

```bash
python 测试-工具注册.py     # 工具是否注册、参数/描述是否齐全
python 测试-画风.py         # 画风命名、解析、叠加
python 测试-画图菜单.py     # 二级目录、画幅三档、按会话解锁
python 测试-独立后端.py     # worker 协议（不需要显卡、不需要模型）
```

测试默认无头运行、不弹窗口；需要模型/显卡的验证都在 README 里单独标注。

## 许可

MIT。模型、LoRA、角色形象等素材的版权归各自作者，不在本仓库内分发。
