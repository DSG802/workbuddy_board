<#
  publish_release_win.ps1 - 把 Windows 产物上传到 GitHub Release。

  Release 附件只能走 API，本机没有 gh CLI，这里用 REST 直连。需要 fine-grained
  PAT 的 Contents 读写权限。Token 只在本进程内使用，不写盘、不回显。

  上传失败会留下 state=starter 的半成品资产，需要手动删：
    GET    /repos/{owner}/{repo}/releases/{id}/assets
    DELETE /repos/{owner}/{repo}/releases/assets/{asset_id}

  用法（执行策略禁止直接跑 .ps1，需带 -ExecutionPolicy Bypass）：
    powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\publish_release_win.ps1 `
      -Token "github_pat_xxx" -Tag "win-v8-20261114" -AssetPath "dist\WorkBuddyBoard-win-x64-v8-20261114.zip"
#>
param(
    [Parameter(Mandatory = $true)][string]$Token,
    [Parameter(Mandatory = $true)][string]$AssetPath,
    [string]$Tag = "win-v6-20261113",
    [string]$ReleaseName = "",
    [string]$Owner = "DSG802",
    [string]$Repo = "workbuddy-board",
    [string]$TargetBranch = "main",
    [string]$NotesPath = ""
)

$ErrorActionPreference = "Stop"
$api = "https://api.github.com"
$up = "https://uploads.github.com"

$hdr = @{
    Authorization          = "Bearer $Token"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "wbb-release-script"
}

function Step($n, $msg) { Write-Host "[$n/5] $msg" -ForegroundColor Cyan }

# ---------- 1. 校验 token ----------
Step 1 "验证 token"
try {
    $me = Invoke-RestMethod -Uri "$api/user" -Headers $hdr -Method Get
} catch {
    Write-Host "  X token 无效：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "    排查顺序：复制是否完整 -> 是否被 revoke -> 是否选了本仓库 -> Contents 是否 Read and write"
    exit 1
}
Write-Host "  身份: $($me.login)"

# ---------- 2. 校验仓库与写权限 ----------
Step 2 "检查仓库权限"
try {
    $r = Invoke-RestMethod -Uri "$api/repos/$Owner/$Repo" -Headers $hdr -Method Get
} catch {
    Write-Host "  X 无法访问 $Owner/$Repo（token 可能没勾这个仓库）" -ForegroundColor Red
    exit 1
}
if (-not $r.permissions.push) {
    Write-Host "  X 缺少写入权限，把 Contents 改成 Read and write 后重试" -ForegroundColor Red
    exit 1
}
Write-Host "  $($r.full_name)  private=$($r.private)  push=$($r.permissions.push)"

# ---------- 3. 校验本地文件 ----------
Step 3 "检查待发布文件"
if (-not (Test-Path $AssetPath)) {
    Write-Host "  X 找不到 $AssetPath" -ForegroundColor Red
    exit 1
}
$f = Get-Item $AssetPath
$hash = (Get-FileHash $AssetPath -Algorithm SHA256).Hash.ToLower()
Write-Host ("  {0}  {1:N2} MB" -f $f.Name, ($f.Length / 1MB))
Write-Host "  SHA-256 $hash"

if (-not $ReleaseName) { $ReleaseName = $Tag }
$notes = "Windows x64 build. SHA-256: $hash"
if ($NotesPath -and (Test-Path $NotesPath)) {
    $notes = (Get-Content -Raw -Encoding UTF8 $NotesPath) + "`n`nSHA-256: $hash"
}

# ---------- 4. 建 Release（已存在则复用）----------
Step 4 "创建 Release $Tag"
$rel = $null
try {
    $rel = Invoke-RestMethod -Uri "$api/repos/$Owner/$Repo/releases/tags/$Tag" -Headers $hdr -Method Get
    Write-Host "  已存在（id=$($rel.id)），复用，不重复建"
} catch {
    $payload = @{
        tag_name         = $Tag
        target_commitish = $TargetBranch
        name             = $ReleaseName
        body             = $notes
        draft            = $false
        prerelease       = $false
    } | ConvertTo-Json -Depth 5
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $rel = Invoke-RestMethod -Uri "$api/repos/$Owner/$Repo/releases" -Headers $hdr -Method Post `
        -Body $bytes -ContentType "application/json; charset=utf-8"
    Write-Host "  已创建（id=$($rel.id)）"
}

# ---------- 5. 上传资产 ----------
Step 5 "上传资产"
$existing = Invoke-RestMethod -Uri "$api/repos/$Owner/$Repo/releases/$($rel.id)/assets" -Headers $hdr -Method Get
$same = $existing | Where-Object { $_.name -eq $f.Name }
if ($same) {
    Write-Host "  已存在同名资产（id=$($same.id)），先删除"
    Invoke-RestMethod -Uri "$api/repos/$Owner/$Repo/releases/assets/$($same.id)" -Headers $hdr -Method Delete | Out-Null
}

$uhdr = @{
    Authorization          = "Bearer $Token"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "wbb-release-script"
}
$url = "$up/repos/$Owner/$Repo/releases/$($rel.id)/assets?name=$([uri]::EscapeDataString($f.Name))"
$asset = Invoke-RestMethod -Uri $url -Headers $uhdr -Method Post -ContentType "application/zip" `
    -InFile $f.FullName -TimeoutSec 900

Write-Host ""
Write-Host "完成" -ForegroundColor Green
Write-Host "  Release: $($rel.html_url)"
Write-Host "  资产   : $($asset.name)  $([math]::Round($asset.size/1MB,2)) MB  ($($asset.state))"
Write-Host "  下载   : $($asset.browser_download_url)"
Write-Host "  SHA256 : $hash"
Write-Host ""
Write-Host "别忘了补一个 SHA256SUMS.txt 附件（COLLAB.md §四 要求）。"
