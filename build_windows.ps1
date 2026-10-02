$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project

$Python = Get-Command py -ErrorAction SilentlyContinue
if (-not $Python) { throw "Python 3.11+ is needed only on the build machine." }
$Iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $Iscc) { throw "Install Inno Setup 6 on the build machine before packaging." }

$Repo = $env:GITHUB_REPOSITORY
if (-not $Repo) {
    Write-Warning "GITHUB_REPOSITORY is empty; the installed app will not know where to check for updates."
}
$AppVersion = $env:APP_VERSION
if (-not $AppVersion) { $AppVersion = "0.1.0" }
@{ github_repo = $Repo; app_version = $AppVersion } | ConvertTo-Json | Set-Content -Encoding UTF8 release_config.json

py -3.11 -m venv .build-venv
$VenvPython = Join-Path $Project ".build-venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Could not install build dependencies." }

& $VenvPython -m PyInstaller --noconfirm --clean --onedir --windowed --name TempoFijo `
    --collect-all librosa --collect-all miniaudio --collect-all lameenc `
    --add-data "release_config.json;." tempo_fijo.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller could not package Tempo Fijo." }

& $Iscc.Source "/DMyAppVersion=$AppVersion" TempoFijo.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup could not create the installer." }
$Installer = Join-Path $Project "outputs\TempoFijoSetup.exe"
if (-not (Test-Path -LiteralPath $Installer)) { throw "Installer output was not created." }
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLowerInvariant()
"$Hash  TempoFijoSetup.exe" | Set-Content -Encoding ASCII "$Installer.sha256"
Write-Host "Created $Installer"
