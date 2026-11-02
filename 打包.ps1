# -*- coding: utf-8 -*-
<#
  打包 + 归档。

  为什么单独写成一个脚本：
    以前是每次手敲一遍 robocopy / Compress-Archive，于是只有一份
    `AstrBot插件分享包.zip` 被反复覆盖 —— 改坏了就没得回档。
    现在每次打包都会额外留一份【带版本号和时刻的】历史包到 分享包历史\，
    同名不覆盖、内容没变也不重复占位（要强制留档加 -Force）。

  版本号来源：两个插件的 metadata.yaml 里的 version 字段，不在这里硬编码。

  用法：
      pwsh -File 打包.ps1
      pwsh -File 打包.ps1 -Force      # 内容与上一份历史包相同也照样留档
#>
[CmdletBinding()]
param(
    [string]$Work = '',
    [switch]$Force
)

# ★ $PSScriptRoot 写在 param() 默认值里，在部分宿主（powershell -File）下取到的是空串，
#   会让后面所有 Join-Path 直接炸「Cannot bind argument to parameter 'Path'」。
#   所以在脚本体里再兜一次。
if ([string]::IsNullOrEmpty($Work)) { $Work = $PSScriptRoot }

# ★★ 本文件必须以「UTF-8 带 BOM」保存 ★★
#   Windows PowerShell 5.1 读无 BOM 的 UTF-8 会按 GBK 解，中文注释里的
#   多字节序列会把后面的引号吃掉 → 报 "Unexpected token '}'" 这种莫名其妙的语法错。
#   用编辑器/工具改完这个文件，记得补回 BOM：
#     $t=[IO.File]::ReadAllText($p,[Text.Encoding]::UTF8)
#     [IO.File]::WriteAllText($p,$t,(New-Object Text.UTF8Encoding($true)))

$ErrorActionPreference = 'Stop'

$stage   = Join-Path $Work 'AstrBot插件分享包'
$history = Join-Path $Work '分享包历史'
$zip     = Join-Path $Work 'AstrBot插件分享包.zip'
$src     = 'F:\NapCatDesk\ASTRBOT\data\plugins'
$plugins = @('astrbot_plugin_ncd_monitor', 'astrbot_plugin_ncd_toolbox', 'astrbot_plugin_ncd_patch', 'astrbot_plugin_ncd_extra', 'astrbot_plugin_ncd_comfy', 'astrbot_plugin_ncd_session', 'astrbot_plugin_ncd_catchup', 'astrbot_plugin_ncd_inject', 'astrbot_plugin_ncd_awake', 'astrbot_plugin_ncd_memory', 'astrbot_plugin_ncd_welcome', 'astrbot_plugin_ncd_feedback')

# ── 1. 读版本号 ────────────────────────────────────────────────────────
$versions = @{}
foreach ($p in $plugins) {
    $mf = Join-Path $src "$p\metadata.yaml"
    if (-not (Test-Path $mf)) { throw "找不到 $mf" }
    $hit = Select-String -Path $mf -Pattern '^version:\s*(\S+)' | Select-Object -First 1
    if (-not $hit) { throw "$mf 里没有 version 字段" }
    $versions[$p] = $hit.Matches[0].Groups[1].Value
}
$pkgVersion = $versions[$plugins[0]]
# ★ 文件名必须带上【所有插件】的版本，不能只用第一个（monitor）。
#   2026-10-17 实测踩过：那天改动全在 toolbox 上，monitor 一直停在 1.4.0，
#   结果 14 个包全叫 `v1.4.0-...`，只能靠时间戳区分，想回档得靠猜。
#   现在拼成 v<monitor>_m<toolbox>，例如 v1.4.0_m1.12.0。
$verParts = @()
for ($i = 0; $i -lt $plugins.Count; $i++) {
    $prefix = if ($i -eq 0) { 'v' } else { 'm' }
    $verParts += "$prefix$($versions[$plugins[$i]])"
}
$verTag = $verParts -join '_'

# ── 2. 同步文件（patches 走 源码补丁\，不进插件目录）──────────────────
# ★ 2026-10-22：暂存目录是持久的，/MIR 只同步清单里的插件 —— 从清单里移除的插件
#   （比如停用的 ncd_draw）会一直留在暂存目录里被打进包。先清掉不在清单里的。
$stagePlugins = Join-Path $stage '插件'
if (Test-Path $stagePlugins) {
    Get-ChildItem $stagePlugins -Directory -Force |
        Where-Object { $_.Name -notin $plugins } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
}
foreach ($p in $plugins) {
    # ★ 2026-10-19：补丁插件自己的 patches\ 要进包（它就是干这个的），
    #   其他插件的 patches\ 不进插件目录，统一走下面的 源码补丁\。
    $excl = if ($p -eq 'astrbot_plugin_ncd_patch') { @('__pycache__','thumbcache','data') } else { @('__pycache__','thumbcache','data','patches') }
    # ★ 2026-10-20：标签库全量 16 MB / 解压后 64 MB，不进分享包（核心库进，够开箱用）。
    if ($p -eq 'astrbot_plugin_ncd_draw') {
        $excl += (Join-Path $src "astrbot_plugin_ncd_draw\kb\full")
        # ★ 法典（kb/docs）是个人资料，不进分享包
        $excl += (Join-Path $src "astrbot_plugin_ncd_draw\kb\docs")
    }
    robocopy (Join-Path $src $p) (Join-Path $stage "插件\$p") /MIR `
        /XD $excl /XF '测试-*' '*.bak-*' '*.bak' /NFL /NDL /NJH /NJS /NP | Out-Null
    # ★ 2026-11-02：robocopy 的 1~7 是"正常/有跳过"，>=8 才是真失败。
    #   原来 | Out-Null 把退出码吞了，缺文件的包照样打出来并显示成功。
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败（$p）: exit $LASTEXITCODE" }
}
robocopy (Join-Path $src "astrbot_plugin_ncd_patch\patches") (Join-Path $stage '源码补丁') `
    /E /NFL /NDL /NJH /NJS /NP | Out-Null
# ★ 2026-10-19：工具箱也开始带补丁（给内置指令 /stop /stats 限权的那个）。
#   分开放：monitor 的补丁直接铺在 源码补丁\，工具箱的进 源码补丁\工具箱\，
#   免得两边的 P1/P2 同名文件互相覆盖。
if (Test-Path (Join-Path $src "$($plugins[1])\patches")) {
    robocopy (Join-Path $src "$($plugins[1])\patches") (Join-Path $stage '源码补丁\工具箱') `
        /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
}

# /XD 挡下来的目录 /MIR 不会自动删，得手动清，否则打出一堆垃圾
# ★ 2026-10-19：范围从「插件\」扩到整个暂存目录 —— 源码补丁\ 下也曾漏进 __pycache__，
#   结果包里带着 .pyc（还会被隐私扫描抓到里面的本机路径）。
Get-ChildItem $stage -Recurse -Directory -Force |
    Where-Object { $_.Name -in @('data', '__pycache__', 'thumbcache') } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }

# ★ 2026-11-02：测试脚本和旧版备份不属于交付物（里面还有本机路径/内网 IP），
#   即便 robocopy 的 /XF 漏了，这里也再兜一刀。
Get-ChildItem $stage -Recurse -File -Force |
    Where-Object { $_.Name -like '测试-*' -or $_.Name -like '*.bak-*' -or $_.Name -like '*.bak' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

# ── 3. 版本清单打进包里（别人解压就知道这是哪一版）────────────────────
$builtAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$lines = @(
    "打包时间: $builtAt",
    "包版本  : $verTag",
    "（文件名里的 v/m 依次对应下面列出的插件，顺序一致）",
    ""
)
foreach ($p in $plugins) { $lines += ("{0,-34} v{1}" -f $p, $versions[$p]) }
$lines += @(
    "",
    "历史包都在：打包脚本同级的 分享包历史\ 目录下",
    "回档方法  ：把要回的那份解压，覆盖 data\plugins 下的同名目录，然后重载插件。"
)
# ★ 用 .NET 直写并显式传 UTF8Encoding($false)：PS 5.1 的 -Encoding UTF8 会加 BOM，
#   而 BOM 在 Linux 下会被 cat/grep 当成正文首字符（手机版脚本早就这么改了）。
[IO.File]::WriteAllText((Join-Path $stage '版本.txt'), ($lines -join "`r`n"), (New-Object Text.UTF8Encoding($false)))

# ── 3.5 必读（给拿到包的人：这包是什么 + 我们那边更新了什么）───────────
# ★ 2026-11-01：给纯电脑版加一份「必读」，把手机版这边的更新逐条列出来，
#   标清哪些已在本包、哪些还没有 —— 以后对照补装时不用翻聊天记录。
$readme = @(
    '必读 · AstrBot 插件分享包（纯电脑版）',
    '================================================',
    "打包时间：$builtAt",
    "包版本  ：$verTag",
    '',
    '一、这个包是什么',
    '  一组 AstrBot 插件，装到 data\plugins 即可；不是 AstrBot 本体的魔改版。',
    '  适合：只有一台 Windows 电脑、不需要手机端联动。',
    '',
    '二、怎么装',
    '  1) 完全退出 AstrBot（不是只关窗口）',
    '  2) 备份 data\plugins',
    '  3) 把「插件\」下的目录复制进去（源码补丁\ 里的按需覆盖）',
    '  4) 启动 AstrBot，确认插件都启用',
    '  详细步骤见同目录的 安装说明.txt。',
    '',
    '三、本包包含哪些插件',
    '  以 版本.txt 为准（那份是打包时现读的 metadata.yaml，不会过期）。',
    '',
    '四、我们这边（手机版）2026-10-31 ~ 11-01 更新了什么',
    '  【已在本包内】',
    '   · comfy 2.20.28  LoRA 三态：留空=用默认风格 / 写名字=替换 / 前缀 + = 叠加',
    '   · comfy 2.20.27  默认风格 LoRA（秘制柔和动漫风24，强度 0.8，仅 moss 预设）',
    '   · comfy 2.20.26  发图被风控后冷却 10→3 分钟；图进待发队列，到期自动补发；',
    '                    同会话两次发图最小间隔 15 秒',
    '   · comfy 2.20.25  风控提示不再把内部指令复述给用户；新增 /ncd-comfy/link 链路接口',
    '   · comfy 2.20.22  出图前先问电脑侧 linkd 探针，区分「网络不通」与「ComfyUI 没起」',
    '   · monitor 1.7.30 修复 Web 路由注册（少一个右括号导致接口全部 404）',
    '   · session 1.0.4  内置 /reset 回执改成中文，并带上会话 ID',
    '  【本包还没有，需要时再补】',
    '   · ncd_link 1.0.3  链路看护插件：每 30 秒探一次 QQ 在线，',
    '                     断线 ≥45 秒后恢复会主动提示「可能漏了消息」',
    '   · 电脑侧工具（不属于插件包，不随包分发）：',
    '                     linkd 探针（8201）、面板防睡眠的电脑端 /awake 页',
    '',
    '五、以后怎么对照补上',
    '   · 我们每次更新都会在「四」这一节追加：日期 + 插件 + 版本 + 一句话改动',
    '   · 你那边只要看 版本.txt 里的插件版本是否落后，',
    '     再按「本包还没有」那段决定要不要补装即可',
    '',
    '六、回档',
    '   历史包都在打包脚本同级的 分享包历史\ 目录下；',
    '   解压后覆盖 data\plugins 下的同名目录，然后重载插件。'
)
[IO.File]::WriteAllText((Join-Path $stage '必读.txt'), ($readme -join "`r`n"), (New-Object Text.UTF8Encoding($false)))



$installNote = @'
AstrBot 插件分享包 · 简单安装
================================

本包只包含插件，不是 AstrBot 本体的魔改版：
不需要替换 AstrBot 源码，也不需要重新安装 AstrBot。

安装步骤：
1. 完全退出 AstrBot（不是只关窗口）。
2. 建议先备份 AstrBot\data\plugins。
3. 解压本包，把“插件”目录下的插件文件夹复制到 AstrBot\data\plugins\。
   - 只想要部分插件时，只复制对应目录即可。
   - 不要把“插件”整个文件夹再套一层；复制后应直接看到
     astrbot_plugin_ncd_comfy\main.py 这种结构。
   - 不要覆盖 data\config 或 data\plugin_data；配置和聊天数据在那里。
4. 启动 AstrBot，WebUI → 插件，确认版本；也可以在插件页重载。
5. “源码补丁”目录是可选的（部分内置指令限权等增强），
   不装不影响基础聊天/绘图；需要时按插件 README 手动应用。

依赖：
- 插件依赖 AstrBot 运行环境里的 Python 包；缺依赖时看 AstrBot 日志报错。
- 本包不包含模型、ComfyUI、TTS 模型或聊天数据。

回档：
把旧分享包解压，覆盖 data\plugins 下的同名插件目录，然后重载插件。
'@
[IO.File]::WriteAllText((Join-Path $stage '安装说明.txt'), $installNote, (New-Object Text.UTF8Encoding($false)))

# ── 4. 先打到临时文件，成功后再替换正式包（避免留下半截 zip）──────────
# ★ 临时名必须以 .zip 结尾：Windows PowerShell 5.1 的 Compress-Archive
#   只认 .zip 后缀，给它 "$zip.tmp" 会直接报「不是支持的存档文件格式」。
$tmp = Join-Path $Work 'AstrBot插件分享包.tmp.zip'
if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Force }
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $tmp -CompressionLevel Optimal
$newHash = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash

# ── 5. 归档 ────────────────────────────────────────────────────────────
if (-not (Test-Path $history)) { New-Item -ItemType Directory -Path $history -Force | Out-Null }
$prev = Get-ChildItem $history -Filter '*.zip' -File |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1

$identical = $prev -and `
    ((Get-FileHash -LiteralPath $prev.FullName -Algorithm SHA256).Hash -eq $newHash)

if ($identical -and -not $Force) {
    "内容与上一份历史包完全相同（$($prev.Name)），跳过归档。要强制留档请加 -Force。"
} else {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $archPath = Join-Path $history "AstrBot插件分享包-$verTag-$stamp.zip"
    $n = 1
    while (Test-Path $archPath) {
        $n++
        $archPath = Join-Path $history "AstrBot插件分享包-$verTag-$stamp-$n.zip"
    }
    Copy-Item -LiteralPath $tmp -Destination $archPath
    "已归档历史包：$(Split-Path $archPath -Leaf)"
}

Move-Item -LiteralPath $tmp -Destination $zip -Force

# ── 6. 汇总 ────────────────────────────────────────────────────────────
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z = [System.IO.Compression.ZipFile]::OpenRead($zip)
"最新包  : $(Split-Path $zip -Leaf)  $((Get-Item $zip).Length) 字节 / $($z.Entries.Count) 条目"
$z.Dispose()
"版本    : " + (($plugins | ForEach-Object { "$_ v$($versions[$_])" }) -join '  ·  ')
""
$hist = Get-ChildItem $history -Filter '*.zip' -File | Sort-Object Name
"历史包（共 $($hist.Count) 份，从不删除）："
foreach ($h in $hist) {
    "  {0,-54} {1,10:N0} 字节  {2}" -f $h.Name, $h.Length, $h.LastWriteTime.ToString('MM-dd HH:mm')
}

# robocopy 的 1 表示“复制了文件”，不是失败；脚本末尾显式 0，避免调用方误判。
exit 0
