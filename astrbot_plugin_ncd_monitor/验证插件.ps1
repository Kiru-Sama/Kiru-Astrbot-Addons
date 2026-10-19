# ==============================================================================
#  链路监控插件 —— 重启后的验证脚本
#  用法：右键 → 使用 PowerShell 运行
#        （或命令行：pwsh -File 验证插件.ps1 [-Log <日志路径>]）
#
#  ★ 不带任何硬编码路径：插件目录从本脚本自己的位置推导，
#    日志路径默认按 <插件目录>\..\..\logs\astrbot.log 推导，
#    推导不对就用 -Log 参数指定（拷贝到别人机器上也能直接用）。
# ==============================================================================

param(
    [string]$Log = ""
)

$ErrorActionPreference = 'SilentlyContinue'
$OutputEncoding = [System.Text.Encoding]::UTF8

# 脚本就躺在插件目录里 → 插件目录 = 脚本所在目录
$PLUGIN = $PSScriptRoot
if (-not $PLUGIN) { $PLUGIN = (Get-Location).Path }

# 插件目录形如 <AstrBot>\data\plugins\<插件名>，所以往上三层就是 <AstrBot>\data
if (-not $Log) {
    $dataDir = Split-Path (Split-Path $PLUGIN -Parent) -Parent
    $Log = Join-Path $dataDir "logs\astrbot.log"
}

# 所有日志行打印前都截断，避免超长行把输出刷爆
function Show-Line {
    param([string]$Text, [string]$Color = "Gray", [int]$Max = 160)
    $t = ($Text -replace '\x1b\[[0-9;]*m', '').Trim()
    if ($t.Length -gt $Max) { $t = $t.Substring(0, $Max) + " ...(截断)" }
    Write-Host ("         " + $t) -ForegroundColor $Color
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  链路监控插件 验证" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

$ok = 0
$bad = 0

# ---------------------------------------------------------------- 1. 文件
Write-Host "---- 1. 插件文件 ----" -ForegroundColor Magenta
$need = [ordered]@{
    "metadata.yaml"                   = "元数据"
    "main.py"                         = "插件主体"
    "pages\monitor\index.html"        = "前端页面"
    ".astrbot-plugin\i18n\zh-CN.json" = "中文标题"
    "README.md"                       = "说明文档"
}
foreach ($rel in $need.Keys) {
    $full = Join-Path $PLUGIN $rel
    if (Test-Path $full) {
        $kb = [math]::Round((Get-Item $full).Length / 1KB, 1)
        Write-Host ("  [OK]   {0,-34} {1,7} KB   {2}" -f $rel, $kb, $need[$rel]) -ForegroundColor Green
        $ok++
    } else {
        Write-Host ("  [缺失] {0,-34}          {1}" -f $rel, $need[$rel]) -ForegroundColor Red
        $bad++
    }
}
Write-Host ""

# ---------------------------------------------------------------- 2. 加载情况
Write-Host "---- 2. AstrBot 有没有加载它 ----" -ForegroundColor Magenta
if (-not (Test-Path $LOG)) {
    Write-Host "  [错误] 找不到日志：$LOG" -ForegroundColor Red
    $bad++
} else {
    # 只取相关行，避免把整个日志读进内存
    $monLines = @(Select-String -Path $LOG -Pattern 'ncd-monitor|ncd_monitor' -Encoding UTF8)
    $monText  = ($monLines | ForEach-Object { $_.Line }) -join "`n"

    if ($monText -match '链路监控插件已加载') {
        Write-Host "  [OK]   插件已加载" -ForegroundColor Green
        $ok++
    } else {
        Write-Host "  [缺失] 没找到加载记录 —— 插件可能没被发现" -ForegroundColor Red
        Write-Host "         ① 重启过 AstrBot 吗（新插件必须重启）" -ForegroundColor Yellow
        Write-Host "         ② 面板插件页里能看到「链路监控」吗" -ForegroundColor Yellow
        $bad++
    }

    # ---- 三个运行时补丁（R1~R3）----
    if ($monText -match '已包装 LLMSummaryCompressor|压缩器已被包装过') {
        Write-Host "  [OK]   R1 已包装 LLMSummaryCompressor（能捕获压缩摘要）" -ForegroundColor Green
        $ok++
    } else {
        Write-Host "  [缺失] R1 没找到包装压缩器的记录 —— 压缩摘要会一直空白" -ForegroundColor Red
        Write-Host "         见《升级前必读》R1 节" -ForegroundColor Yellow
        $bad++
    }

    if ($monText -match '已包装 (\d+) 个 Provider 类的 text_chat') {
        $n = [int]$Matches[1]
        if ($n -ge 2) {
            Write-Host "  [OK]   R2 已包装 $n 个 Provider 类（能抓发给模型的完整上下文）" -ForegroundColor Green
            $ok++
        } else {
            Write-Host "  [异常] R2 只包装了 $n 个类 —— 通常应为 4 个" -ForegroundColor Red
            Write-Host "         只包到 1 个说明 provider source 模块没导入成功，" -ForegroundColor Yellow
            Write-Host "         右侧栏不会有数据。见《升级前必读》R2 节" -ForegroundColor Yellow
            $bad++
        }
    } elseif ($monText -match '新包装 Provider 子类') {
        $n = @(Select-String -Path $LOG -Pattern '新包装 Provider 子类' -Encoding UTF8 -ErrorAction SilentlyContinue).Count
        Write-Host "  [OK]   R2 已包装 $n 个 Provider 子类（惰性补包版本）" -ForegroundColor Green
        $ok++
    } elseif ($monText -match '已包装 Provider\.text_chat') {
        Write-Host "  [异常] R2 是【旧版】包装方式（只包基类）—— 抓不到任何上下文" -ForegroundColor Red
        Write-Host "         插件需要重载到新版（惰性补包）。见《升级前必读》R2 节" -ForegroundColor Yellow
        $bad++
    } else {
        Write-Host "  [缺失] R2 没找到包装 Provider.text_chat 的记录 —— 右侧栏会一直空白" -ForegroundColor Red
        Write-Host "         见《升级前必读》R2 节" -ForegroundColor Yellow
        $bad++
    }

    # R2 的前置步骤：预导入 provider source（失败会导致只包到基类）
    $pre = @(Select-String -Path $LOG -Pattern '预导入 provider source' -Encoding UTF8 -ErrorAction SilentlyContinue)
    if ($pre.Count) {
        $t = ($pre[-1].Line -replace '\x1b\[[0-9;]*m', '').Trim()
        $i = $t.IndexOf('预导入 provider source')
        if ($i -ge 0) {
            $msg = $t.Substring($i)
            if ($msg -match '失败 [1-9]') {
                Write-Host "  [提示] $msg" -ForegroundColor Yellow
            } else {
                Write-Host "  [OK]   $msg" -ForegroundColor Green
            }
        }
    }

    if ($monText -match '已包装 EstimateTokenCounter\.count_tokens') {
        Write-Host "  [OK]   R3 已包装 token 计数器（图片 765/张 计入压缩判断）" -ForegroundColor Green
        $ok++
    } else {
        Write-Host "  [缺失] R3 没找到包装 token 计数器的记录" -ForegroundColor Red
        Write-Host "         → 图片不会计入压缩判断，会无限累积且永远清不掉" -ForegroundColor Yellow
        Write-Host "         见《升级前必读》R3 节" -ForegroundColor Yellow
        $bad++
    }

    if ($monText -match '失败') {
        Write-Host "  [警告] 有 [ncd-monitor] 报错（最多显示 6 条）：" -ForegroundColor Yellow
        $monLines | Select-Object -Last 6 | ForEach-Object { Show-Line $_.Line "Yellow" }
        $bad++
    }

    $capLines = @(Select-String -Path $LOG -Pattern '捕获到第 .* 次上下文压缩' -Encoding UTF8)
    if ($capLines.Count -gt 0) {
        Write-Host ("  [OK]   已捕获过 {0} 次压缩" -f $capLines.Count) -ForegroundColor Green
        $capLines | Select-Object -Last 3 | ForEach-Object { Show-Line $_.Line "Gray" }
        $ok++
    } else {
        Write-Host "  [说明] 还没捕获到压缩 —— 正常，要聊够轮数才会压缩" -ForegroundColor Yellow
    }

    if ($monLines.Count -eq 0) {
        Write-Host "  [说明] 日志里完全没有 ncd-monitor 的痕迹" -ForegroundColor Yellow
    }
}
Write-Host ""

# ---------------------------------------------------------------- 3. 端口
Write-Host "---- 3. 相关服务是否在运行 ----" -ForegroundColor Magenta
# ★ 这里列的是这套组合的【常见默认端口】，纯属"看一眼谁没起来"，
#   不是断言 —— 你改了端口它就显示"未监听"，不影响插件本身。
#   端口不一致时：LM Studio / SnowLuma 的地址请在
#   AstrBot 面板 → 插件 → 链路监控 → 配置 里改。
foreach ($p in @(
    @{ Port = 6185;  Name = "AstrBot 面板" },
    @{ Port = 38978; Name = "AstrBot OneBot 反向 WS" },
    @{ Port = 5099;  Name = "SnowLuma WebUI" },
    @{ Port = 1234;  Name = "LM Studio" }
)) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $p.Port -EA SilentlyContinue | Select-Object -First 1
    if ($conn) {
        Write-Host ("  [OK]   {0,-26} {1}:{2}" -f $p.Name, $conn.LocalAddress, $p.Port) -ForegroundColor Green
        $ok++
    } else {
        Write-Host ("  [未监听] {0,-24} 端口 {1}" -f $p.Name, $p.Port) -ForegroundColor DarkGray
    }
}
Write-Host ""

# ---------------------------------------------------------------- 汇总
Write-Host "================================================================" -ForegroundColor Cyan
if ($bad -eq 0) {
    Write-Host "  检查通过（$ok 项）。" -ForegroundColor Green
    Write-Host ""
    Write-Host "  下一步：AstrBot 面板 → 插件页 → 找「链路监控」→ 点它的页面入口" -ForegroundColor White
} else {
    Write-Host "  有 $bad 项要处理（通过 $ok 项）。" -ForegroundColor Red
    Write-Host ""
    Write-Host "  排查顺序：" -ForegroundColor Yellow
    Write-Host "    1. 重启过 AstrBot 吗？新插件必须重启才会被扫描到。" -ForegroundColor Yellow
    Write-Host "    2. 面板 → 插件页，能看到「链路监控」吗？" -ForegroundColor Yellow
    Write-Host "    3. 日志里搜 ncd-monitor 看报错" -ForegroundColor Yellow
    Write-Host "    4. 自述文档：$PLUGIN\README.md" -ForegroundColor Yellow
}
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""
Read-Host "按回车退出"
