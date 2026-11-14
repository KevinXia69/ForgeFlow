# Runs on Windows PowerShell 5.1 or PowerShell 7, with an isolated Docker function.
# No Docker daemon, API key, browser, provider request or service is used.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('forgeflow-launcher-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
Copy-Item (Join-Path $repo 'installer/Start-ForgeFlow.ps1') $temporary
Copy-Item (Join-Path $repo 'installer/Stop-ForgeFlow.ps1') $temporary
Set-Content (Join-Path $temporary 'compose.yaml') 'services: {}'
Set-Content (Join-Path $temporary 'compose.source.yaml') 'services: {}'
New-Item -ItemType Directory -Path (Join-Path $temporary 'source') | Out-Null
$previousContext = $env:DOCKER_CONTEXT
$previousHost = $env:DOCKER_HOST
$previousLocation = Get-Location
$env:DOCKER_CONTEXT = $null
$env:DOCKER_HOST = $null
$global:FFCalls = [Collections.Generic.List[string]]::new()
$global:FFConfigured = $false
$global:FFRunning = $false
$global:FFPullFailure = $false
$global:FFEndpoint = 'npipe:////./pipe/docker_engine'
function global:docker {
    $call = @($args)
    $global:FFCalls.Add(($call -join '|'))
    $global:LASTEXITCODE = 0
    if ($call[0] -eq 'context') { return $global:FFEndpoint }
    if ($call[0] -eq 'info') { return 'linux' }
    if ($call[0] -ne 'compose') { $global:LASTEXITCODE = 2; return }
    if ($call[1] -eq 'version') { return 'Docker Compose version v2.39.0' }
    $command = @($call | Select-Object -Skip 5)
    if ($command[0] -eq '-f') { $command = @($command | Select-Object -Skip 2) }
    if ($command[0] -eq 'ps') { if ($global:FFRunning) { return @('api', 'supervisor') }; return }
    if ($command[0] -eq 'pull' -and $global:FFPullFailure) { $global:LASTEXITCODE = 1; return }
    if ($command -contains 'forgeflow.local_workbench.setup') {
        if ($command -contains 'url') { return 'http://127.0.0.1:17801/#synthetic-only-capability' }
        if ($command -contains '--saved') { return }
        if ($command -contains 'status' -and -not $global:FFConfigured) { $global:LASTEXITCODE = 1; return }
    }
}
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
try {
    & (Join-Path $temporary 'Start-ForgeFlow.ps1') -NoOpen
    Assert-True (($global:FFCalls | Where-Object { $_ -match 'status\|--saved' }).Count -gt 0) 'First setup must wait for save.'
    Assert-True (($global:FFCalls | Where-Object { $_ -match 'resume-admission$' }).Count -gt 0) 'Healthy startup must resume admission.'
    $global:FFCalls.Clear()
    $global:FFConfigured = $true
    $global:FFRunning = $true
    & (Join-Path $temporary 'Start-ForgeFlow.ps1') -Configure -NoOpen
    $ordered = $global:FFCalls -join "`n"
    Assert-True ($ordered.IndexOf('stop|-t|-1|api') -lt $ordered.IndexOf('quiesce')) 'API must stop before admission drains.'
    Assert-True ($ordered.IndexOf('quiesce') -lt $ordered.IndexOf('doctor|--require-idle')) 'Admission must quiesce before idle verification.'
    Assert-True ($ordered.IndexOf('doctor|--require-idle') -lt $ordered.IndexOf('stop|-t|-1|supervisor|docmind')) 'Workers must drain before stop.'
    $global:FFCalls.Clear()
    & (Join-Path $temporary 'Stop-ForgeFlow.ps1')
    Assert-True ($global:FFCalls[$global:FFCalls.Count - 1] -match 'stop\|-t\|-1$') 'Stop must preserve data and never use force kill.'
    Assert-True (-not ($global:FFCalls | Where-Object { $_ -match '\|(down|prune|--volumes)\|' })) 'Stop may not delete volumes.'
    $global:FFCalls.Clear()
    $global:FFRunning = $false
    $global:FFPullFailure = $true
    & (Join-Path $temporary 'Start-ForgeFlow.ps1') -NoOpen
    $buildIndex = -1
    for ($i = 0; $i -lt $global:FFCalls.Count; $i++) { if ($global:FFCalls[$i] -match '\|build\|') { $buildIndex = $i; break } }
    Assert-True ($buildIndex -ge 0) 'Missing bundled source fallback.'
    foreach ($call in $global:FFCalls.GetRange($buildIndex, $global:FFCalls.Count - $buildIndex)) {
        Assert-True ($call -match 'compose.source.yaml') 'Every command after fallback must keep the source overlay.'
    }
    $global:FFCalls.Clear()
    $env:DOCKER_CONTEXT = 'remote-selected'
    $env:DOCKER_HOST = 'npipe:////./pipe/docker_engine'
    $global:FFEndpoint = 'ssh://untrusted-remote'
    $rejected = $false
    try { & (Join-Path $temporary 'Start-ForgeFlow.ps1') -NoOpen } catch { $rejected = $true }
    Assert-True $rejected 'DOCKER_CONTEXT must take precedence; remote context must be rejected.'
    Assert-True (-not ($global:FFCalls | Where-Object { $_ -match '\|(up|pull|stop)\|' })) 'No mutation is allowed on remote Docker.'
    Write-Output '{"ok":true,"checks":5,"scope":"PowerShell launchers with isolated Docker function; no Docker Desktop or provider calls"}'
} finally {
    Set-Location $previousLocation
    $env:DOCKER_CONTEXT = $previousContext
    $env:DOCKER_HOST = $previousHost
    Remove-Item Function:\docker -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporary -Recurse -Force
}
