$ErrorActionPreference = "Stop"

$Version = "0.6.0"
$PyInstallerVersion = if ($env:PYINSTALLER_VERSION) { $env:PYINSTALLER_VERSION } else { "6.15.0" }
$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$Dist = Join-Path $Root "dist"
$Build = Join-Path $Root "build"
$Package = Join-Path $Dist "PilferedParrot-$Version-windows-x64"
$Zip = Join-Path $Dist "PilferedParrot-$Version-windows-x64.zip"
$Built = Join-Path $Dist "PilferedParrot"
$Checksums = Join-Path $Dist "SHA256SUMS"

foreach ($Output in @($Built, $Package, $Zip, $Checksums)) {
    if (Test-Path $Output) { Remove-Item -Recurse -Force $Output }
}
New-Item -ItemType Directory -Force $Dist, $Build | Out-Null

python -m pip install --disable-pip-version-check --no-input "pyinstaller==$PyInstallerVersion"
if ($LASTEXITCODE -ne 0) {
    throw "Could not install PyInstaller $PyInstallerVersion"
}
$InstalledVersion = (python -m PyInstaller --version).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not determine the installed PyInstaller version"
}
if ($InstalledVersion -ne $PyInstallerVersion) {
    throw "Expected PyInstaller $PyInstallerVersion, found $InstalledVersion"
}

python -m PyInstaller --clean --noconfirm `
    --distpath $Dist --workpath $Build `
    (Join-Path $Root "packaging/windows/PilferedParrot.spec")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path (Join-Path $Built "PilferedParrot.exe"))) {
    throw "PyInstaller did not produce PilferedParrot.exe"
}
if (Test-Path $Package) { Remove-Item -Recurse -Force $Package }
Move-Item $Built $Package
$RequiredAssets = @(
    (Join-Path $Root "packaging/windows/README-WINDOWS.txt"),
    (Join-Path $Root "config.example.json"),
    (Join-Path $Root "LICENSE"),
    (Join-Path $Root "NOTICE")
)
foreach ($Asset in $RequiredAssets) {
    if (-not (Test-Path $Asset -PathType Leaf)) {
        throw "Required package asset is missing: $Asset"
    }
}
Copy-Item (Join-Path $Root "packaging/windows/README-WINDOWS.txt") (Join-Path $Package "README-WINDOWS.txt")
Copy-Item (Join-Path $Root "config.example.json") $Package
Copy-Item (Join-Path $Root "LICENSE") $Package
Copy-Item (Join-Path $Root "NOTICE") $Package
$PythonLicense = Join-Path (python -c "import sys; print(sys.base_prefix)") "LICENSE.txt"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $PythonLicense -PathType Leaf)) {
    throw "The bundled Python license is missing"
}
$PyInstallerLicense = python -c "from importlib.metadata import distribution; d = distribution('pyinstaller'); print(next(d.locate_file(f) for f in d.files if f.name == 'COPYING.txt'))"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $PyInstallerLicense -PathType Leaf)) {
    throw "The PyInstaller license is missing"
}
New-Item -ItemType Directory -Force (Join-Path $Package "licenses") | Out-Null
Copy-Item $PythonLicense (Join-Path $Package "licenses/Python-LICENSE.txt")
Copy-Item $PyInstallerLicense (Join-Path $Package "licenses/PyInstaller-COPYING.txt")
Compress-Archive -Path (Join-Path $Package "*") -DestinationPath $Zip
$Hash = (Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLowerInvariant()
"$Hash  $(Split-Path $Zip -Leaf)" | Set-Content -NoNewline $Checksums
Write-Host "Created $Zip"
