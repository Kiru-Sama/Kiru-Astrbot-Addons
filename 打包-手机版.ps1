# -*- coding: utf-8 -*-
<#
  打包【手机移植版】。

  与电脑版的区别：
    - comfy 已纳入（2026-10-30 起）：它支持 comfy_host 指向【远程 ComfyUI】，
      手机端 bot 可以把出图交给电脑的显卡，不再只是「已交给后台」的假承诺。
      默认 comfy_host=127.0.0.1 且插件禁用，要用时填电脑 IP 再启用。
    - 不含 astrbot_plugin_ncd_awake：代码里写死 sys.platform == "win32"，手机无效。
    - 附带 源码补丁-手机\P5-help.py.patch：4.29 起 /help 只列内置命令，
      这个补丁让插件命令也显示出来（路径前缀是 astrbot/，对应源码布局）。
    - 安装说明针对泡泡版（proot + Ubuntu）的目录结构写。

  用法：
      pwsh -File 打包-手机版.ps1
      pwsh -File 打包-手机版.ps1 -Force
#>
[CmdletBinding()]
param(
    [string]$Work = '',
    [switch]$Force,
    # ★ 2026-10-30：同一个脚本出两种手机包
    #   -WithComfy 不传 = 手机版（纯手机，9 个插件，不含 comfy）
    #   -WithComfy   传 = 手机电脑接入版（10 个插件，含 comfy，走电脑显卡）
    [switch]$WithComfy
)

if ([string]::IsNullOrEmpty($Work)) { $Work = $PSScriptRoot }
$ErrorActionPreference = 'Stop'

$variantName = if ($WithComfy) { '手机电脑接入版' } else { '手机版' }
$stage   = Join-Path $Work "AstrBot插件分享包-$variantName"
$history = Join-Path $Work "分享包历史-$variantName"
$zip     = Join-Path $Work "AstrBot插件分享包-$variantName.zip"
$src     = 'F:\NapCatDesk\ASTRBOT\data\plugins'
$patchSrc = Join-Path $Work '_probe\P5-help.py.patch'

# 手机版清单：去掉 awake（仅 Windows）
$plugins = @(
    'astrbot_plugin_ncd_monitor',
    'astrbot_plugin_ncd_toolbox',
    'astrbot_plugin_ncd_patch',
    'astrbot_plugin_ncd_extra',
    'astrbot_plugin_ncd_session',
    'astrbot_plugin_ncd_catchup',
    'astrbot_plugin_ncd_inject',
    'astrbot_plugin_ncd_memory',
    'astrbot_plugin_ncd_welcome'
    'astrbot_plugin_ncd_feedback', 'astrbot_plugin_ncd_link'
)
# 电脑接入版才带 comfy：它靠 comfy_host 把出图交给电脑的显卡。
if ($WithComfy) { $plugins += 'astrbot_plugin_ncd_comfy' }

# ── 1. 读版本号 ────────────────────────────────────────────────────────
$versions = @{}
foreach ($p in $plugins) {
    $mf = Join-Path $src "$p\metadata.yaml"
    if (-not (Test-Path $mf)) { throw "找不到 $mf" }
    $hit = Select-String -Path $mf -Pattern '^version:\s*(\S+)' | Select-Object -First 1
    if (-not $hit) { throw "$mf 里没有 version 字段" }
    $versions[$p] = $hit.Matches[0].Groups[1].Value
}

$verParts = @()
for ($i = 0; $i -lt $plugins.Count; $i++) {
    $prefix = if ($i -eq 0) { 'v' } else { 'm' }
    $verParts += "$prefix$($versions[$plugins[$i]])"
}
$verTag = $(if ($WithComfy) { 'pclink_' } else { 'phone_' }) + ($verParts -join '_')

# ── 2. 同步文件 ────────────────────────────────────────────────────────
$stagePlugins = Join-Path $stage '插件'
if (Test-Path $stagePlugins) {
    Get-ChildItem $stagePlugins -Directory -Force |
        Where-Object { $_.Name -notin $plugins } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
}
foreach ($p in $plugins) {
    $excl = @('__pycache__','thumbcache','data','patches')
    robocopy (Join-Path $src $p) (Join-Path $stage "插件\$p") /MIR `
        /XD $excl /XF '测试-*' '*.bak-*' '*.bak' /NFL /NDL /NJH /NJS /NP | Out-Null
    # ★ 2026-11-02：robocopy 的 1~7 是"正常/有跳过"，>=8 才是真失败。
    #   原来 | Out-Null 把退出码吞了，缺文件的包照样打出来并显示成功。
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败（$p）: exit $LASTEXITCODE" }
}

# 手机补丁单独一个目录，别和电脑版混
$patchDir = Join-Path $stage '源码补丁-手机'
if (Test-Path $patchDir) { Remove-Item -LiteralPath $patchDir -Recurse -Force }
New-Item -ItemType Directory -Path $patchDir -Force | Out-Null
if (Test-Path $patchSrc) {
    Copy-Item -LiteralPath $patchSrc -Destination (Join-Path $patchDir 'P5-help.py.patch') -Force
} else {
    Write-Warning "找不到 P5 补丁：$patchSrc"
}

Get-ChildItem $stage -Recurse -Directory -Force |
    Where-Object { $_.Name -in @('data', '__pycache__', 'thumbcache') } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }

# ★ 2026-11-02：测试脚本和旧版备份不属于交付物（里面还有本机路径/内网 IP），
#   即便 robocopy 的 /XF 漏了，这里也再兜一刀。
Get-ChildItem $stage -Recurse -File -Force |
    Where-Object { $_.Name -like '测试-*' -or $_.Name -like '*.bak-*' -or $_.Name -like '*.bak' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

# ── 3. 版本清单 ────────────────────────────────────────────────────────
$builtAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
if ($WithComfy) {
    $typeLine = "包类型  : 手机电脑接入版（手机跑 bot + 电脑出图，泡泡版 proot + Ubuntu）"
    $intro = @(
        "本包 = 手机版 + comfy（远程出图）。bot 住手机，出图交给电脑的显卡。",
        "comfy 装完默认是【禁用】状态，配好 comfy_host 再启用（详见安装说明）。",
        "同时附了电脑端 comfy_launcher.py —— 它负责按需启动 ComfyUI、闲着自动关。"
    )
} else {
    $typeLine = "包类型  : 手机移植版（纯手机，泡泡版 proot + Ubuntu）"
    $intro = @(
        "本包是纯手机版：不含 comfy，也不依赖电脑，装完自己就能跑。",
        "要「手机 bot + 电脑显卡出图」，请改用【手机电脑接入版】那个包。"
    )
}
$lines = @(
    "打包时间: $builtAt",
    $typeLine,
    "包版本  : $verTag",
    "",
    "不含 awake（仅 Windows 有效）。"
) + $intro + @(
    ""
)
foreach ($p in $plugins) { $lines += ("{0,-34} v{1}" -f $p, $versions[$p]) }
if ($WithComfy) {
    $lines += @(
        "",
        "★ comfy 远程出图：手机端 bot + 电脑显卡。",
        "  电脑启动 ComfyUI：python main.py --port 8199 --listen 0.0.0.0",
        "  或者跑 comfy_launcher.py（推荐，按需启动 + 空闲自动关）",
        "  插件配置：comfy_host=电脑IP（热点是 192.168.137.1），comfy_port=8199",
        "  comfy_launcher=http://电脑IP:8200（配了就能自动拉起）",
        "  电脑防火墙需放行 8199 / 8200。手机与电脑要在同一网段。"
    )
}
$lines += @(
    "",
    "★ 推荐在 agent 的辅助下安装（proot/端口/补丁/保活容易卡住）",
    "",
    "历史包都在：分享包历史-$variantName\",
    "回档方法  ：解压旧包，覆盖 /root/AstrBot/data/plugins 下同名目录。"
)
# ★ 用 .NET 写，避免 PowerShell 5.1 的 -Encoding UTF8 加 BOM
#   （BOM 在 Linux 的 cat/less 里会显示成小点，虽不影响解析但不干净）
[IO.File]::WriteAllText((Join-Path $stage '版本.txt'), ($lines -join "`r`n"), (New-Object Text.UTF8Encoding($false)))

$installNote = @'
AstrBot 插件分享包 · 手机移植版 · 安装说明
==========================================

★ 推荐在 agent（AI 助手）的辅助下安装 ★

手机移植要穿过 proot 容器、端口转发、源码补丁和保活设置这几道关，
每一步都可能卡在权限或路径上（比如 /sdcard 在容器里是 700 root:root，
电池和温度读不到）。有 agent 在旁边，可以边做边验证、出错时直接看日志
定位，比照着文档硬敲省事得多。

这是给【泡泡版 AstrBot（proot + Ubuntu 容器）】用的，和电脑版区分：
  · comfy 已纳入（远程模式）—— 手机端 bot 可以把出图交给电脑的显卡，
    配置 comfy_host 指向电脑 IP 即可。默认禁用，不配也不会报错。
  · 不含 awake —— 仅 Windows 有效

一、装到哪里
  容器内：/root/AstrBot/data/plugins/

二、推荐装法（电脑→手机）
  1. 泡泡版里先「停止 AstrBot」
  2. 电脑上起一个临时 HTTP 服务：
       cd 解压目录
       python -m http.server 8000
  3. 手机 USB 连电脑，把端口送进容器：
       adb reverse tcp:8000 tcp:8000
  4. 泡泡版 → 终端，下载并解压：
       curl -s -O http://127.0.0.1:8000/包名.zip
       cd /root/AstrBot/data/plugins && unzip -o /root/包名.zip
     （没有 unzip 就先 apt install unzip）
  5. 泡泡版 → 启动 AstrBot，面板 → 插件，确认版本

  为什么不用 adb push 到 /sdcard：proot 里的 fake root 进不去
  /sdcard（700 root:root），curl 走 localhost 更省事。

三、从电脑搬家（可选）
  只搬这些：
    cmd_config.json、plugins.json、data_v4.db（含 -wal/-shm）
    config/、plugins/、plugin_data/、knowledge_base/、attachments/
  不要搬这些（会拖慢或污染）：
    temp/、logs/、plugins_disabled/
    plugin_data/ncd_toolbox/browser_profile/（Edge 用户数据，手机上没用）
  替换前先把容器里的 data 改名：
    mv /root/AstrBot/data /root/AstrBot/data.old

四、平台配置（cmd_config.json 的 platform 段）
  ws_reverse_port  要和 NapCat 的 onebot11.json 里 url 的端口一致（默认 6199）
  ws_reverse_token 要和 NapCat 的 token 一致

五、源码补丁（源码补丁-手机/P5-help.py.patch）
  作用：4.29 起 /help 只列内置指令，这个补丁让插件指令也显示。
  应用：
    cd /root/AstrBot
    git apply -p1 /path/to/P5-help.py.patch
  撤销：
    git apply -R -p1 /path/to/P5-help.py.patch
  注意：路径前缀是 astrbot/（源码布局）。若 AstrBot 是 pip 装到
  site-packages，前缀要改成 builtin_stars/，用 -p1 之外的方式应用。

六、保活（手机长期跑的关键）
  1. 泡泡版 设置 里确认「电池优化豁免：已授权」
  2. 系统设置 → 电池 → 启动管理 → AstrBot泡泡版 →
     允许「自启动 / 关联启动 / 后台活动」
  3. 泡泡版自带前台服务（astrbot_keep_alive_channel），不要关它的通知
  4. 重启手机后需要手动打开泡泡版点一次「启动 AstrBot」
     （泡泡版没有自动拉起 AstrBot 的选项）

七、手机上的已知限制
  · GPU / 显存 / 温度 / 功耗：读不到（proot 权限）
  · 电池：读不到（/sys/class/power_supply Permission denied）
  · comfy 出图：不可用（要出图得另配局域网远程 ComfyUI）
  · toolbox「抓网页正文」：不可用（依赖 Edge）
  · /设备状态：monitor 1.7.24+ 会自动识别手机容器并切手机版显示

八、回档
  解压旧分享包，覆盖 /root/AstrBot/data/plugins 下同名目录，重载插件。
'@
if ($WithComfy) {
    $installNote = $installNote.Replace(
        '  · comfy 出图：不可用（要出图得另配局域网远程 ComfyUI）',
        '  · comfy 出图：可用，但需要电脑开着并跑 ComfyUI（见第五节）')
} else {
    $installNote = $installNote.Replace(
        '  · comfy 出图：不可用（要出图得另配局域网远程 ComfyUI）',
        '  · comfy：本包不含。要「手机 bot + 电脑显卡出图」，请用【手机电脑接入版】')
}
$installNote = $installNote -replace '手机移植版 · 安装说明', "$variantName · 安装说明"
[IO.File]::WriteAllText((Join-Path $stage "安装说明-$variantName.txt"), $installNote, (New-Object Text.UTF8Encoding($false)))

# 电脑接入版顺手把拉起器脚本也放进去（电脑侧要用）
if ($WithComfy) {
    $launcherSrc = 'C:\Users\KIRUSI\Desktop\comfy_launcher.py'
    if (Test-Path $launcherSrc) {
        Copy-Item -LiteralPath $launcherSrc -Destination (Join-Path $stage 'comfy_launcher.py') -Force
    } else {
        Write-Warning "没找到 $launcherSrc，包里将不含拉起器脚本"
    }
}

# ── 4. 打 zip + 修正路径分隔符 ─────────────────────────────────────────
$tmp = Join-Path $Work 'AstrBot插件分享包-手机版.tmp.zip'
if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Force }
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $tmp -CompressionLevel Optimal
Move-Item -LiteralPath $tmp -Destination $zip -Force

# ★ Compress-Archive 把路径写成 `插件\x\main.py`（反斜杠）。Linux 解压时
#   反斜杠是合法文件名字符，结果会变成一个名字里带 \ 的单文件，整个包作废。
#   这里用 Python 重打一遍：正斜杠 + UTF-8 文件名标志。
#   ★ 归档必须放在重打【之后】—— 否则历史包里存的是反斜杠版，Linux 解压会坏。
$py = 'F:\NapCatDesk\ASTRBOT\.venv\Scripts\python.exe'
$repack = Join-Path $Work 'repack_phone.py'
if ((Test-Path $py) -and (Test-Path $repack)) {
    & $py $repack $stage $zip | Out-Null
    # ★ 2026-11-02：重打失败必须报错（原来 | Out-Null 吞掉退出码，
    #   接入版没被重打也会"打包成功"，Linux 解压出来是带反斜杠的坏文件名）
    if ($LASTEXITCODE -ne 0) { throw "Python 重打失败: exit $LASTEXITCODE" }
    "已用 Python 重打（正斜杠 + UTF-8 文件名）"
} else {
    Write-Warning "跳过重打：找不到 $py 或 $repack（包里的路径可能仍是反斜杠）"
}

# ── 5. 归档（存的是重打后的成品）──────────────────────────────────────
$newHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
if (-not (Test-Path $history)) { New-Item -ItemType Directory -Path $history -Force | Out-Null }
$prev = Get-ChildItem $history -Filter '*.zip' -File |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
$identical = $prev -and `
    ((Get-FileHash -LiteralPath $prev.FullName -Algorithm SHA256).Hash -eq $newHash)

if ($identical -and -not $Force) {
    "内容与上一份历史包完全相同（$($prev.Name)），跳过归档。要强制留档请加 -Force。"
} else {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $archPath = Join-Path $history "$variantName-$verTag-$stamp.zip"
    $n = 1
    while (Test-Path $archPath) { $n++; $archPath = Join-Path $history "$variantName-$verTag-$stamp-$n.zip" }
    Copy-Item -LiteralPath $zip -Destination $archPath
    "已归档历史包：$(Split-Path $archPath -Leaf)"
}

# ── 6. 汇总 ────────────────────────────────────────────────────────────
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z = [System.IO.Compression.ZipFile]::OpenRead($zip)
"最新包  : $(Split-Path $zip -Leaf)  $((Get-Item $zip).Length) 字节 / $($z.Entries.Count) 条目"
$z.Dispose()
"版本    : " + (($plugins | ForEach-Object { "$_ v$($versions[$_])" }) -join '  ·  ')
""
$hist = Get-ChildItem $history -Filter '*.zip' -File | Sort-Object Name
"历史包（共 $($hist.Count) 份）："
foreach ($h in $hist) {
    "  {0,-54} {1,10:N0} 字节  {2}" -f $h.Name, $h.Length, $h.LastWriteTime.ToString('MM-dd HH:mm')
}

exit 0
