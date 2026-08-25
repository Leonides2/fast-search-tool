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

import customtkinter as ctk
from tkinter import filedialog, messagebox, ttk

from search_core import SearchOptions, run_search, format_time, MAX_THREADS

# Forzar salida UTF-8 en consola (por si se lanza desde una terminal) para
# evitar caracteres corruptos en los prints de diagnóstico.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Límite de seguridad para la tabla de resultados: los resultados se insertan
# en lotes (self.after) para no congelar la ventana, pero por encima de este
# número el propio widget Treeview empieza a ir lento sin importar cómo se
# llene, así que se trunca la vista (no la búsqueda) a partir de aquí.
MAX_DISPLAYED_ROWS = 50000
_ROW_BATCH_SIZE = 300

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BuscarArchivos")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Extensiones agrupadas por categoría para el menú de filtros. La clave es lo
# que se muestra en el checkbox; el valor, las extensiones reales que cubre.
EXTENSION_GROUPS = {
    "PDF": ["pdf"],
    "Word": ["doc", "docx"],
    "Excel": ["xls", "xlsx"],
    "PowerPoint": ["ppt", "pptx"],
    "Imágenes": ["jpg", "jpeg", "png", "gif", "bmp"],
    "Comprimidos": ["zip", "rar", "7z"],
    "Texto": ["txt"],
    "Video": ["mp4", "avi", "mkv", "mov"],
}


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
        self.geometry("1000x760")
        self.minsize(820, 600)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        # --- Estado de la búsqueda en curso ---
        self.search_thread: threading.Thread | None = None
        self.cancel_event: threading.Event | None = None
        self.event_queue: "queue.Queue" = queue.Queue()
        self._progress_files = 0
        self._progress_dirs = 0
        self._last_matches: list[tuple[str, float]] = []
        self._last_errors: list[str] = []
        self._advanced_visible = False
        self._applied_theme_mode = None

        cfg = load_config()
        self._build_form(cfg)
        self._build_advanced(cfg)
        self._build_actions()
        self._build_progress()
        self._build_results()

        self._update_target_dependent_ui()
        self._apply_treeview_theme()
        self._watch_theme()

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
        ctk.CTkLabel(frame, text="Buscar").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        target_frame = ctk.CTkFrame(frame, fg_color="transparent")
        target_frame.grid(row=row, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        self.target_var = ctk.StringVar(value=cfg.get("target", "files"))
        ctk.CTkRadioButton(
            target_frame, text="Archivos", variable=self.target_var, value="files",
            command=self._update_target_dependent_ui,
        ).pack(side="left", padx=6)
        ctk.CTkRadioButton(
            target_frame, text="Carpetas", variable=self.target_var, value="dirs",
            command=self._update_target_dependent_ui,
        ).pack(side="left", padx=6)

        row += 1
        self.ext_label = ctk.CTkLabel(frame, text="Extensiones")
        self.ext_label.grid(row=row, column=0, sticky="nw", padx=8, pady=6)
        self.ext_frame = ctk.CTkFrame(frame, fg_color="transparent")
        self.ext_frame.grid(row=row, column=1, columnspan=2, sticky="w", padx=4, pady=2)

        saved_groups = set(cfg.get("ext_groups", []))
        self.ext_group_vars: dict[str, ctk.BooleanVar] = {}
        cols = 4
        for i, group in enumerate(EXTENSION_GROUPS):
            var = ctk.BooleanVar(value=group in saved_groups)
            self.ext_group_vars[group] = var
            cb = ctk.CTkCheckBox(self.ext_frame, text=group, variable=var)
            cb.grid(row=i // cols, column=i % cols, sticky="w", padx=6, pady=3)

        extra_row = (len(EXTENSION_GROUPS) - 1) // cols + 1
        self.ext_other_var = ctk.BooleanVar(value=cfg.get("ext_other_enabled", False))
        self.ext_other_check = ctk.CTkCheckBox(
            self.ext_frame, text="Otras", variable=self.ext_other_var, command=self._update_ext_other_state
        )
        self.ext_other_check.grid(row=extra_row, column=0, sticky="w", padx=6, pady=(6, 3))
        self.ext_other_entry = ctk.CTkEntry(
            self.ext_frame, placeholder_text="ej. log json xml", width=260
        )
        self.ext_other_entry.insert(0, cfg.get("ext_other", ""))
        self.ext_other_entry.grid(row=extra_row, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 3))
        self._update_ext_other_state()

    def _build_advanced(self, cfg: dict):
        self.advanced_toggle = ctk.CTkButton(
            self, text="▸ Opciones avanzadas", anchor="w", fg_color="transparent",
            text_color=("gray10", "gray90"), hover_color=("gray85", "gray25"),
            command=self._toggle_advanced,
        )
        self.advanced_toggle.pack(fill="x", padx=12, pady=(0, 2))

        self.advanced_frame = ctk.CTkFrame(self)
        self.advanced_frame.grid_columnconfigure(1, weight=1)
        # No se hace pack() todavía: arranca oculto (ver _toggle_advanced).

        row = 0
        ctk.CTkLabel(self.advanced_frame, text="Modo").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        modos_frame = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
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
        opts_frame = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
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
        ctk.CTkLabel(self.advanced_frame, text="Excluir carpetas").grid(
            row=row, column=0, sticky="w", padx=8, pady=6
        )
        self.exclude_entry = ctk.CTkEntry(self.advanced_frame, placeholder_text="carpeta1, carpeta2, ...")
        self.exclude_entry.insert(0, cfg.get("exclude", ""))
        self.exclude_entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=6)

        row += 1
        default_threads = (os.cpu_count() or 4) * 4
        ctk.CTkLabel(self.advanced_frame, text="Hilos").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        threads_frame = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
        threads_frame.grid(row=row, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        self.threads_entry = ctk.CTkEntry(threads_frame, width=100)
        self.threads_entry.insert(0, str(cfg.get("threads", default_threads)))
        self.threads_entry.pack(side="left")
        ctk.CTkLabel(
            threads_frame,
            text=f"(automático según CPU: {default_threads}; ajusta solo si lo necesitas)",
            text_color=("gray40", "gray60"),
        ).pack(side="left", padx=10)

    def _toggle_advanced(self):
        self._advanced_visible = not self._advanced_visible
        if self._advanced_visible:
            self.advanced_toggle.configure(text="▾ Opciones avanzadas")
            self.advanced_frame.pack(fill="x", padx=12, pady=(0, 6), after=self.advanced_toggle)
        else:
            self.advanced_toggle.configure(text="▸ Opciones avanzadas")
            self.advanced_frame.pack_forget()

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
        ctk.CTkButton(actions, text="Abrir en el explorador", command=self._open_containing_folder).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(actions, text="Copiar ruta", command=self._copy_path).pack(side="left", padx=(0, 8))
        self.errors_button = ctk.CTkButton(
            actions, text="Ver errores", command=self._show_errors, state="disabled", width=100,
            fg_color="#a13d3d", hover_color="#832f2f",
        )
        self.errors_button.pack(side="left", padx=(0, 8))
        self.errors_label = ctk.CTkLabel(actions, text="")
        self.errors_label.pack(side="left", padx=8)

    # -------------------------------------------------------- tema (dark/light)

    def _apply_treeview_theme(self):
        mode = ctk.get_appearance_mode()  # "Light" o "Dark"
        if mode == self._applied_theme_mode:
            return
        self._applied_theme_mode = mode

        style = ttk.Style()
        style.theme_use("clam")  # los temas nativos de Windows ignoran los colores custom

        if mode == "Dark":
            bg, fg = "#2b2b2b", "#dce4ee"
            heading_bg, heading_fg = "#212121", "#dce4ee"
            selected_bg, selected_fg = "#1f6aa5", "#ffffff"
            trough, arrow = "#212121", "#dce4ee"
        else:
            bg, fg = "#ffffff", "#1a1a1a"
            heading_bg, heading_fg = "#e8e8e8", "#1a1a1a"
            selected_bg, selected_fg = "#3b8ed0", "#ffffff"
            trough, arrow = "#e8e8e8", "#1a1a1a"

        style.configure(
            "Treeview", background=bg, fieldbackground=bg, foreground=fg,
            bordercolor=bg, borderwidth=0, rowheight=24,
        )
        style.map("Treeview", background=[("selected", selected_bg)], foreground=[("selected", selected_fg)])
        style.configure("Treeview.Heading", background=heading_bg, foreground=heading_fg, relief="flat")
        style.map("Treeview.Heading", background=[("active", heading_bg)])
        style.configure(
            "Vertical.TScrollbar", background=heading_bg, troughcolor=trough,
            bordercolor=bg, arrowcolor=arrow,
        )

    def _watch_theme(self):
        # CustomTkinter no siempre notifica cuando el tema del SO cambia en
        # caliente; comprobar cada par de segundos es barato y garantiza que
        # la tabla de resultados (que es un widget ttk aparte) se mantenga
        # sincronizada con el modo claro/oscuro del sistema.
        self._apply_treeview_theme()
        self.after(2000, self._watch_theme)

    # ------------------------------------------------------------- eventos

    def _browse_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, path)

    def _update_target_dependent_ui(self):
        is_files = self.target_var.get() == "files"
        state = "normal" if is_files else "disabled"
        for widget in self.ext_frame.winfo_children():
            widget.configure(state=state)
        if is_files:
            self._update_ext_other_state()
        self.ext_label.configure(text_color=("gray10", "gray90") if is_files else ("gray50", "gray50"))

    def _update_ext_other_state(self):
        self.ext_other_entry.configure(state="normal" if self.ext_other_var.get() else "disabled")

    def _selected_extensions(self) -> list[str] | None:
        if self.target_var.get() != "files":
            return None
        ext: list[str] = []
        for group, var in self.ext_group_vars.items():
            if var.get():
                ext.extend(EXTENSION_GROUPS[group])
        if self.ext_other_var.get():
            extra = self.ext_other_entry.get().strip()
            if extra:
                ext.extend(x.strip().lstrip(".").lower() for x in extra.replace(",", " ").split())
        return ext or None

    def _current_options(self) -> SearchOptions:
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
            ext=self._selected_extensions(),
            oldest_first=self.oldest_var.get(),
            target=self.target_var.get(),
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

        # Limpiar resultados de la ejecución anterior antes de arrancar.
        self.tree.delete(*self.tree.get_children())
        self.errors_label.configure(text="")
        self.errors_button.configure(state="disabled")
        self._last_matches = []
        self._last_errors = []
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
                self.event_queue.put(("done", result))
            except Exception as ex:
                self.event_queue.put(("error", ex))

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
        # (sondeo periódico: la búsqueda puede procesar miles de elementos
        # por segundo, pero la ventana solo se repinta aquí, ~6-7 veces/seg).
        etiqueta = "Archivos" if self.target_var.get() == "files" else "Carpetas"
        self.progress_label.configure(
            text=f"{etiqueta} analizados: {self._progress_files}  |  Directorios recorridos: {self._progress_dirs}"
        )

        try:
            msg = self.event_queue.get_nowait()
        except queue.Empty:
            if self.search_thread and self.search_thread.is_alive():
                self.after(150, self._poll)
            return

        kind, payload = msg
        if kind == "done":
            self._on_search_done(payload)
        elif kind == "error":
            self._on_search_error(payload)

    def _on_search_done(self, result):
        self.progress_bar.stop()
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")

        self._last_matches = result.matches
        self._last_errors = result.errors

        truncated = len(result.matches) > MAX_DISPLAYED_ROWS
        to_show = result.matches[:MAX_DISPLAYED_ROWS]
        self._populate_results_batched(to_show)

        estado = "Cancelada" if result.cancelled else "Completada"
        self.status_label.configure(
            text=f"{estado} en {result.elapsed:.2f}s — {len(result.matches)} coincidencias."
        )
        if truncated:
            self.progress_label.configure(
                text=self.progress_label.cget("text")
                + f"  (mostrando los primeros {MAX_DISPLAYED_ROWS} de {len(result.matches)}; afina la búsqueda para acotar)"
            )
        if result.errors:
            self.errors_label.configure(text=f"⚠ {len(result.errors)} error(es) durante la búsqueda")
            self.errors_button.configure(state="normal")
        else:
            self.errors_label.configure(text="")
            self.errors_button.configure(state="disabled")

    def _populate_results_batched(self, matches, index=0):
        # Inserta en lotes vía self.after() en vez de todo de golpe: con miles
        # de coincidencias, un solo bucle de insert() puede congelar la
        # ventana perceptiblemente. Repartido en lotes, la ventana sigue
        # respondiendo (se puede hasta scrollear) mientras se termina de llenar.
        end = min(index + _ROW_BATCH_SIZE, len(matches))
        for path, mtime in matches[index:end]:
            self.tree.insert("", "end", values=(path, format_time(mtime)))
        if end < len(matches):
            self.after(1, lambda: self._populate_results_batched(matches, end))

    def _on_search_error(self, ex: Exception):
        self.progress_bar.stop()
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status_label.configure(text="Error.")
        messagebox.showerror("Error durante la búsqueda", str(ex))

    def _show_errors(self):
        if not self._last_errors:
            return
        top = ctk.CTkToplevel(self)
        top.title(f"Errores ({len(self._last_errors)})")
        top.geometry("700x400")
        textbox = ctk.CTkTextbox(top, wrap="word")
        textbox.pack(fill="both", expand=True, padx=10, pady=10)
        textbox.insert("1.0", "\n".join(self._last_errors))
        textbox.configure(state="disabled")
        top.transient(self)
        top.focus()

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
        target = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            os.startfile(target)  # noqa: type: ignore[attr-defined]  (específico de Windows)
        except Exception as ex:
            messagebox.showerror("No se pudo abrir la ubicación", str(ex))

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
                "target": self.target_var.get(),
                "mode": self.mode_var.get(),
                "case_sensitive": self.case_var.get(),
                "oldest_first": self.oldest_var.get(),
                "exclude": self.exclude_entry.get().strip(),
                "ext_groups": [g for g, v in self.ext_group_vars.items() if v.get()],
                "ext_other_enabled": self.ext_other_var.get(),
                "ext_other": self.ext_other_entry.get().strip(),
                "threads": self.threads_entry.get().strip(),
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
