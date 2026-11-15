$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$logRoot = Join-Path $appRoot 'artifacts\web_rag_v1'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$env:PYTHONPATH = Join-Path $appRoot 'src'
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
if (-not $env:DEEPSEEK_API_KEY) {
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
}
$pythonPath = Join-Path $appRoot '.venv\Scripts\python.exe'
$port = & $pythonPath -c "from tax_rag.config import load_toml; print(load_toml('runtime.toml')['web']['port'])"
if ($LASTEXITCODE -ne 0) { throw '无法读取网页配置。' }
$url = 'http://127.0.0.1:{0}' -f $port.Trim()
$existing = Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    try {
        $health = Invoke-RestMethod -Uri ($url + '/health') -TimeoutSec 5
        if ($health.ok -and $health.pipeline -eq 'vat-web-rag-v1') {
            Write-Output ('现有增值税网页可复用：' + $url)
            exit 0
        }
    } catch { }
    throw ('端口已被进程占用，请先核对所属项目。PID=' + ($existing.OwningProcess -join ','))
}
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-B', '-u', '-m', 'tax_rag.cli.run_web') -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'server.stdout.log') -RedirectStandardError (Join-Path $logRoot 'server.stderr.log') -PassThru
$deadline = (Get-Date).AddSeconds(25)
do {
    Start-Sleep -Milliseconds 500
    $listener = Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
} until ($listener -or (Get-Date) -ge $deadline)
if (-not $listener) { throw '服务未就绪，请检查artifacts/web_rag_v1/server.stderr.log。' }
$running = Get-CimInstance Win32_Process -Filter ('ProcessId = {0}' -f $listener.OwningProcess)
if ($running.CommandLine -notmatch 'tax_rag\.cli\.run_web') { throw '新监听进程与增值税入口不匹配，请核对。' }
$record = [ordered]@{
    Url = $url
    ProjectRoot = $appRoot
    LauncherPid = $process.Id
    ListenerPid = $running.ProcessId
    ListenerCreatedUtc = $running.CreationDate.ToUniversalTime().ToString('o')
    ListenerCreatedTicks = $running.CreationDate.ToUniversalTime().Ticks
    Entry = 'tax_rag.cli.run_web'
}
$record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logRoot 'server_process.json') -Encoding utf8
$health = Invoke-RestMethod -Uri ($url + '/health') -TimeoutSec 10
if (-not $health.ok) { throw '网页已监听，活动知识库尚未就绪。' }
$record | ConvertTo-Json
