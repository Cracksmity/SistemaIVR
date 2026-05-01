#!/usr/bin/env python3
"""
Interfaz gráfica (tkinter) para calcular el IVR sin usar la línea de comandos.

Ejecución: desde la carpeta del proyecto,
    python ivr_guadalajara_gui.py
"""

from __future__ import annotations

import logging
import threading
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ivr_guadalajara import run_pipeline


class TextWidgetLogHandler(logging.Handler):
    """Envía logs al widget de texto de forma segura desde hilos secundarios."""

    def __init__(self, text_widget: tk.Text) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        self._text = text_widget

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)

        def append() -> None:
            self._text.configure(state=tk.NORMAL)
            self._text.insert(tk.END, msg + "\n")
            self._text.see(tk.END)
            self._text.configure(state=tk.DISABLED)

        try:
            self._text.after(0, append)
        except tk.TclError:
            pass


def main() -> None:
    root = tk.Tk()
    root.title("IVR Guadalajara — Cálculo y mapa")
    root.minsize(640, 520)

    # --- Variables ---
    input_path = tk.StringVar()
    output_html = tk.StringVar(value=str(Path.cwd() / "mapa_ivr_guadalajara.html"))
    export_csv = tk.BooleanVar(value=True)
    export_gpkg = tk.BooleanVar(value=False)
    csv_path = tk.StringVar()
    gpkg_path = tk.StringVar()
    skip_boundary = tk.BooleanVar(value=False)

    frm = ttk.Frame(root, padding=12)
    frm.grid(row=0, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)

    row = 0

    ttk.Label(frm, text="Archivo vectorial (Shapefile o GeoPackage):").grid(
        row=row, column=0, columnspan=3, sticky="w"
    )
    row += 1
    ent_input = ttk.Entry(frm, textvariable=input_path, width=70)
    ent_input.grid(row=row, column=0, columnspan=2, sticky="ew", padx=(0, 8))
    ttk.Button(
        frm,
        text="Buscar…",
        command=lambda: _pick_vector(input_path, output_html, csv_path, gpkg_path),
    ).grid(row=row, column=2, sticky="e")
    row += 1

    ttk.Label(frm, text="Mapa HTML de salida:").grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))
    row += 1
    ttk.Entry(frm, textvariable=output_html, width=70).grid(
        row=row, column=0, columnspan=2, sticky="ew", padx=(0, 8)
    )
    ttk.Button(frm, text="Buscar…", command=lambda: _pick_save_html(output_html)).grid(
        row=row, column=2, sticky="e"
    )
    row += 1

    ttk.Checkbutton(
        frm,
        text="Exportar también tabla CSV",
        variable=export_csv,
    ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))
    row += 1
    ttk.Entry(frm, textvariable=csv_path, width=70).grid(
        row=row, column=0, columnspan=2, sticky="ew", padx=(0, 8)
    )
    ttk.Button(frm, text="Buscar…", command=lambda: _pick_save_csv(csv_path)).grid(
        row=row, column=2, sticky="e"
    )
    row += 1

    ttk.Checkbutton(
        frm,
        text="Exportar también GeoPackage con geometría",
        variable=export_gpkg,
    ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))
    row += 1
    ttk.Entry(frm, textvariable=gpkg_path, width=70).grid(
        row=row, column=0, columnspan=2, sticky="ew", padx=(0, 8)
    )
    ttk.Button(frm, text="Buscar…", command=lambda: _pick_save_gpkg(gpkg_path)).grid(
        row=row, column=2, sticky="e"
    )
    row += 1

    ttk.Checkbutton(
        frm,
        text="No filtrar por límite OSM de Guadalajara (usar todos los polígonos del archivo)",
        variable=skip_boundary,
    ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))
    row += 1

    btn_run = ttk.Button(frm, text="Calcular IVR y generar mapa")
    btn_run.grid(row=row, column=0, pady=(16, 8), sticky="w")
    row += 1

    ttk.Label(frm, text="Registro:").grid(row=row, column=0, columnspan=3, sticky="w")
    row += 1
    log_text = tk.Text(frm, height=16, wrap=tk.WORD, state=tk.DISABLED)
    log_text.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
    frm.rowconfigure(row, weight=1)
    row += 1

    scroll = ttk.Scrollbar(frm, command=log_text.yview)
    scroll.grid(row=row - 1, column=3, sticky="ns")
    log_text.configure(yscrollcommand=scroll.set)

    log_handler = TextWidgetLogHandler(log_text)
    ivr_log = logging.getLogger("ivr_guadalajara")
    ivr_log.setLevel(logging.INFO)
    ivr_log.addHandler(log_handler)
    # Solo registro en pantalla (evita duplicar en consola por basicConfig del módulo)
    ivr_log.propagate = False

    def on_run() -> None:
        inp = input_path.get().strip()
        if not inp:
            messagebox.showwarning("Falta archivo", "Selecciona el archivo vectorial de entrada.")
            return
        if not Path(inp).exists():
            messagebox.showerror("No encontrado", f"No existe el archivo:\n{inp}")
            return

        html_out = output_html.get().strip()
        if not html_out:
            messagebox.showwarning("Salida HTML", "Indica la ruta del mapa HTML de salida.")
            return

        out_csv = csv_path.get().strip() if export_csv.get() else None
        if export_csv.get() and not out_csv:
            messagebox.showwarning("CSV", "Indica la ruta del CSV o desmarca la exportación CSV.")
            return

        out_gpkg = gpkg_path.get().strip() if export_gpkg.get() else None
        if export_gpkg.get() and not out_gpkg:
            messagebox.showwarning("GeoPackage", "Indica la ruta del .gpkg o desmarca esa exportación.")
            return

        btn_run.configure(state=tk.DISABLED)
        log_text.configure(state=tk.NORMAL)
        log_text.delete("1.0", tk.END)
        log_text.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                run_pipeline(
                    shp_path=inp,
                    output_html=html_out,
                    output_csv=out_csv,
                    output_gpkg=out_gpkg,
                    skip_boundary_filter=skip_boundary.get(),
                )
                def _done_ok() -> None:
                    messagebox.showinfo(
                        "Listo",
                        "Proceso terminado.\n\n"
                        f"Mapa: {html_out}\n"
                        + (f"CSV: {out_csv}\n" if out_csv else "")
                        + (f"GPKG: {out_gpkg}" if out_gpkg else ""),
                    )
                    if messagebox.askyesno("Mapa", "¿Abrir el mapa HTML en el navegador?"):
                        webbrowser.open(Path(html_out).resolve().as_uri())

                root.after(0, _done_ok)
            except Exception as exc:  # noqa: BLE001
                root.after(
                    0,
                    lambda e=exc: messagebox.showerror("Error", str(e)),
                )
            finally:
                root.after(0, lambda: btn_run.configure(state=tk.NORMAL))

        threading.Thread(target=worker, daemon=True).start()

    btn_run.configure(command=on_run)

    ttk.Label(
        frm,
        text="Requiere columna POBTOT y polígonos. Necesita conexión a internet para OSM.",
        font=("", 8),
        foreground="gray",
    ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))

    root.mainloop()


def _pick_vector(
    var: tk.StringVar,
    html_var: tk.StringVar,
    csv_var: tk.StringVar,
    gpkg_var: tk.StringVar,
) -> None:
    path = filedialog.askopenfilename(
        title="Archivo vectorial INEGI",
        filetypes=[
            ("Vectorial", "*.shp *.gpkg *.geojson"),
            ("GeoPackage", "*.gpkg"),
            ("Shapefile", "*.shp"),
            ("Todos", "*.*"),
        ],
    )
    if path:
        var.set(path)
        p = Path(path)
        # Salidas por defecto junto al archivo de entrada
        html_var.set(str(p.parent / "mapa_ivr_guadalajara.html"))
        csv_var.set(str(p.parent / f"{p.stem}_ivr.csv"))
        gpkg_var.set(str(p.parent / f"{p.stem}_ivr.gpkg"))


def _pick_save_html(var: tk.StringVar) -> None:
    path = filedialog.asksaveasfilename(
        title="Guardar mapa HTML",
        defaultextension=".html",
        filetypes=[("HTML", "*.html"), ("Todos", "*.*")],
    )
    if path:
        var.set(path)


def _pick_save_csv(var: tk.StringVar) -> None:
    path = filedialog.asksaveasfilename(
        title="Guardar CSV",
        defaultextension=".csv",
        filetypes=[("CSV", "*.csv"), ("Todos", "*.*")],
    )
    if path:
        var.set(path)


def _pick_save_gpkg(var: tk.StringVar) -> None:
    path = filedialog.asksaveasfilename(
        title="Guardar GeoPackage",
        defaultextension=".gpkg",
        filetypes=[("GeoPackage", "*.gpkg"), ("Todos", "*.*")],
    )
    if path:
        var.set(path)


if __name__ == "__main__":
    main()
