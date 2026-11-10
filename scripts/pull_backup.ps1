# 把服务器上最新的数据库备份拉到本机。
#
# 为什么需要这一步：服务器上那份备份和库在同一块磁盘上，
# 磁盘没了两个一起没。真正的备份必须落在另一台机器上。
#
# 用法（在仓库根目录）：
#     powershell -ExecutionPolicy Bypass -File scripts\pull_backup.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\pull_backup.ps1 -Keep 7
#
# 依赖：已经配好 ssh 别名 gb（见 docs/deploy.md 第九节）。

param(
    [string]$RemoteDir = "/opt/group-brain/data/backups",
    [string]$DestDir   = "$env:USERPROFILE\Desktop\group-brain-backups",
    [int]$Keep         = 7
)

# 注意：PowerShell 要求 param() 必须是脚本里第一个可执行语句，
# 所以编码这两行只能放在它后面。
# PS 5.1 的控制台默认不是 UTF-8，中文和 python 的输出都会变成乱码。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"

$ErrorActionPreference = "Stop"

if (-not (Test-Path $DestDir)) { New-Item -ItemType Directory -Path $DestDir -Force | Out-Null }

# 找服务器上最新的一份
$latest = (ssh gb "ls -1t $RemoteDir/group_brain-*.db 2>/dev/null | head -1").Trim()
if (-not $latest) {
    Write-Host "[X] 服务器上还没有备份。先手动跑一次：" -ForegroundColor Red
    Write-Host "    ssh gb 'cd /opt/group-brain && .venv/bin/python scripts/backup_db.py'"
    exit 1
}

$name = Split-Path $latest -Leaf
$dest = Join-Path $DestDir $name
Write-Host "拉到: $name"

scp -q "gb:$latest" $dest

# 拉下来立刻验一遍：文件能打开、三张表行数不为 0
$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if (Test-Path $py) {
    & $py -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print('[ok] 备份可读:', {t: c.execute('select count(*) from '+t).fetchone()[0] for t in ('messages','memes','vectors')})" $dest
}

$size = "{0:N2} MB" -f ((Get-Item $dest).Length / 1MB)
Write-Host "[ok] $dest  ($size)" -ForegroundColor Green

# 只留最近 Keep 份
Get-ChildItem $DestDir -Filter "group_brain-*.db" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep |
    ForEach-Object { Remove-Item $_.FullName; Write-Host "[ok] 删本地旧备份: $($_.Name)" }

Write-Host ("[ok] 本机共 " + (Get-ChildItem $DestDir -Filter 'group_brain-*.db').Count + " 份备份")
