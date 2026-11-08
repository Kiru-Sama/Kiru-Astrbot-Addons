# -*- coding: utf-8 -*-
# 把 v2026.10.30.4 里的 comfy_launcher.py 换成带 idle 看门狗的新版
$ErrorActionPreference = 'Stop'

$req = "protocol=https`nhost=github.com`n`n"
$out = $req | git credential fill 2>&1
$token = (($out | Where-Object { $_ -match '^password=' }) -replace '^password=','')
$h = @{
    Authorization = "Bearer $token"
    'User-Agent'  = 'dsh-agent'
    Accept        = 'application/vnd.github+json'
}
$repo = 'Kiru-Sama/Kiru-Astrbot-Addons'
$tag  = 'v2026.10.30.4'

$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$tag" -Headers $h -TimeoutSec 30
"release id=$($rel.id)"

# 删旧的 comfy_launcher.py 资产
foreach ($a in $rel.assets) {
    if ($a.name -eq 'comfy_launcher.py') {
        try {
            Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/assets/$($a.id)" -Method Delete -Headers $h -TimeoutSec 30 | Out-Null
            "deleted old asset id=$($a.id)"
        } catch { "delete ERR: $($_.Exception.Message)" }
    }
}

# 传新版
$path = 'E:\comfyuiMINI\comfy_launcher.py'
$bytes = [IO.File]::ReadAllBytes($path)
$url = "https://uploads.github.com/repos/$repo/releases/$($rel.id)/assets" + '?name=comfy_launcher.py'
$res = Invoke-RestMethod -Uri $url -Method Post -Headers $h -Body $bytes -ContentType 'text/x-python' -TimeoutSec 120
"uploaded: $($res.name)  $($res.size) bytes"

$rel2 = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$tag" -Headers $h -TimeoutSec 30
""
"=== 复核 ==="
foreach ($a in $rel2.assets) { "  $($a.name)  $($a.size) bytes" }
