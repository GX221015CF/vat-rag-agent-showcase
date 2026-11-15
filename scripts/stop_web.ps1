$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$recordPath = Join-Path $appRoot 'artifacts\web_rag_v1\server_process.json'
$record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
if ($record.ProjectRoot -ne $appRoot) { throw '启动记录项目目录不匹配。' }
$running = Get-CimInstance Win32_Process -Filter ('ProcessId = {0}' -f $record.ListenerPid)
if (-not $running) { Write-Output '增值税网页服务已停止。'; exit 0 }
if ($running.CreationDate.ToUniversalTime().Ticks -ne [long]$record.ListenerCreatedTicks -or $running.CommandLine -notmatch 'tax_rag\.cli\.run_web') {
    throw '进程身份与启动记录不一致，请重新核对。'
}
Stop-Process -Id $running.ProcessId
Write-Output '增值税网页服务已停止。'
