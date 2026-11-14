<#
  stamp_build_win.ps1 - 把构建时间戳写进产物，供客户端页脚显示。

  app/checkin.py 的 BUILD_TAG 是模块级常量，运行时读环境变量；构建进程里 set
  的变量传不到用户双击启动的进程，所以改写产物中 checkin.py 的默认值。只改
  dist 下的副本，仓库源码不动。显式指定 BUILD_TAG 时环境变量仍然优先。

  用法：
    .\tools\stamp_build_win.ps1 -AppDir dist\WorkBuddyBoard\_internal\app
    .\tools\stamp_build_win.ps1 -AppDir ... -Tag 2026-11-14.1439
#>
param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [string]$Tag = ''
)

$ErrorActionPreference = 'Stop'

if (-not $Tag) { $Tag = Get-Date -Format 'yyyy-MM-dd.HHmm' }
if ($Tag -notmatch '^[0-9A-Za-z._:-]+$') { throw "Tag 含可疑字符：$Tag" }

$file = Join-Path $AppDir 'checkin.py'
if (-not (Test-Path $file)) { throw "找不到 $file" }

$raw = [System.IO.File]::ReadAllText($file, [System.Text.Encoding]::UTF8)
$pattern = 'BUILD_TAG = os\.environ\.get\("BUILD_TAG", "[^"]*"\)'
$m = [regex]::Match($raw, $pattern)
if (-not $m.Success) { throw "在 $file 里没找到 BUILD_TAG 赋值行，模板可能改了" }

$old = $m.Value
$new = 'BUILD_TAG = os.environ.get("BUILD_TAG", "' + $Tag + '")'

# 原文件没有 BOM，写回时也不带，避免和仓库里那份产生无谓差异
# （android 侧需要 app/checkin.py 与源码一致，虽然只改默认值也要讲究）
[System.IO.File]::WriteAllText($file, $raw.Replace($old, $new), (New-Object System.Text.UTF8Encoding($false)))

# 同时清掉同目录 __pycache__ 里的 checkin 编译缓存。只改 .py 不清 .pyc，
# 解释器可能仍加载旧值；缓存也不该进安装包，会让按 md5 校验 .py 的做法失真。
$cache = Join-Path $AppDir '__pycache__'
if (Test-Path $cache) {
    $pycs = Get-ChildItem -Path $cache -Filter 'checkin.*.pyc' -ErrorAction SilentlyContinue
    foreach ($p in $pycs) {
        Remove-Item -LiteralPath $p.FullName -Force -ErrorAction SilentlyContinue
        Write-Host ("  已删除陈旧缓存: " + $p.Name)
    }
}

Write-Host ("  BUILD_TAG 已写入产物: " + $Tag)
Write-Host ("    旧: " + $old)
Write-Host ("    新: " + $new)
