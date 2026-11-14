param([switch]$Configure, [switch]$NoOpen)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
function Invoke-Docker([string[]]$Arguments) {
    $output = & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Docker command failed. No local data was deleted.' }
    return $output
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Install Docker Desktop with Linux containers: https://docs.docker.com/desktop/setup/install/windows-install/'
}
& docker compose version *> $null
$script:ComposePlugin = ($LASTEXITCODE -eq 0)
if (-not $script:ComposePlugin -and -not (Get-Command docker-compose -ErrorAction SilentlyContinue)) {
    throw 'Docker Compose v2 is required: https://docs.docker.com/compose/install/'
}
$endpoint = $env:DOCKER_HOST
if ($env:DOCKER_CONTEXT) { $endpoint = & docker context inspect $env:DOCKER_CONTEXT --format '{{.Endpoints.docker.Host}}'; if ($LASTEXITCODE -ne 0) { throw 'Docker context unavailable.' } }
if (-not $endpoint) { $endpoint = Invoke-Docker @('context','inspect','--format','{{.Endpoints.docker.Host}}') }
if ($endpoint -notmatch '^(unix|npipe)://') { throw 'Select a local Docker context first. Remote endpoints are not supported.' }
$osType = Invoke-Docker @('info','--format','{{.OSType}}')
if ($osType -ne 'linux') { throw 'Start Docker Desktop and select Linux containers.' }
$script:ComposeExtra = @()
function Invoke-Compose([string[]]$Arguments, [switch]$AllowFailure) {
    $common = @('-p','forgeflow-local','-f','compose.yaml') + $script:ComposeExtra
    if ($script:ComposePlugin) { $output = & docker compose @common @Arguments }
    else { $output = & docker-compose @common @Arguments }
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) { throw 'Compose command failed. Configuration and local data are preserved.' }
    return $output
}
function Open-LocalUrl([string]$Url) {
    if ($NoOpen) { Write-Host $Url } else { Start-Process $Url }
}
$running = Invoke-Compose @('ps','--status','running','--services')
if (-not $Configure -and $running -contains 'api') {
    Invoke-Compose @('exec','-T','api','python','-m','forgeflow.local_workbench.runtime','doctor') | Out-Host
    Write-Host 'ForgeFlow is already running. To install a new version, safely stop it first.'
    Open-LocalUrl 'http://127.0.0.1:17800/app/'
    exit 0
}
if ($Configure) {
    $running = Invoke-Compose @('ps','--status','running','--services')
    if ($running -contains 'api') { Invoke-Compose @('stop','-t','-1','api') | Out-Host }
    if ($running -contains 'supervisor') {
        Invoke-Compose @('exec','-T','supervisor','python','-m','forgeflow.local_workbench.runtime','quiesce') | Out-Host
        $idle = $false
        for ($attempt = 0; $attempt -lt 150; $attempt++) {
            Invoke-Compose -Arguments @('exec','-T','supervisor','python','-m','forgeflow.local_workbench.runtime','doctor','--require-idle') -AllowFailure | Out-Host
            if ($LASTEXITCODE -eq 0) { $idle = $true; break }
            Start-Sleep -Seconds 2
        }
        if (-not $idle) { throw 'Background work is active. Admission remains paused; no worker was killed. Retry after it finishes.' }
    }
    Invoke-Compose @('stop','-t','-1','supervisor','docmind') | Out-Host
}
Invoke-Compose -Arguments @('pull','setup','odoo-init') -AllowFailure | Out-Host
if ($LASTEXITCODE -ne 0) {
    if (-not (Test-Path 'compose.source.yaml') -or -not (Test-Path 'source' -PathType Container)) { throw 'Image download failed. Download the complete release bundle to use its source fallback.' }
    Write-Host 'Prebuilt images are unavailable. Building the bundled source locally; base images still require internet access.'
    $script:ComposeExtra = @('-f','compose.source.yaml')
    Invoke-Compose @('build','setup','odoo-init') | Out-Host
}
Invoke-Compose @('up','-d','--force-recreate','setup') | Out-Host
$setupUrl = $null
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $candidate = Invoke-Compose -Arguments @('exec','-T','setup','python','-m','forgeflow.local_workbench.setup','url') -AllowFailure
    if ($LASTEXITCODE -eq 0) { $setupUrl = [string]$candidate; break }
    Start-Sleep -Seconds 2
}
if (-not $setupUrl) { throw 'Local setup did not become ready. Check Docker service status.' }
Invoke-Compose -Arguments @('exec','-T','setup','python','-m','forgeflow.local_workbench.setup','status') -AllowFailure | Out-Null
if ($Configure -or $LASTEXITCODE -ne 0) {
    Write-Host 'Complete the local setup page. The private one-time link expires after 30 minutes.'
    Open-LocalUrl $setupUrl
    $saved = $false
    for ($attempt = 0; $attempt -lt 900; $attempt++) {
        Invoke-Compose -Arguments @('exec','-T','setup','python','-m','forgeflow.local_workbench.setup','status','--saved') -AllowFailure | Out-Null
        if ($LASTEXITCODE -eq 0) { $saved = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $saved) { Invoke-Compose @('stop','setup') | Out-Null; throw 'Setup was not completed. Run the launcher again. Existing data is preserved.' }
}
$setupUrl = $null
Write-Host 'Starting ForgeFlow. Initial download and initialization can take several minutes.'
if ($Configure) { Invoke-Compose @('up','-d','--wait','--wait-timeout','900','--force-recreate','api','supervisor','docmind') | Out-Host }
else { Invoke-Compose @('up','-d','--wait','--wait-timeout','900') | Out-Host }
Invoke-Compose @('exec','-T','supervisor','python','-m','forgeflow.local_workbench.runtime','resume-admission') | Out-Host
Invoke-Compose @('stop','setup') | Out-Host
Write-Host 'ForgeFlow is ready. Use the invite code shown by setup. Stop scripts preserve your data.'
Open-LocalUrl 'http://127.0.0.1:17800/app/'
