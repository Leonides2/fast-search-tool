
#!/usr/bin/env python3
import os
import re
import csv
import time
import argparse
import threading
import queue
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

# Variables globales para recopilar resultados, errores y estadísticas
results = []
errors = []
lock = threading.Lock()
stats = {"files": 0, "dirs": 0}

def match(name, args):
    """
    Compara el nombre del archivo (sin extensión) con el patrón de búsqueda
    según los argumentos proporcionados (exacto, contiene, regex, sensible a mayúsculas/minúsculas).
    """
    # Obtener el nombre del archivo sin su extensión
    stem = os.path.splitext(name)[0]
    pat = args.pattern
    
    # Normalizar a minúsculas si la búsqueda no es sensible a mayúsculas
    a = stem if args.case_sensitive else stem.lower()
    b = pat if args.case_sensitive else pat.lower()
    
    if args.regex:
        flags = 0 if args.case_sensitive else re.I
        return re.search(pat, stem, flags)
    
    if args.exact:
        return a == b
    
    if args.contains:
        return b in a
        
    # Por defecto, se verifica si empieza con el patrón
    return a.startswith(b)

def worker(q, args, pbar):
    """
    Consumidor de la cola. Procesa las rutas de archivos añadidas por el productor
    y verifica si coinciden con los criterios de búsqueda.
    """
    while True:
        item = q.get()
        # Señal de parada (None) para terminar el hilo worker
        if item is None:
            q.task_done()
            return
        
        try:
            filename = os.path.basename(item)
            if match(filename, args):
                with lock:
                    results.append(item)
        finally:
            with lock:
                stats["files"] += 1
                pbar.update(1)
            q.task_done()

def walk(root, q, args):
    """
    Productor que recorre el sistema de archivos de forma iterativa (usando un stack)
    y añade las rutas de los archivos encontrados a la cola para su procesamiento.
    """
    stack = [root]
    excl = set(args.exclude or [])
    
    while stack:
        current_dir = stack.pop()
        try:
            # os.scandir es más eficiente que os.listdir porque devuelve objetos DirEntry con metadatos
            with os.scandir(current_dir) as it:
                stats["dirs"] += 1
                for entry in it:
                    try:
                        # Si es un directorio y no está excluido, se añade al stack para explorarlo
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in excl:
                                continue
                            stack.append(entry.path)
                        
                        # Si es un archivo, se filtra por extensión si aplica, y se añade a la cola
                        elif entry.is_file(follow_symlinks=False):
                            if args.ext:
                                # Normalizar las extensiones para la comparación (.txt -> txt)
                                file_ext = os.path.splitext(entry.name)[1].lstrip(".").lower()
                                allowed_exts = [x.lower().lstrip(".") for x in args.ext]
                                if file_ext not in allowed_exts:
                                    continue
                            q.put(entry.path)
                    except Exception as ex:
                        errors.append(f"Error procesando entrada en {entry.path}: {ex}")
        except Exception as ex:
            errors.append(f"Error accediendo al directorio {current_dir}: {ex}")

def main():
    # Configuración de los argumentos de línea de comandos
    ap = argparse.ArgumentParser(description="Herramienta rápida de búsqueda de archivos en Python con hilos concurrentes.")
    ap.add_argument("directory", help="Directorio raíz para iniciar la búsqueda.")
    ap.add_argument("pattern", help="Patrón o texto a buscar en el nombre de los archivos.")
    ap.add_argument("-t", "--threads", type=int, default=os.cpu_count() * 4,
                    help="Número de hilos trabajadores (por defecto: CPU_COUNT * 4).")
    ap.add_argument("--exact", action="store_true", help="El nombre debe coincidir exactamente.")
    ap.add_argument("--contains", action="store_true", help="El nombre debe contener el patrón en cualquier posición.")
    ap.add_argument("--regex", action="store_true", help="El patrón se interpreta como una expresión regular.")
    ap.add_argument("-c", "--case-sensitive", action="store_true", help="Búsqueda sensible a mayúsculas y minúsculas.")
    ap.add_argument("--exclude", nargs="*", help="Lista de carpetas a excluir del recorrido.")
    ap.add_argument("--ext", nargs="*", help="Lista de extensiones de archivo permitidas (ej. txt py csv).")
    ap.add_argument("--csv", default="resultados.csv", help="Ruta del archivo CSV de salida (por defecto: resultados.csv).")
    ap.add_argument("--log", default="resultados.log", help="Ruta del archivo de log de salida (por defecto: resultados.log).")
    ap.add_argument("--oldest-first", action="store_true",
                    help="Ordena los resultados del más antiguo al más reciente (por defecto: del más reciente al más antiguo).")
    args = ap.parse_args()

    # Inicializar la cola de archivos con un límite para controlar el uso de memoria
    q = queue.Queue(maxsize=5000)
    start_time = time.time()
    
    # Barra de progreso para feedback visual en tiempo real
    pbar = tqdm(unit=" archivos", desc="Procesados")
    
    # Ejecución concurrente usando un pool de hilos
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        # Iniciar los hilos trabajadores
        for _ in range(args.threads):
            executor.submit(worker, q, args, pbar)
        
        # Iniciar el recorrido de carpetas para llenar la cola
        walk(args.directory, q, args)
        
        # Enviar señal de fin (None) a cada hilo trabajador para que terminen ordenadamente
        for _ in range(args.threads):
            q.put(None)
        
        # Esperar a que todos los elementos de la cola se hayan procesado por completo
        q.join()
        
    pbar.close()
    elapsed_time = time.time() - start_time

    # Obtener fecha de modificación y ordenar los resultados
    def get_file_mtime(path):
        try:
            return os.path.getmtime(path)
        except Exception:
            return 0.0

    # Crear tuplas (ruta, mtime) para ordenar de manera eficiente sin recalcular
    results_with_time = []
    for r in results:
        results_with_time.append((r, get_file_mtime(r)))

    # Ordenar por mtime: reverse=True para el más reciente primero (por defecto)
    reverse_sort = not args.oldest_first
    results_with_time.sort(key=lambda x: x[1], reverse=reverse_sort)

    # Escribir reporte detallado en el archivo de registro (.log)
    with open(args.log, "w", encoding="utf8") as f:
        f.write(f"Archivos procesados: {stats['files']}\n")
        f.write(f"Directorios recorridos: {stats['dirs']}\n")
        f.write(f"Coincidencias encontradas: {len(results)}\n")
        f.write(f"Tiempo de ejecución: {elapsed_time:.2f}s\n\n")
        
        f.write("== ARCHIVOS COINCIDENTES ==\n")
        for r, mtime in results_with_time:
            time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime)) if mtime > 0 else "Desconocida"
            f.write(f"[{time_str}] {r}\n")
        
        if errors:
            f.write("\n== ERRORES ==\n")
            for e in errors:
                f.write(e + "\n")
                
    # Guardar la lista de archivos coincidentes en el archivo CSV
    with open(args.csv, "w", newline="", encoding="utf8") as f:
        writer = csv.writer(f)
        writer.writerow(["ruta", "fecha_modificacion"])
        for r, mtime in results_with_time:
            time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime)) if mtime > 0 else "Desconocida"
            writer.writerow([r, time_str])
            
    # Resumen de resultados por consola
    print(f"\nBúsqueda finalizada en {elapsed_time:.2f} segundos.")
    print(f"Coincidencias encontradas: {len(results)}")
    print(f"Log detallado guardado en: {args.log}")
    print(f"Resultados CSV guardados en: {args.csv}")

if __name__ == "__main__":
    main()

