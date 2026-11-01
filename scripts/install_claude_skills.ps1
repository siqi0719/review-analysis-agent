# 把 skills/ 下的 3 个 Skill 链接到 .claude/skills/，让 Claude Code 能用 /issue-extract 等命令直接调用。
# 用法（在项目根目录）：powershell -ExecutionPolicy Bypass -File scripts\install_claude_skills.ps1
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root ".claude\skills"
New-Item -ItemType Directory -Force $target | Out-Null

$map = @{ "issue-extract" = "issue_extract"; "issue-topology" = "issue_topology"; "satisfaction-trend" = "satisfaction_trend" }
foreach ($name in $map.Keys) {
    $link = Join-Path $target $name
    if (Test-Path $link) { Write-Host "已存在：$name"; continue }
    # 目录联接（junction）不需要管理员权限
    cmd /c mklink /J "$link" "$(Join-Path $root "skills\$($map[$name])")" | Out-Null
    Write-Host "已链接：$name"
}
