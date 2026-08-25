# Compila la GUI (gui.py) a un ejecutable standalone de Windows con Nuitka.
#
# Uso:
#   1. Activar el entorno virtual:  .\venv\Scripts\Activate.ps1
#   2. Instalar dependencias:       pip install -r requirements.txt -r requirements-build.txt
#   3. Ejecutar:                    .\build.ps1
#
# El resultado queda en build\gui.dist\BuscarArchivos.exe junto con sus
# dependencias (modo --standalone: arranque rápido, sin auto-extracción en
# cada uso, a diferencia de --onefile).

$ErrorActionPreference = "Stop"

Remove-Item -Recurse -Force build, build_log.txt -ErrorAction SilentlyContinue

python -m nuitka `
    --standalone `
    --assume-yes-for-downloads `
    --windows-console-mode=disable `
    --enable-plugin=tk-inter `
    --include-package-data=customtkinter `
    --output-dir=build `
    --output-filename=BuscarArchivos.exe `
    --company-name="Herramienta interna" `
    --product-name="Buscador de Archivos" `
    --file-version=1.0.0.0 `
    --product-version=1.0.0.0 `
    gui.py

if ($LASTEXITCODE -eq 0) {
    Write-Host "`nListo: build\gui.dist\BuscarArchivos.exe" -ForegroundColor Green
} else {
    Write-Host "`nLa compilacion fallo (codigo $LASTEXITCODE). Revisa el detalle arriba." -ForegroundColor Red
}
