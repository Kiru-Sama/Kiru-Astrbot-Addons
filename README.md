# Kiru-Astrbot-Addons

给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 写的插件与补丁合集。都是先给自己用、再把私人配置剥干净后放上来的：
**能不碰上游源码就不碰**，必须动的地方单独记成补丁；每个结论都尽量附可复现的证据（日志、像素差、测试用例）。

## 里面有什么

| 目录 | 插件 | 一句话 |
|---|---|---|
| `astrbot_plugin_ncd_toolbox/` | 链路工具箱 | 8 个外部工具：搜索 / 抓正文 / 收藏表情×3 / 头像 / 历史 / 工具菜单 + 消息合并 |
| `astrbot_plugin_ncd_draw/` | 链路绘图 | 独立的 SDXL 绘图后端（不依赖 ComfyUI）+ 标签库检索 + 生图页 |
| `astrbot_plugin_ncd_patch/` | 链路补丁 | 上游源码补丁的管理器：状态自检 / 手动应用 / 回滚，**绝不自动打** |
| `astrbot_plugin_ncd_monitor/` | 链路监控 | QQ / AstrBot / LM Studio / 宿主机 四段链路的状态面板 |

> 2026-10-19 做过一次拆分：原来工具箱一个插件里塞了搜索、表情、头像、画图、生图页、worker，
> 一共 3400 多行；现在按职责拆成上面四个，各自独立发版。

## 链路工具箱（toolbox）

- **搜索与抓取**：走本机 Edge 的真浏览器（CDP），不依赖搜索 API；只回标题+网址，正文先总结再给模型，省上下文。
- **收藏表情**：列出 / 预览 / 发送 QQ 收藏表情（分页、序号、打乱顺序）。
- **看头像 / 历史回溯**：头像只给模型看，不发给用户；历史回溯支持 30/50/70/90 条。
- **工具菜单**：`ncd_tools` 收窄首轮工具数；**沉浸模式**会剥掉 AstrBot 的机械工具说明（full / half 两档）。
- **消息合并**：同一个人 3 秒内连发几条 → 合成一条再交给模型，只回一次（`/` 指令和带图消息不参与）。

## 链路绘图（draw）

从工具箱里拆出来的独立绘图插件，**不依赖 ComfyUI**：自带 `sd_worker.py`（diffusers 子进程，按需拉起、空闲自退）。

- **入口闸门**：所有绘图操作必须先调 `drawing_tools`；没调就直接调会**返回错误并指正**（不是静默跳过）。
- **参数全开**：`prompt / negative / size / width / height / steps / cfg / seed / sampler / model / style / reference / strength / force`。
- **负面词是追加**：`sd_negative` 那套默认词永远保留，模型/页面给的额外负面词**追加**在后面，不会覆盖。
- **画幅三档**：单边 ≤1440 直接画；1440~1920 先警告并问用户（`force=true` 才继续）；超 1920 或面积超 1920×1440 直接拒绝。
- **两档采样器**：`euler`（欧拉+普通，默认）/ `dpmpp2m_karras`。
- **生图页**：`/ncd-draw/` 四个接口（status / generate / progress / last），人也能直接出图下载。
- **标签库检索**：`draw_kb_search` 查 28.5 万条 Danbooru 标签（`tag ｜ 中文名 ｜ 帖数 ｜ 释义`），
  默认只加载**核心库**（3.4 MB / 5.9 万行），角色/作品/画师/长尾在**全量库**（16 MB / 28.5 万行），
  只有模型显式 `scope="full"` 才懒加载。数据本身**不在本仓库**，见下节。

## 链路补丁（patch）

补丁以前散在监控插件里，谁都不知道「到底打没打」。现在收在一处：

- `patch_status()` 列出每个补丁：已应用 / 未应用 / **版本不匹配**（AstrBot 升级过）/ 目标缺失；
- `patch_apply(id)` / `patch_revert(id)` 都先 `git apply --check`，预检不过**一个字节都不动**；
- 仅管理员可用；**启动不自动打补丁**（自动打会把升级后的新文件改坏）。

当前补丁：

| ID | 目标 | 说明 |
|---|---|---|
| P1 | `core/tools/cron_tools.py` | 定时任务参数兜底 |
| P2 | `builtin_stars/astrbot/main.py` | 戳一戳按人设回应 |
| P4 | `core/astr_main_agent.py` | 模型不支持工具时不再塞工具提示 |
| — | `builtin_stars/builtin_commands/main.py` | 给 `/stop` `/stats` 补「群聊需管理员、私聊不限」 |

最后一个不是 `.patch`，是 `astrbot_plugin_ncd_patch/patches/group_admin_commands.py`（幂等，带 `--check/--revert`）。

## 链路监控（monitor）

把一个请求从 QQ 端到模型端的四段链路拼起来看：谁卡住、上下文压缩产生了什么摘要、当前用的是哪个人格/模型。

## 安装

1. 把需要的 `astrbot_plugin_ncd_*` 目录整个放进 `<AstrBot>/data/plugins/`。
2. AstrBot 面板 → 插件 → 启用 + 重载（或重启 AstrBot）。
3. 按需配置：
   - **draw**：`sd_python`（装了 torch + diffusers 的独立环境）、`sd_checkpoint`（SDXL 底模）；
     画风放进 `sd_lora_dir`，文件名用 `SFW_名字` / `NSFW_名字`（中文名可用）。
   - **toolbox**：需要一个可被 CDP 驱动的 Edge（默认端口 9222，插件会自己拉起）。
   - **patch**：仅管理员；先调一次 `patch_status` 看看现状。
   - **monitor**：无额外依赖。
4. 配置项说明里都写了「为什么」和「踩过的坑」。

## 标签库数据（不在仓库里）

`astrbot_plugin_ncd_draw/kb/` 是**数据目录**，体积大且各有许可，所以不入库：

```
kb/
├── core/   核心库（默认加载）  标签库-核心/*.txt
└── full/   全量库（按需）      标签库/按分类/*.txt
```

每行一条：`tag ｜ 中文名 ｜ 帖数 ｜ 释义`。没有数据也能正常用——`draw_kb_search` 只会回一句「标签库是空的」，
其余画图功能不受影响。细节见 [`astrbot_plugin_ncd_draw/kb/README.md`](astrbot_plugin_ncd_draw/kb/README.md)。

## 已知限制

- 绘图**只支持 SDXL**（Illustrious / NoobAI / WAI 这类单文件底模）；SD1.5 会被明确拒绝而不是画出废图。
- 画幅默认按 **12 GB 显存**封顶：单边 ≤1920、面积 ≤1920×1440（约 2.76 MP），可在配置里调大。
- 画风文件名**必须带** `SFW_` / `NSFW_` 前缀才会被扫描到。
- 模型、LoRA、标签库数据都**不在本仓库**（体积大，也各有各的许可）。
- 这些插件是按「单人自建、和朋友共用」的规模写的，没有多租户和权限体系。

## 目录结构

```
astrbot_plugin_ncd_toolbox/
  main.py            工具（搜索/表情/头像/历史）、工具菜单、消息合并、沉浸模式
  browser.py         Edge/CDP 驱动
  stickers.py        收藏表情
  avatar.py          头像抓取与缓存
  history.py         历史回溯
  测试-*.py          离线回归测试

astrbot_plugin_ncd_draw/
  main.py            3 个绘图工具 + 标签库检索 + 生图页 API
  sd_worker.py       独立 SDXL worker（纯 diffusers，单独进程）
  sd_local.py        worker 客户端：按需拉起、探活、换底模
  lora_catalog.py    画风扫描/解析（SFW_/NSFW_ 命名）
  kb_index.py        标签库索引与检索（core 默认 / full 按需）
  kb/                标签库数据（不入库，见上）
  pages/imggen/      生图页面
  测试-*.py          离线回归测试

astrbot_plugin_ncd_patch/
  main.py            补丁管理与状态自检
  patches/           补丁文件 + 校验值 + 说明 + 应用脚本

astrbot_plugin_ncd_monitor/
  main.py            监控页与 API
  pages/index.html   监控面板
  测试-*.py          回归测试
```

## 测试

每个插件目录里都有 `测试-*.py`，用 AstrBot 自己的 venv 跑（一个文件一个进程，失败即非零退出）：

```bash
# 绘图
python 测试-画图接线.py     # 参数 → payload 接线（含负面词追加）
python 测试-画图菜单.py     # 入口闸门 / 参数说明 / 画幅
python 测试-知识库.py       # 核心库命中 / 全量懒加载 / 降级
python 测试-换模型.py 测试-采样器.py 测试-出图健壮性.py 测试-独立后端.py

# 工具箱
python 测试-工具注册.py 测试-工具菜单.py 测试-合并消息.py

# 补丁 / 监控
python 测试-补丁管理.py 测试-链路.py
```

测试默认无头运行、不弹窗口；需要模型/显卡的验证都在各自 README 里单独标注。

## 许可

MIT。模型、LoRA、标签库数据、角色形象等素材的版权归各自作者，不在本仓库内分发。
