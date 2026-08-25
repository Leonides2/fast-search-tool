#!/usr/bin/env python3
"""
Interfaz gráfica de la herramienta de búsqueda de archivos.

Regla de oro para no perder rendimiento ni congelar la ventana:
la búsqueda corre en un hilo de fondo (search_core.run_search) y ESE hilo
nunca toca widgets de Tkinter directamente. Solo escribe un par de contadores
simples (_progress_files / _progress_dirs), que la ventana sondea con
self.after() cada ~150 ms. El resultado final viaja por una queue.Queue()
thread-safe. Así, aunque el escaneo procese miles de archivos por segundo,
la interfaz solo se repinta ~6-7 veces por segundo.
"""
import os
import sys
import json
import queue
import threading
import subprocess
from dataclasses import asdict

import customtkinter as ctk
from tkinter import filedialog, messagebox, ttk

from search_core import SearchOptions, run_search, write_log, write_csv, MAX_THREADS

# Forzar salida UTF-8 en consola (por si se lanza desde una terminal) para
# evitar caracteres corruptos en los prints de diagnóstico.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Límite de filas que se pintan en la tabla de resultados. Insertar decenas
# de miles de filas en el Treeview de golpe puede congelar la ventana unos
# segundos; el CSV/log de salida siempre contiene el listado completo, sin
# este límite.
MAX_DISPLAYED_ROWS = 5000

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BuscarArchivos")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        # Persistir la configuración es una comodidad, no algo crítico:
        # si falla (permisos, disco lleno...) no debe romper la GUI.
        pass


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Buscador de Archivos")
        self.geometry("980x680")
        self.minsize(760, 520)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        # --- Estado de la búsqueda en curso ---
        self.search_thread: threading.Thread | None = None
        self.cancel_event: threading.Event | None = None
        self.event_queue: "queue.Queue" = queue.Queue()
        self._progress_files = 0
        self._progress_dirs = 0
        self._last_matches: list[tuple[str, float]] = []

        cfg = load_config()
        self._build_form(cfg)
        self._build_actions()
        self._build_progress()
        self._build_results()

        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    # ------------------------------------------------------------------ UI

    def _build_form(self, cfg: dict):
        frame = ctk.CTkFrame(self)
        frame.pack(fill="x", padx=12, pady=(12, 6))
        frame.grid_columnconfigure(1, weight=1)

        row = 0
        ctk.CTkLabel(frame, text="Directorio").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        self.dir_entry = ctk.CTkEntry(frame)
        self.dir_entry.insert(0, cfg.get("directory", ""))
        self.dir_entry.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        ctk.CTkButton(frame, text="Examinar...", width=100, command=self._browse_dir).grid(
            row=row, column=2, padx=8, pady=6
        )

        row += 1
        ctk.CTkLabel(frame, text="Patrón").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        self.pattern_entry = ctk.CTkEntry(frame)
        self.pattern_entry.insert(0, cfg.get("pattern", ""))
        self.pattern_entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=6)

        row += 1
        ctk.CTkLabel(frame, text="Modo").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        modos_frame = ctk.CTkFrame(frame, fg_color="transparent")
        modos_frame.grid(row=row, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        self.mode_var = ctk.StringVar(value=cfg.get("mode", "startswith"))
        for texto, valor in [
            ("Empieza con", "startswith"),
            ("Contiene", "contains"),
            ("Exacto", "exact"),
            ("Regex", "regex"),
        ]:
            ctk.CTkRadioButton(modos_frame, text=texto, variable=self.mode_var, value=valor).pack(
                side="left", padx=6
            )

        row += 1
        opts_frame = ctk.CTkFrame(frame, fg_color="transparent")
        opts_frame.grid(row=row, column=0, columnspan=3, sticky="w", padx=4, pady=2)
        self.case_var = ctk.BooleanVar(value=cfg.get("case_sensitive", False))
        ctk.CTkCheckBox(opts_frame, text="Sensible a mayúsculas", variable=self.case_var).pack(
            side="left", padx=6
        )
        self.oldest_var = ctk.BooleanVar(value=cfg.get("oldest_first", False))
        ctk.CTkCheckBox(opts_frame, text="Más antiguos primero", variable=self.oldest_var).pack(
            side="left", padx=6
        )

        row += 1
        ctk.CTkLabel(frame, text="Excluir carpetas").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        self.exclude_entry = ctk.CTkEntry(frame, placeholder_text="carpeta1, carpeta2, ...")
        self.exclude_entry.insert(0, cfg.get("exclude", ""))
        self.exclude_entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=6)

        row += 1
        ctk.CTkLabel(frame, text="Extensiones").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        self.ext_entry = ctk.CTkEntry(frame, placeholder_text="txt py csv (vacío = todas)")
        self.ext_entry.insert(0, cfg.get("ext", ""))
        self.ext_entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=6)

        row += 1
        ctk.CTkLabel(frame, text="Hilos").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        default_threads = (os.cpu_count() or 4) * 4
        self.threads_entry = ctk.CTkEntry(frame, width=100)
        self.threads_entry.insert(0, str(cfg.get("threads", default_threads)))
        self.threads_entry.grid(row=row, column=1, sticky="w", padx=8, pady=6)

        row += 1
        ctk.CTkLabel(frame, text="Salida CSV").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        self.csv_entry = ctk.CTkEntry(frame)
        self.csv_entry.insert(0, cfg.get("csv", "resultados.csv"))
        self.csv_entry.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        ctk.CTkButton(frame, text="Guardar como...", width=100, command=self._browse_csv).grid(
            row=row, column=2, padx=8, pady=6
        )

        row += 1
        ctk.CTkLabel(frame, text="Salida Log").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        self.log_entry = ctk.CTkEntry(frame)
        self.log_entry.insert(0, cfg.get("log", "resultados.log"))
        self.log_entry.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        ctk.CTkButton(frame, text="Guardar como...", width=100, command=self._browse_log).grid(
            row=row, column=2, padx=8, pady=6
        )

    def _build_actions(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=12, pady=4)

        self.run_button = ctk.CTkButton(frame, text="Buscar", command=self._on_run)
        self.run_button.pack(side="left", padx=(0, 8))

        self.cancel_button = ctk.CTkButton(
            frame, text="Cancelar", command=self._on_cancel, state="disabled", fg_color="#a13d3d", hover_color="#832f2f"
        )
        self.cancel_button.pack(side="left")

        self.status_label = ctk.CTkLabel(frame, text="Listo.")
        self.status_label.pack(side="left", padx=16)

    def _build_progress(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=12, pady=(0, 4))

        self.progress_bar = ctk.CTkProgressBar(frame, mode="indeterminate")
        self.progress_bar.pack(fill="x", padx=4, pady=4)

        self.progress_label = ctk.CTkLabel(frame, text="")
        self.progress_label.pack(anchor="w", padx=4)

    def _build_results(self):
        frame = ctk.CTkFrame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        columns = ("ruta", "fecha")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("ruta", text="Ruta")
        self.tree.heading("fecha", text="Fecha de modificación")
        self.tree.column("ruta", width=650)
        self.tree.column("fecha", width=180, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.bind("<Double-1>", lambda e: self._open_containing_folder())

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(actions, text="Abrir carpeta contenedora", command=self._open_containing_folder).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(actions, text="Copiar ruta", command=self._copy_path).pack(side="left", padx=(0, 8))
        self.errors_label = ctk.CTkLabel(actions, text="")
        self.errors_label.pack(side="left", padx=16)

    # ------------------------------------------------------------- eventos

    def _browse_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, path)

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            self.csv_entry.delete(0, "end")
            self.csv_entry.insert(0, path)

    def _browse_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".log", filetypes=[("Log", "*.log")])
        if path:
            self.log_entry.delete(0, "end")
            self.log_entry.insert(0, path)

    def _current_options(self) -> SearchOptions:
        ext_raw = self.ext_entry.get().strip()
        ext = ext_raw.split() if ext_raw else None
        try:
            threads = int(self.threads_entry.get().strip())
        except ValueError:
            raise ValueError("El número de hilos debe ser un entero.")

        mode = self.mode_var.get()
        return SearchOptions(
            directory=self.dir_entry.get().strip(),
            pattern=self.pattern_entry.get(),
            threads=threads,
            exact=(mode == "exact"),
            contains=(mode == "contains"),
            regex=(mode == "regex"),
            case_sensitive=self.case_var.get(),
            exclude=self.exclude_entry.get().strip() or None,
            ext=ext,
            oldest_first=self.oldest_var.get(),
        )

    def _on_run(self):
        if self.search_thread and self.search_thread.is_alive():
            return  # ya hay una búsqueda en curso

        try:
            opts = self._current_options()
            opts.validate()
        except ValueError as ex:
            messagebox.showerror("Opciones inválidas", str(ex))
            return

        csv_path = self.csv_entry.get().strip() or "resultados.csv"
        log_path = self.log_entry.get().strip() or "resultados.log"

        # Limpiar resultados de la ejecución anterior antes de arrancar.
        self.tree.delete(*self.tree.get_children())
        self.errors_label.configure(text="")
        self._last_matches = []
        self._progress_files = 0
        self._progress_dirs = 0

        self.cancel_event = threading.Event()
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_label.configure(text="Buscando...")
        self.progress_bar.start()

        def progress_cb(files_now, dirs_now):
            # OJO: se llama desde los hilos worker. Nunca tocar widgets aquí,
            # solo escribir enteros simples (atómico bajo el GIL de CPython).
            self._progress_files = files_now
            self._progress_dirs = dirs_now

        def worker():
            try:
                result = run_search(opts, progress_cb=progress_cb, cancel_event=self.cancel_event)
                self.event_queue.put(("done", result, csv_path, log_path))
            except Exception as ex:
                self.event_queue.put(("error", ex, csv_path, log_path))

        self.search_thread = threading.Thread(target=worker, daemon=True)
        self.search_thread.start()
        self._save_current_config()
        self.after(150, self._poll)

    def _on_cancel(self):
        if self.cancel_event:
            self.cancel_event.set()
            self.status_label.configure(text="Cancelando...")
            self.cancel_button.configure(state="disabled")

    def _poll(self):
        # Actualiza la barra/label de progreso con los últimos contadores
        # (sondeo periódico: la búsqueda puede procesar miles de archivos
        # por segundo, pero la ventana solo se repinta aquí, ~6-7 veces/seg).
        self.progress_label.configure(
            text=f"Archivos procesados: {self._progress_files}  |  Directorios recorridos: {self._progress_dirs}"
        )

        try:
            msg = self.event_queue.get_nowait()
        except queue.Empty:
            if self.search_thread and self.search_thread.is_alive():
                self.after(150, self._poll)
            return

        kind = msg[0]
        if kind == "done":
            _, result, csv_path, log_path = msg
            self._on_search_done(result, csv_path, log_path)
        elif kind == "error":
            _, ex, csv_path, log_path = msg
            self._on_search_error(ex)

    def _on_search_done(self, result, csv_path, log_path):
        self.progress_bar.stop()
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")

        try:
            write_log(log_path, result)
            write_csv(csv_path, result)
        except Exception as ex:
            messagebox.showwarning("No se pudo escribir la salida", str(ex))

        self._last_matches = result.matches
        for path, mtime in result.matches[:MAX_DISPLAYED_ROWS]:
            from search_core import format_time
            self.tree.insert("", "end", values=(path, format_time(mtime)))

        estado = "Cancelada" if result.cancelled else "Completada"
        self.status_label.configure(
            text=f"{estado} en {result.elapsed:.2f}s — {len(result.matches)} coincidencias."
        )
        if len(result.matches) > MAX_DISPLAYED_ROWS:
            self.progress_label.configure(
                text=self.progress_label.cget("text")
                + f"  (mostrando los primeros {MAX_DISPLAYED_ROWS} de {len(result.matches)}; ver CSV/log completo)"
            )
        if result.errors:
            self.errors_label.configure(text=f"⚠ {len(result.errors)} error(es) — ver {log_path}")
        else:
            self.errors_label.configure(text="")

    def _on_search_error(self, ex: Exception):
        self.progress_bar.stop()
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status_label.configure(text="Error.")
        messagebox.showerror("Error durante la búsqueda", str(ex))

    def _selected_path(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        values = self.tree.item(sel[0], "values")
        return values[0] if values else None

    def _open_containing_folder(self):
        path = self._selected_path()
        if not path:
            return
        folder = os.path.dirname(path)
        try:
            os.startfile(folder)  # noqa: type: ignore[attr-defined]  (específico de Windows)
        except Exception as ex:
            messagebox.showerror("No se pudo abrir la carpeta", str(ex))

    def _copy_path(self):
        path = self._selected_path()
        if not path:
            return
        self.clipboard_clear()
        self.clipboard_append(path)

    def _save_current_config(self):
        save_config(
            {
                "directory": self.dir_entry.get().strip(),
                "pattern": self.pattern_entry.get(),
                "mode": self.mode_var.get(),
                "case_sensitive": self.case_var.get(),
                "oldest_first": self.oldest_var.get(),
                "exclude": self.exclude_entry.get().strip(),
                "ext": self.ext_entry.get().strip(),
                "threads": self.threads_entry.get().strip(),
                "csv": self.csv_entry.get().strip(),
                "log": self.log_entry.get().strip(),
            }
        )

    def _on_closing(self):
        if self.search_thread and self.search_thread.is_alive():
            if self.cancel_event:
                self.cancel_event.set()
            # Damos un margen breve a que el hilo se detenga de forma
            # ordenada (el diseño de cancelación cooperativa de search_core
            # garantiza que no se queda colgado) antes de cerrar la ventana.
            self.search_thread.join(timeout=2)
        self._save_current_config()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
