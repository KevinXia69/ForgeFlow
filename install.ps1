# ForgeFlow one-command installer for Windows.
$ErrorActionPreference = 'Stop'
$Version = if ($env:FORGEFLOW_VERSION) { $env:FORGEFLOW_VERSION } else { '3.11.0' }
$Repo = 'KevinXia69/ForgeFlow'
$Base = "https://github.com/$Repo/releases/download/v$Version"
$Asset = "ForgeFlow-v$Version-Windows.zip"
$Dest = if ($env:FORGEFLOW_HOME) { $env:FORGEFLOW_HOME } else { Join-Path $HOME 'ForgeFlow' }

if (Test-Path $Dest) {
  throw "Destination already exists: $Dest. Set FORGEFLOW_HOME to another directory."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw 'Docker is required. Install Docker Desktop and select Linux containers.'
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("forgeflow-install-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  Write-Host "Downloading $Asset..."
  Invoke-WebRequest -UseBasicParsing -Uri "$Base/$Asset" -OutFile (Join-Path $tmp $Asset)
  Invoke-WebRequest -UseBasicParsing -Uri "$Base/SHA256SUMS" -OutFile (Join-Path $tmp 'SHA256SUMS')
  $line = Get-Content (Join-Path $tmp 'SHA256SUMS') | Where-Object { $_ -match "\s$([regex]::Escape($Asset))$" } | Select-Object -First 1
  if (-not $line) { throw "No checksum found for $Asset." }
  $expected = ($line -split '\s+')[0].ToLowerInvariant()
  $actual = (Get-FileHash -Algorithm SHA256 -Path (Join-Path $tmp $Asset)).Hash.ToLowerInvariant()
  if ($expected -ne $actual) { throw 'Checksum verification failed; refusing to install.' }

  New-Item -ItemType Directory -Path $Dest | Out-Null
  Expand-Archive -Path (Join-Path $tmp $Asset) -DestinationPath (Join-Path $tmp 'unpacked')
  $root = Get-ChildItem (Join-Path $tmp 'unpacked') -Directory | Select-Object -First 1
  if (-not $root) { throw 'Unexpected installer archive layout.' }
  Copy-Item -Path (Join-Path $root.FullName '*') -Destination $Dest -Recurse -Force
  Write-Host "ForgeFlow $Version installed at: $Dest"
  Write-Host 'Starting the one-time local setup...'
  Start-Process -FilePath (Join-Path $Dest 'Start-ForgeFlow.cmd') -WorkingDirectory $Dest
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
