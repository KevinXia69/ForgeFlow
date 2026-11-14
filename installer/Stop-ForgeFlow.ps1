$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker is not installed.' }
$endpoint = $env:DOCKER_HOST
if ($env:DOCKER_CONTEXT) { $endpoint = & docker context inspect $env:DOCKER_CONTEXT --format '{{.Endpoints.docker.Host}}'; if ($LASTEXITCODE -ne 0) { throw 'Docker context unavailable.' } }
if (-not $endpoint) { $endpoint = & docker context inspect --format '{{.Endpoints.docker.Host}}'; if ($LASTEXITCODE -ne 0) { throw 'Docker context unavailable.' } }
if ($endpoint -notmatch '^(unix|npipe)://') { throw 'Refusing to stop containers on a remote Docker endpoint.' }
& docker compose version *> $null
$script:ComposePlugin = ($LASTEXITCODE -eq 0)
if (-not $script:ComposePlugin -and -not (Get-Command docker-compose -ErrorAction SilentlyContinue)) { throw 'Docker Compose v2 is required.' }
function Invoke-Compose([string[]]$Arguments, [switch]$AllowFailure) {
    $common = @('-p','forgeflow-local','-f','compose.yaml')
    if ($script:ComposePlugin) { $output = & docker compose @common @Arguments }
    else { $output = & docker-compose @common @Arguments }
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) { throw 'Safe stop failed. No data was deleted.' }
    return $output
}
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
    if (-not $idle) { throw 'Background work remains active. Admission is paused; workers were left running to finish. Retry later.' }
}
Invoke-Compose @('stop','-t','-1') | Out-Host
Write-Host 'ForgeFlow stopped. Configuration, databases, documents and local volumes are preserved.'
