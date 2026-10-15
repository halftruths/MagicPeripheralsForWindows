# Builds the onedir exe, the portable zip and (if Inno Setup is installed) the installer into companion\packaging\dist.
#   powershell -ExecutionPolicy Bypass -File companion\packaging\build.ps1 [-SkipInstaller]
param([switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$version = (Select-String -Path (Join-Path $here '..\version.py') -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Magic Companion $version"

$venv = Join-Path $here '.venv'
if (-not (Test-Path "$venv\Scripts\python.exe")) { python -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install --quiet --upgrade pyinstaller

Push-Location $here
try {
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
    & "$venv\Scripts\python.exe" -m PyInstaller MagicCompanion.spec --noconfirm --distpath dist --workpath build
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

    $zip = "dist\MagicCompanion-$version-portable.zip"
    Compress-Archive -Path 'dist\MagicCompanion' -DestinationPath $zip -Force
    Write-Host "Portable zip: $zip"

    if (-not $SkipInstaller) {
        $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
        if (-not $iscc) {
            $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                      "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
        }
        if ($iscc) {
            & $iscc "/DAppVersion=$version" MagicCompanion.iss
            if ($LASTEXITCODE -ne 0) { throw 'ISCC failed' }
            Write-Host "Installer: dist\MagicCompanion-Setup-$version.exe"
        } else {
            Write-Warning 'Inno Setup 6 not found; skipped the installer (winget install JRSoftware.InnoSetup).'
        }
    }
} finally { Pop-Location }
