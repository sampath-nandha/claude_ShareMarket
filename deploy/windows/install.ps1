# MoneyMakingMachine - Windows 11 native install (no containers).
# Run in an elevated PowerShell from the repo root:  .\deploy\windows\install.ps1 -DataDir D:\ShareMarketData
param(
    [string]$DataDir = "C:\ShareMarketData",
    [string]$WinSWUrl = "https://github.com/winsw/winsw/releases/latest/download/WinSW-x64.exe",
    [switch]$SkipServices
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
Write-Host "Repo: $Repo"

# 1. Python env via uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Push-Location $Repo
uv venv --python 3.12 .venv
uv pip install -e ".[dev]" --python .venv\Scripts\python.exe
Pop-Location

# 2. Data directory + .env (secrets go to Credential Manager via `mmm auth set-keys`)
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$envFile = Join-Path $Repo ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $Repo ".env.example") $envFile
}
(Get-Content $envFile) -replace '^MMM_DATA_DIR=.*', "MMM_DATA_DIR=$DataDir" | Set-Content $envFile
Write-Host "Data dir: $DataDir"

# 3. Defender exclusion for the data folder (large Parquet/SQLite writes)
try { Add-MpPreference -ExclusionPath $DataDir } catch { Write-Warning "Could not add Defender exclusion: $_" }

# 4. Power & update hygiene (never sleep; active hours cover the session)
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
try {
    New-Item -Path "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" -Force | Out-Null
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" -Name ActiveHoursStart -Value 8
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" -Name ActiveHoursEnd -Value 18
} catch { Write-Warning "Could not set active hours: $_" }

# 5. Time sync every 15 minutes (accurate tick timestamps)
try {
    w32tm /config /manualpeerlist:"time.windows.com,0x9 in.pool.ntp.org,0x9" /syncfromflags:manual /update | Out-Null
    Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient" -Name SpecialPollInterval -Value 900
    Restart-Service w32time
} catch { Write-Warning "Time sync config failed: $_" }

if ($SkipServices) { Write-Host "Skipping services."; exit 0 }

# 6. WinSW services: collector (market hours) and scheduler (always on)
$svcDir = Join-Path $Repo "deploy\windows"
$winsw = Join-Path $svcDir "WinSW.exe"
if (-not (Test-Path $winsw)) { Invoke-WebRequest $WinSWUrl -OutFile $winsw }
foreach ($name in @("ShareMarket-Scheduler", "ShareMarket-Collector")) {
    $exe = Join-Path $svcDir "$name.exe"
    Copy-Item $winsw $exe -Force
    $xml = Get-Content (Join-Path $svcDir "$name.xml") -Raw
    $xml = $xml.Replace("__REPO__", $Repo).Replace("__DATADIR__", $DataDir)
    Set-Content (Join-Path $svcDir "$name.xml") $xml
    & $exe install | Out-Null
    & $exe start | Out-Null
    Write-Host "Service $name installed and started."
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. .\.venv\Scripts\mmm auth set-keys      (API key/secret -> Credential Manager)"
Write-Host "  2. .\.venv\Scripts\mmm auth login         (every trading morning, before 09:00)"
Write-Host "  3. .\.venv\Scripts\mmm data instruments && .\.venv\Scripts\mmm data backfill"
Write-Host "  4. .\.venv\Scripts\mmm dashboard          (http://localhost:8501)"
