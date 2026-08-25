#!/usr/bin/env python3
import os
import sys
import argparse
from tqdm import tqdm

from search_core import SearchOptions, run_search, write_log, write_csv, MAX_THREADS

# Forzar salida UTF-8 en consola para evitar caracteres corruptos (mojibake)
# en terminales que no usan UTF-8 por defecto (p. ej. cmd.exe con code page legado).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    # Configuración de los argumentos de línea de comandos
    ap = argparse.ArgumentParser(description="Herramienta rápida de búsqueda de archivos en Python con hilos concurrentes.")
    ap.add_argument("directory", help="Directorio raíz para iniciar la búsqueda.")
    ap.add_argument("pattern", help="Patrón o texto a buscar en el nombre de los archivos.")
    ap.add_argument("-t", "--threads", type=int, default=(os.cpu_count() or 4) * 4,
                    help=f"Número de hilos trabajadores (por defecto: CPU_COUNT * 4, máximo {MAX_THREADS}).")
    # Mutuamente excluyentes: solo uno de estos modos de coincidencia puede usarse a la vez.
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--exact", action="store_true", help="El nombre debe coincidir exactamente.")
    modo.add_argument("--contains", action="store_true", help="El nombre debe contener el patrón en cualquier posición.")
    modo.add_argument("--regex", action="store_true", help="El patrón se interpreta como una expresión regular.")
    ap.add_argument("-c", "--case-sensitive", action="store_true", help="Búsqueda sensible a mayúsculas y minúsculas.")
    ap.add_argument("--exclude", help="Lista de carpetas a excluir, separadas por comas.")
    ap.add_argument("--ext", nargs="*", help="Lista de extensiones de archivo permitidas (ej. txt py csv).")
    ap.add_argument("--csv", default="resultados.csv", help="Ruta del archivo CSV de salida (por defecto: resultados.csv).")
    ap.add_argument("--log", default="resultados.log", help="Ruta del archivo de log de salida (por defecto: resultados.log).")
    ap.add_argument("--oldest-first", action="store_true",
                    help="Ordena los resultados del más antiguo al más reciente (por defecto: del más reciente al más antiguo).")
    args = ap.parse_args()

    opts = SearchOptions(
        directory=args.directory,
        pattern=args.pattern,
        threads=args.threads,
        exact=args.exact,
        contains=args.contains,
        regex=args.regex,
        case_sensitive=args.case_sensitive,
        exclude=args.exclude,
        ext=args.ext,
        oldest_first=args.oldest_first,
    )

    try:
        opts.validate()
    except ValueError as ex:
        ap.error(str(ex))
        return
    if opts.threads != args.threads:
        print(f"Aviso: --threads={args.threads} es demasiado alto; se limita a {opts.threads}.")

    excl = set(x.strip() for x in opts.exclude.split(",") if x.strip()) if opts.exclude else set()
    print(f"Excluyendo carpetas: {excl}" if excl else "No se excluyen carpetas.")

    # Barra de progreso para feedback visual en tiempo real
    pbar = tqdm(unit=" archivos", desc="Procesados")

    def progress_cb(files_now, dirs_now):
        # Se invoca una vez por archivo procesado: un simple update(1) basta
        # (tqdm throttlea el redibujado internamente, así que es seguro
        # llamarlo miles de veces por segundo sin frenar la búsqueda).
        pbar.update(1)

    result = run_search(opts, progress_cb=progress_cb)
    pbar.close()

    write_log(args.log, result)
    write_csv(args.csv, result)

    # Resumen de resultados por consola
    print(f"\nBúsqueda finalizada en {result.elapsed:.2f} segundos.")
    print(f"Coincidencias encontradas: {len(result.matches)}")
    print(f"Log detallado guardado en: {args.log}")
    print(f"Resultados CSV guardados en: {args.csv}")


if __name__ == "__main__":
    main()
