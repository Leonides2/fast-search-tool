# Compila la GUI (gui.py) a un ejecutable standalone de Windows con Nuitka.
#
# Uso:
#   1. Activar el entorno virtual:  .\venv\Scripts\Activate.ps1
#   2. Instalar dependencias:       pip install -r requirements.txt -r requirements-build.txt
#   3. Ejecutar:                    .\build.ps1
#
# Atajo interactivo: la lógica real de compilación (flags de Nuitka, ícono,
# versión, etc.) vive en build.py — la misma que usa el workflow de GitHub
# Actions para los tres sistemas operativos — así no hay dos comandos
# distintos que puedan desincronizarse entre sí.
#
# El resultado queda en dist\BuscarArchivos\BuscarArchivos.exe (modo
# --standalone: arranque rápido, sin auto-extracción en cada uso, a
# diferencia de --onefile).

$ErrorActionPreference = "Stop"

python build.py

if ($LASTEXITCODE -eq 0) {
    Write-Host "`nListo: dist\BuscarArchivos\BuscarArchivos.exe" -ForegroundColor Green
} else {
    Write-Host "`nLa compilacion fallo (codigo $LASTEXITCODE). Revisa el detalle arriba." -ForegroundColor Red
}
