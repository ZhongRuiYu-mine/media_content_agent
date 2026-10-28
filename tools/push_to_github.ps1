# push_to_github.ps1 — 推送仓库到 GitHub
#
# 我（AI）所在的环境没有外网、也没有你的 GitHub 凭据，所以最后这一步必须在你机器上跑。
# 本脚本把容易踩的地方都替你处理了：
#   ① 检查敏感文件（.env / 账号文件 / 审计日志）有没有误入库
#   ② 检查工作区是否还有未提交改动
#   ③ 按模式准备要推的内容（默认只推交付物，legacy/ 档案不进公开仓库）
#   ④ 推送失败时**不会丢东西**：准备好的仓库留在临时目录，可以进去手动再推
#
# 三种模式
#   -Mode deliver  (默认) 只推交付物：把 deliver/ 的内容做成一个干净仓库再推。
#                          仓库根 = content/ components/ static/ test/ 文档…
#                          不依赖 git subtree / sh，最稳。
#   -Mode history  只推交付物、但保留这些文件的提交历史（git subtree split，
#                          需要能起 sh；受限沙箱里会失败，普通终端可以）
#   -Mode all      整仓推送：含 legacy/ 档案与全部提交
#
# 用法（在任意位置都行）
#   .\deliver\tools\push_to_github.ps1                 # 推交付物（推荐）
#   .\deliver\tools\push_to_github.ps1 -Mode all       # 连 legacy 一起推
#   .\deliver\tools\push_to_github.ps1 -DryRun         # 只演练，不真推
#
# 登录二选一
#   $env:GH_TOKEN = "ghp_xxx"    # 推荐：要有 repo 权限；只用于本次，不写盘
#   不设 → 走 Git 凭据管理器弹窗登录
#
# 前置：先在 GitHub 网页建好**空仓库**（别勾 Add a README）。
#       万一勾了，首次推会冲突 → 加 -Force 重跑（只覆盖那一个初始提交）。

param(
    [ValidateSet("deliver", "history", "all")]
    [string]$Mode = "deliver",

    [string]$RepoUrl = "https://github.com/ZhongRuiYu-mine/media_content_agent.git",

    [string]$Branch = "main",

    [switch]$DryRun,

    [switch]$Force
)

$ErrorActionPreference = "Continue"

# ---- 定位仓库根（本脚本在 <repo>/deliver/tools/ 下）----
$Root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
if (-not $Root -or -not (Test-Path (Join-Path $Root "deliver"))) {
    $Root = (git rev-parse --show-toplevel 2>$null)
}
if (-not $Root -or -not (Test-Path (Join-Path $Root "deliver"))) {
    Write-Host "找不到仓库根（应含 deliver/ 子目录）" -ForegroundColor Red
    exit 1
}

function Step($n, $msg) { Write-Host ""; Write-Host "[$n] $msg" -ForegroundColor Cyan }
function Ok($m) { Write-Host ("   " + $m) -ForegroundColor Green }
function Bad($m) { Write-Host ("   " + $m) -ForegroundColor Red }

Step 1 "环境"
Write-Host ("   仓库根 : " + $Root)
Write-Host ("   git    : " + ((git --version) -join ""))
$token = [Environment]::GetEnvironmentVariable("GH_TOKEN")
if ($token) { Ok "GH_TOKEN 已设置（只用于本次推送，不落盘）" }
else { Write-Host "   GH_TOKEN 未设置（走 Git 凭据管理器登录）" }

Step 2 "敏感文件检查"
$tracked = @(git -C $Root ls-files)
$bad = @($tracked | Where-Object {
        $_ -match "(^|/)(users\.json|user_configs\.json|llm_gate\.jsonl)$" -or
        $_ -match "(^|/)\.env$" -or
        $_ -match "旧运行时数据"
    })
if ($bad.Count -gt 0) {
    Bad "发现不该入库的文件，先删掉再推："
    $bad | ForEach-Object { Bad ("  " + $_) }
    exit 1
}
Ok ("无 .env / 无账号文件 / 无审计日志（已跟踪 " + $tracked.Count + " 个文件）")

Step 3 "提交状态"
$dirty = @(git -C $Root status --porcelain)
if ($dirty.Count -gt 0) {
    Bad "工作区有未提交改动，先 commit 再推："
    $dirty | Select-Object -First 10 | ForEach-Object { Bad ("  " + $_) }
    exit 1
}
Ok ("干净；当前分支 " + (git -C $Root branch --show-current) +
    "，共 " + ((git -C $Root rev-list --count HEAD) -join "") + " 个提交")

$PushDir = $Root
$Refspec = "HEAD:" + $Branch
$Built = $false

if ($Mode -eq "all") {
    Step 4 "全量推送（含 legacy/ 档案与全部提交历史）"
}
elseif ($Mode -eq "history") {
    Step 4 "交付物 + 保留历史（git subtree split）"
    $split = @(git -C $Root subtree split --prefix=deliver -b deliver-only 2>&1)
    $sha = ($split | Where-Object { $_ -match "^[0-9a-f]{7,40}$" } | Select-Object -Last 1)
    if (-not $sha) {
        Bad "subtree split 失败（受限环境里 sh 起不来）："
        $split | Select-Object -Last 3 | ForEach-Object { Bad ("  " + $_) }
        Bad "改用默认模式重跑：.\deliver\tools\push_to_github.ps1"
        exit 1
    }
    Ok ("deliver-only = " + $sha)
    $Refspec = "deliver-only:" + $Branch
}
else {
    Step 4 "准备交付物仓库（把 deliver/ 做成干净仓库根）"
    $PushDir = Join-Path $env:TEMP ("push_" + (Get-Date -Format "MMddHHmmss"))
    if (Test-Path $PushDir) { Remove-Item $PushDir -Recurse -Force }
    New-Item -ItemType Directory -Path $PushDir -Force | Out-Null
    $srcDir = Join-Path $Root "deliver"
    # 用 PowerShell 自带拷贝（不 spawn robocopy：受限环境下起子进程可能被拒）
    $skip = @("data", "logs", "_shots_tmp", "__pycache__", ".git")
    Get-ChildItem -Path $srcDir -Force | Where-Object { $skip -notcontains $_.Name } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $PushDir -Recurse -Force
    }
    # 带上根 .gitignore，交付仓库以后也不会误提交 data/ 与缓存
    $gi = Join-Path $Root ".gitignore"
    if (Test-Path $gi) { Copy-Item -LiteralPath $gi -Destination (Join-Path $PushDir ".gitignore") -Force }
    $null = git -C $PushDir init -b $Branch 2>&1
    $null = git -C $PushDir add -A 2>&1
    $null = git -C $PushDir -c user.name="media-content-agent" -c user.email="dev@local" commit -m "新媒体信宣智能体（采编台）· 交付版" 2>&1
    $n = (git -C $PushDir ls-files | Measure-Object).Count
    Ok ("已建干净仓库：" + $PushDir)
    Ok ("包含 " + $n + " 个文件（不含 data/ logs/ 缓存）")
    $Built = $true
    $Refspec = "HEAD:" + $Branch
}

Step 5 $(if ($DryRun) { "试运行（不真推）" } else { "推送到 " + $RepoUrl })
$target = $RepoUrl
if ($token) { $target = $RepoUrl.Replace("https://", "https://x-access-token:" + $token + "@") }
$gitArgs = @("push", $target, $Refspec)
if ($DryRun) { $gitArgs += "--dry-run" }
if ($Force) { $gitArgs += "--force" }
Write-Host ("   git push remote " + $Refspec + $(if ($DryRun) { " --dry-run" } else { "" }))
& git -C $PushDir @gitArgs
$code = $LASTEXITCODE

Step 6 "结果"
if ($code -eq 0) {
    Ok ("推送成功 → " + $RepoUrl + "/tree/" + $Branch)
    if ($Built) {
        Write-Host "   仓库根就是交付物；本地仓库（含 legacy/ 与全部历史）原样保留。"
        Write-Host ("   构建产物同时留在：" + $PushDir + "（以后重推可以直接用）")
    }
}
else {
    Bad ("推送失败（exit " + $code + "）。常见原因：")
    Write-Host "     · 仓库不存在或名字不对 → 先在 GitHub 建一个空仓库"
    Write-Host "     · 未登录 / 令牌没有 repo 权限 → 设 `$env:GH_TOKEN，或用凭据管理器登录"
    Write-Host "     · 远端已有 README 初始提交 → 加 -Force 重跑（只覆盖那一个初始提交）"
    Write-Host "     · 网络不通 → 换网络或代理后重试"
    if ($Built) {
        Write-Host ("   注意：准备好的仓库没丢，在 " + $PushDir)
        Write-Host ("   可以进去手动推：git push " + $RepoUrl + " HEAD:" + $Branch)
    }
}
exit $code
