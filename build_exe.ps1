# PyInstaller ile tek dosyalık exe üretir: dist/policy-extract.exe
# Çalıştırma (tools/policy_extract içinden):
#   powershell -ExecutionPolicy Bypass -File ./build_exe.ps1
# Çıktı: dist/policy-extract.exe  (+ dist/policy-extract.version.txt)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path ".venv/Scripts/Activate.ps1")) {
  Write-Host ".venv yok, oluşturuluyor..."
  python -m venv .venv
}
. ./.venv/Scripts/Activate.ps1

python -m pip install --upgrade pip
pip install -e .
pip install pyinstaller

# Sürümü tek kaynaktan (service.py) alıp hem exe adına göm hem yanına yaz.
$version = python -c "from policy_extract.service import SERVICE_VERSION; print(SERVICE_VERSION)"
Write-Host "policy-extract sürümü: $version"

pyinstaller `
  --noconfirm `
  --clean `
  --onefile `
  --console `
  --name policy-extract `
  --paths src `
  --collect-all pypdf `
  --collect-all fonttools `
  --hidden-import policy_extract.extractor `
  --hidden-import policy_extract.service `
  --hidden-import policy_extract.updater `
  --exclude-module numpy `
  --exclude-module PIL `
  --exclude-module lxml `
  --exclude-module yaml `
  --exclude-module charset_normalizer `
  --exclude-module bs4 `
  --exclude-module soupsieve `
  --exclude-module psutil `
  --exclude-module olefile `
  --exclude-module defusedxml `
  --exclude-module pandas `
  --exclude-module scipy `
  --exclude-module matplotlib `
  --exclude-module requests `
  --exclude-module urllib3 `
  --exclude-module pypdfium2 `
  --exclude-module tkinter `
  --exclude-module unittest `
  --exclude-module test `
  src/policy_extract/cli.py

New-Item -ItemType Directory -Force -Path dist | Out-Null
"policy-extract $version" | Out-File -Encoding utf8 dist/policy-extract.version.txt
Write-Host "Bitti: dist/policy-extract.exe"
Write-Host "Kontrol: dist/policy-extract.exe --version  (beklenen: $version)"
