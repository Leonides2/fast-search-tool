#!/usr/bin/env python3
"""
Núcleo de búsqueda reutilizable.

Toda la lógica de recorrido/filtrado vive aquí, sin estado a nivel de módulo
(a diferencia de la versión original, que usaba listas y contadores globales).
Esto permite que tanto la CLI (buscar.py) como la GUI (gui.py) llamen a
run_search() varias veces en el mismo proceso sin que una ejecución deje
residuos de estado para la siguiente.
"""
import os
import re
import time
import threading
import queue
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# Límite razonable de hilos, para evitar agotar los recursos del sistema
# si el usuario indica un valor absurdamente alto.
MAX_THREADS = 256

# Tamaño de la cola de archivos pendientes: acota el uso de memoria durante
# el recorrido en árboles de directorios muy grandes.
QUEUE_MAXSIZE = 5000


@dataclass
class SearchOptions:
    """Parámetros de una búsqueda. Espejo de los argumentos de la CLI."""
    directory: str
    pattern: str
    threads: int = field(default_factory=lambda: (os.cpu_count() or 4) * 4)
    exact: bool = False
    contains: bool = False
    regex: bool = False
    case_sensitive: bool = False
    exclude: Optional[str] = None
    ext: Optional[List[str]] = None
    oldest_first: bool = False

    def validate(self) -> None:
        """Lanza ValueError con un mensaje claro si la combinación de opciones
        es inválida, en vez de dejar que el error aparezca más tarde como un
        traceback confuso o (peor) un cuelgue silencioso."""
        if not self.directory or not str(self.directory).strip():
            raise ValueError("Debes indicar un directorio.")
        if not self.pattern:
            raise ValueError("Debes indicar un patrón de búsqueda.")
        if self.threads < 1:
            raise ValueError("El número de hilos debe ser al menos 1.")
        if self.threads > MAX_THREADS:
            self.threads = MAX_THREADS
        modos = sum([bool(self.exact), bool(self.contains), bool(self.regex)])
        if modos > 1:
            raise ValueError("--exact, --contains y --regex son mutuamente excluyentes.")
        if self.regex:
            try:
                re.compile(self.pattern)
            except re.error as ex:
                raise ValueError(f"Patrón de expresión regular inválido: {ex}")


@dataclass
class SearchResult:
    """Resultado de una búsqueda ya completada (o cancelada)."""
    matches: List[Tuple[str, float]]   # (ruta, mtime) ordenados
    errors: List[str]
    files_scanned: int
    dirs_scanned: int
    symlinks_skipped: int
    elapsed: float
    cancelled: bool = False


def _match(name: str, opts: SearchOptions):
    """Compara el nombre del archivo (sin extensión) con el patrón de búsqueda
    según el modo indicado (exacto, contiene, regex, sensible a mayúsculas)."""
    stem = os.path.splitext(name)[0]
    pat = opts.pattern

    a = stem if opts.case_sensitive else stem.lower()
    b = pat if opts.case_sensitive else pat.lower()

    if opts.regex:
        flags = 0 if opts.case_sensitive else re.I
        return re.search(pat, stem, flags)
    if opts.exact:
        return a == b
    if opts.contains:
        return b in a
    return a.startswith(b)


def run_search(
    opts: SearchOptions,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> SearchResult:
    """Ejecuta una búsqueda completa (bloqueante) y devuelve el resultado.

    progress_cb(archivos_procesados, directorios_recorridos) se invoca desde
    los hilos worker en cada archivo procesado — debe ser barata (por ejemplo,
    guardar los dos números en una variable compartida) y NUNCA debe tocar
    directamente widgets de una GUI, ya que se llama desde hilos que no son
    el hilo principal.

    cancel_event permite pedir una cancelación cooperativa: deja de encolar
    archivos nuevos y los workers dejan de procesarlos, pero el vaciado de la
    cola y el cierre de los hilos sigue el mismo camino ordenado que un
    término normal (sin dejar hilos colgados ni recursos sin liberar).
    """
    opts.validate()
    cancel_event = cancel_event or threading.Event()

    results: List[str] = []
    errors: List[str] = []
    lock = threading.Lock()
    stats = {"files": 0, "dirs": 0, "symlinks": 0}
    q: "queue.Queue" = queue.Queue(maxsize=QUEUE_MAXSIZE)

    excl = set(x.strip() for x in opts.exclude.split(",") if x.strip()) if opts.exclude else set()
    allowed_exts = [x.lower().lstrip(".") for x in opts.ext] if opts.ext else None

    def worker():
        while True:
            item = q.get()
            if item is None:
                q.task_done()
                return
            try:
                if not cancel_event.is_set():
                    filename = os.path.basename(item)
                    if _match(filename, opts):
                        with lock:
                            results.append(item)
            except Exception as ex:
                # No dejamos morir el hilo: un worker de menos respecto a los
                # sentinelas enviados provoca un cuelgue permanente en q.join().
                with lock:
                    errors.append(f"Error procesando archivo {item}: {ex}")
            finally:
                with lock:
                    stats["files"] += 1
                    files_now, dirs_now = stats["files"], stats["dirs"]
                q.task_done()
                if progress_cb:
                    progress_cb(files_now, dirs_now)

    def walk():
        stack = [opts.directory]
        while stack and not cancel_event.is_set():
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    stats["dirs"] += 1
                    for entry in it:
                        if cancel_event.is_set():
                            break
                        try:
                            # Los symlinks se omiten deliberadamente (a archivo o a
                            # carpeta): seguirlos podría provocar un recorrido infinito
                            # si hay un enlace circular, colgando el programa o
                            # agotando memoria de forma indefinida.
                            if entry.is_symlink():
                                stats["symlinks"] += 1
                                continue

                            if entry.is_dir(follow_symlinks=False):
                                if entry.name in excl:
                                    continue
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                if allowed_exts is not None:
                                    file_ext = os.path.splitext(entry.name)[1].lstrip(".").lower()
                                    if file_ext not in allowed_exts:
                                        continue
                                # put() con timeout en bucle: si se cancela mientras
                                # la cola está llena, no nos quedamos bloqueados
                                # esperando espacio para siempre.
                                while not cancel_event.is_set():
                                    try:
                                        q.put(entry.path, timeout=0.2)
                                        break
                                    except queue.Full:
                                        continue
                        except Exception as ex:
                            errors.append(f"Error procesando entrada en {entry.path}: {ex}")
            except Exception as ex:
                errors.append(f"Error accediendo al directorio {current_dir}: {ex}")

    start_time = time.time()
    with ThreadPoolExecutor(max_workers=opts.threads) as executor:
        for _ in range(opts.threads):
            executor.submit(worker)
        try:
            walk()
        except Exception as ex:
            # Si el recorrido falla de forma inesperada, igual hay que enviar los
            # sentinelas en el finally: si no, los workers quedan bloqueados en
            # q.get() para siempre y la búsqueda se cuelga.
            errors.append(f"Error fatal durante el recorrido de directorios: {ex}")
        finally:
            for _ in range(opts.threads):
                q.put(None)
        q.join()
    elapsed = time.time() - start_time

    def get_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except Exception:
            return 0.0

    matches = [(r, get_mtime(r)) for r in results]
    matches.sort(key=lambda x: x[1], reverse=not opts.oldest_first)

    return SearchResult(
        matches=matches,
        errors=errors,
        files_scanned=stats["files"],
        dirs_scanned=stats["dirs"],
        symlinks_skipped=stats["symlinks"],
        elapsed=elapsed,
        cancelled=cancel_event.is_set(),
    )


def format_time(mtime: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime > 0 else "Desconocida"


def write_log(path: str, result: SearchResult) -> None:
    with open(path, "w", encoding="utf8") as f:
        f.write(f"Archivos procesados: {result.files_scanned}\n")
        f.write(f"Directorios recorridos: {result.dirs_scanned}\n")
        f.write(f"Symlinks omitidos (no se siguen): {result.symlinks_skipped}\n")
        f.write(f"Coincidencias encontradas: {len(result.matches)}\n")
        f.write(f"Tiempo de ejecución: {result.elapsed:.2f}s\n")
        if result.cancelled:
            f.write("Búsqueda CANCELADA por el usuario (resultados parciales).\n")
        f.write("\n== ARCHIVOS COINCIDENTES ==\n")
        for r, mtime in result.matches:
            f.write(f"[{format_time(mtime)}] {r}\n")
        if result.errors:
            f.write("\n== ERRORES ==\n")
            for e in result.errors:
                f.write(e + "\n")


def write_csv(path: str, result: SearchResult) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf8") as f:
        writer = csv.writer(f)
        writer.writerow(["ruta", "fecha_modificacion"])
        for r, mtime in result.matches:
            writer.writerow([r, format_time(mtime)])
