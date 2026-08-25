#!/usr/bin/env python3
"""
Compila gui.py a un ejecutable standalone con Nuitka para el sistema
operativo donde se ejecuta este script.

Lo usa tanto un desarrollador en local (Windows, Linux o macOS) como el
workflow de GitHub Actions (.github/workflows/release.yml), que lo corre
en los tres sistemas para generar los builds de cada release. Es la única
fuente de verdad de "cómo se compila" — build.ps1 sigue existiendo como
atajo interactivo para Windows, pero la lógica real vive aquí.

Nota sobre Linux y macOS: son builds "best effort". Windows es la
plataforma que de verdad se usa y la que se probó a fondo (arranque medido,
clicks reales, persistencia de configuración, etc.). En macOS, al no estar
firmado con un certificado de Apple, Gatekeeper bloqueará la app por
defecto — hay que ejecutarla desde Terminal (./BuscarArchivos) o
autorizarla en Preferencias del Sistema > Privacidad y Seguridad.

Uso:
    python build.py
    BUILD_VERSION=1.2.3 python build.py   # fija la versión incrustada en el .exe (Windows)

El resultado queda siempre en ./dist/BuscarArchivos/, sin importar el SO,
para que quien empaquete el resultado (a mano o en CI) no tenga que conocer
los detalles de nombres internos de Nuitka (gui.dist, etc.).
"""
import os
import platform
import shutil
import subprocess
import sys

APP_NAME = "BuscarArchivos"
BUILD_DIR = "build"
DIST_DIR = "dist"


def _win_version(v: str) -> str:
    """Nuitka espera exactamente 4 componentes numéricos (X.X.X.X) para
    --file-version/--product-version en Windows."""
    parts = (v.split(".") + ["0", "0", "0", "0"])[:4]
    parts = [p if p.isdigit() else "0" for p in parts]
    return ".".join(parts)


def main() -> None:
    system = platform.system()  # "Windows", "Linux", "Darwin"
    version = os.environ.get("BUILD_VERSION", "0.0.0")

    for d in (BUILD_DIR, DIST_DIR):
        if os.path.isdir(d):
            shutil.rmtree(d)

    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",  # nunca preguntar de forma interactiva: en CI eso cuelga el job para siempre
        "--enable-plugin=tk-inter",
        "--include-package-data=customtkinter",
        f"--output-dir={BUILD_DIR}",
        "--company-name=Herramienta interna",
        "--product-name=Buscador de Archivos",
    ]

    if system == "Windows":
        cmd += [
            "--windows-console-mode=disable",
            f"--output-filename={APP_NAME}.exe",
            f"--file-version={_win_version(version)}",
            f"--product-version={_win_version(version)}",
        ]
    else:
        # Linux/macOS: binario standalone simple (sin bundle .app en macOS)
        # para mantener un único camino de empaquetado en los tres SO.
        cmd += [f"--output-filename={APP_NAME}"]

    cmd.append("gui.py")

    print("Ejecutando:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)

    produced = os.path.join(BUILD_DIR, "gui.dist")
    if not os.path.isdir(produced):
        sys.exit(f"ERROR: no se encontró la carpeta de salida esperada en {produced}")

    os.makedirs(DIST_DIR, exist_ok=True)
    final_path = os.path.join(DIST_DIR, APP_NAME)
    shutil.copytree(produced, final_path)
    print(f"\nBuild final disponible en ./{final_path}")


if __name__ == "__main__":
    main()
