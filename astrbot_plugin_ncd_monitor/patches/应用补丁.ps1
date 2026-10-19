# ==============================================================================
#  AstrBot 源码补丁 P1~P4 —— 一键应用 + 校验
#
#  用法：
#      powershell -ExecutionPolicy Bypass -File 应用补丁.ps1 `
#          -AstrBotPkg "D:\AstrBot\.venv\Lib\site-packages\astrbot"
#
#      -AstrBotPkg 也可以直接给 AstrBot 安装根目录（脚本会自己补上
#      .venv\Lib\site-packages\astrbot）。
#
#      只想看看能不能打、不真改文件：加 -DryRun
#
#  它做什么：
#      ① 按 校验值.json 检查每个文件当前状态（还没打 / 已经打过 / 状态不对）
#      ② 干跑（git apply --check 或 patch --dry-run），确认能干净打上
#      ③ 全部通过后才真打
#      ④ 打完按 SHA256 前 16 位校验，逐个报 OK/FAIL
#
#  ★ 不会盲目开打：任何一步失败就停下，并且明确告诉你哪个文件状态不对。
# ==============================================================================

param(
    [string]$AstrBotPkg = "",
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8

$HERE = $PSScriptRoot
if (-not $HERE) { $HERE = (Get-Location).Path }
$MANIFEST = Join-Path $HERE '校验值.json'

function Say  { param($t, $c = 'Gray')  Write-Host $t -ForegroundColor $c }
function Ok   { param($t) Write-Host "  [OK]   $t" -ForegroundColor Green }
function Bad  { param($t) Write-Host "  [FAIL] $t" -ForegroundColor Red }
function Info { param($t) Write-Host "  [--]   $t" -ForegroundColor DarkGray }

# ------------------------------------------------------------------ 前置检查
Say ""
Say "================================================================" Cyan
Say "  AstrBot 源码补丁 P1~P4" Cyan
Say "================================================================" Cyan
Say ""

if (-not (Test-Path $MANIFEST)) {
    Bad "找不到 校验值.json（应在 $HERE）"
    exit 1
}
$specs = Get-Content $MANIFEST -Raw -Encoding UTF8 | ConvertFrom-Json

# 允许传 AstrBot 根目录，也允许直接传包目录
function Resolve-Pkg([string]$p) {
    if (-not $p) { return "" }
    $p = $p.Trim().Trim('"').Trim("'").Trim()
    if (-not $p) { return "" }
    if (Test-Path (Join-Path $p 'core\astr_main_agent.py')) { return $p }
    $cand = Join-Path $p '.venv\Lib\site-packages\astrbot'
    if (Test-Path (Join-Path $cand 'core\astr_main_agent.py')) { return $cand }
    return ""
}

$pkg = Resolve-Pkg $AstrBotPkg
if (-not $pkg) {
    Bad "没找到 AstrBot 的包目录。"
    Say ""
    Say "  请用 -AstrBotPkg 指定，两种写法都行：" Yellow
    Say "     -AstrBotPkg `"D:\AstrBot`"                                   ← 安装根目录" Gray
    Say "     -AstrBotPkg `"D:\AstrBot\.venv\Lib\site-packages\astrbot`"     ← 或包目录" Gray
    Say ""
    Say "  不确定装在哪？在 AstrBot 面板 → 插件 → 链路监控的日志里搜" Gray
    Say "  「环境解析」，或者在 AstrBot 日志里找 site-packages 路径。" Gray
    Say ""
    exit 1
}
Ok "AstrBot 包目录：$pkg"
Say ""

# ------------------------------------------------------------------ 挑工具
$git = (Get-Command git   -ErrorAction SilentlyContinue).Source
$pat = (Get-Command patch -ErrorAction SilentlyContinue).Source
if ($git)      { Info "用 git 打补丁：$git" }
elseif ($pat)  { Info "没有 git，改用 patch：$pat" }
else {
    Bad "既没有 git 也没有 patch。"
    Say ""
    Say "  这个脚本需要其中一样。两条出路：" Yellow
    Say "    1) 装个 git（Git for Windows 自带 bash，安装时勾选就能用）" Gray
    Say "    2) 不用脚本 —— 照「补丁说明.md」里的说明手改，每个补丁只有几行" Gray
    Say ""
    exit 1
}
Say ""

function Get-Sha16([string]$path) {
    return (Get-FileHash $path -Algorithm SHA256).Hash.Substring(0, 16).ToLower()
}

# ★ 调外部命令时【必须临时关掉 $ErrorActionPreference='Stop'】：
#   git/patch 把报错写到 stderr，而 `2>&1` 会把 stderr 变成 ErrorRecord，
#   在 Stop 模式下直接变成终止错误 —— 那样"打不上"就变成了脚本崩掉，
#   连退出码都看不到。所以这里局部改成 Continue，并把输出留起来备用。
#
# ★ git 必须显式关掉换行转换（-c core.autocrlf=false）：
#   AstrBot 的源码是【纯 LF】，而 git apply 在【不在 git 仓库里】时会
#   按 autocrlf 的默认行为把 LF 写成 CRLF —— 补丁内容确实打对了，
#   但文件字节全变，SHA256 校验必然对不上。
#   （实测：不加这个参数，1783 行的文件会变成 1783 个 CRLF、0 个 LF。）
$script:LastPatchOutput = ""
function Invoke-Patch([string]$patchFile, [switch]$CheckOnly) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($git) {
            $args = @(
                '-C', $pkg,
                '-c', 'core.autocrlf=false',
                '-c', 'core.safecrlf=false',
                '-c', 'core.eol=lf',
                'apply'
            )
            if ($CheckOnly) { $args += '--check' }
            $args += @('-p1', $patchFile)
            $out = & $git @args 2>&1
        }
        else {
            $args = @('-d', $pkg, '-p1')
            if ($CheckOnly) { $args += '--dry-run' }
            $args += @('--forward', '--silent', '-i', $patchFile)
            $out = & $pat @args 2>&1
        }
        $script:LastPatchOutput = ($out | Out-String).Trim()
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $old
    }
}

# ------------------------------------------------------------------ 先看状态
Say "---- 1. 检查当前状态 ----" Magenta

$todo = @()
foreach ($s in $specs) {
    $target = Join-Path $pkg ($s.rel -replace '/', '\')
    if (-not (Test-Path $target)) {
        Bad "$($s.id)  目标文件不存在：$($s.rel)"
        Say "        这个 AstrBot 版本里没有它，可能版本差太多。" Gray
        exit 1
    }
    $now = Get-Sha16 $target
    if ($now -eq $s.sha_after) {
        Info "$($s.id)  已经是打过补丁的状态（$now），跳过"
    }
    elseif ($now -eq $s.sha_before) {
        Ok "$($s.id)  未打补丁，可以打（$now）"
        $todo += $s
    }
    else {
        Bad "$($s.id)  $($s.rel) 的哈希对不上："
        Say "        现在        = $now" Gray
        Say "        未打补丁应为 = $($s.sha_before)" Gray
        Say "        打过应为     = $($s.sha_after)" Gray
        Say "        说明这个文件被别的东西改过（或 AstrBot 版本不同）。" Yellow
        Say "        → 不要用脚本硬打。照「补丁说明.md」手动对照着改。" Yellow
        exit 1
    }
}

if (-not $todo.Count) {
    Say ""
    Ok "四个补丁都已经打过了，没什么可做的。"
    Say ""
    Say "  别忘了打完要【重启 AstrBot】。" Yellow
    exit 0
}
Say ""

# ------------------------------------------------------------------ 干跑
Say "---- 2. 干跑（不改文件）----" Magenta
foreach ($s in $todo) {
    $patch = Join-Path $HERE $s.patch
    if (-not (Test-Path $patch)) { Bad "$($s.id)  找不到补丁文件 $($s.patch)"; exit 1 }
    $rc = Invoke-Patch $patch -CheckOnly
    if ($rc -eq 0) { Ok "$($s.id)  $($s.patch)  可以干净打上" }
    else {
        Bad "$($s.id)  $($s.patch)  打不上（退出码 $rc）"
        if ($script:LastPatchOutput) {
            Say "        ---- 补丁工具说 ----" Gray
            foreach ($line in ($script:LastPatchOutput -split "`r?`n")) {
                if ($line.Trim()) { Say "        $line" Gray }
            }
        }
        Say "        通常是 AstrBot 版本差异，照着「补丁说明.md」手改更稳。" Yellow
        exit 1
    }
}

if ($DryRun) {
    Say ""
    Ok "干跑通过（-DryRun，没有改动任何文件）。"
    Say ""
    exit 0
}
Say ""

# ------------------------------------------------------------------ 真打
Say "---- 3. 应用 ----" Magenta
foreach ($s in $todo) {
    $patch = Join-Path $HERE $s.patch
    $rc = Invoke-Patch $patch
    if ($rc -ne 0) { Bad "$($s.id)  应用失败（退出码 $rc）"; exit 1 }
    Ok "$($s.id)  已应用 $($s.patch)"
}
Say ""

# ------------------------------------------------------------------ 校验
Say "---- 4. 校验 ----" Magenta
$allOk = $true
foreach ($s in ($specs | Where-Object { $_.sha_after })) {
    $target = Join-Path $pkg ($s.rel -replace '/', '\')
    $now = Get-Sha16 $target
    if ($now -eq $s.sha_after) {
        Ok "$($s.id)  $($s.rel)  $now"
    }
    elseif ($now -eq $s.sha_before) {
        Bad "$($s.id)  $($s.rel)  还是未打补丁的状态（$now）"
        $allOk = $false
    }
    else {
        Bad "$($s.id)  $($s.rel)  哈希异常：$now"
        $allOk = $false
    }
}

Say ""
if ($allOk) {
    Say "================================================================" Green
    Say "  全部 OK —— 四个补丁都已就位" Green
    Say "================================================================" Green
    Say ""
    Say "  ⚠️  现在【必须重启 AstrBot】才生效。" Yellow
    Say "      改的是 .py 源码，配置那种热重载不适用。" Yellow
    Say ""
} else {
    Say "  有补丁没打上，看上面对应那行。" Red
    exit 1
}
