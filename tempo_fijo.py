"""Tempo Fijo: inspect and conform MP3s for DJ beatgrids."""

from __future__ import annotations

import json
import hashlib
import queue
import subprocess
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Tempo Fijo"


def app_resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


def release_config() -> dict:
    try:
        return json.loads(app_resource("release_config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}


def release_repository() -> str:
    return str(release_config().get("github_repo", "")).strip()


APP_VERSION = str(release_config().get("app_version", "0.1.0"))


def load_audio_tools():
    try:
        import librosa
        import miniaudio
        import lameenc
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Faltan componentes de audio. Reinstala Tempo Fijo con su instalador actualizado."
        ) from exc
    return librosa, np, miniaudio, lameenc


@dataclass
class TrackAnalysis:
    sample_rate: int
    audio: object
    duration: float
    beat_times: object
    segments: list[tuple[float, float, float]]


def analyze_track(path: Path, progress=None) -> TrackAnalysis:
    librosa, np, miniaudio, _, = load_audio_tools()
    if progress:
        progress("Leyendo MP3…")
    info = miniaudio.get_file_info(str(path))
    decoded = miniaudio.decode_file(
        str(path), output_format=miniaudio.SampleFormat.FLOAT32, nchannels=2, sample_rate=info.sample_rate
    )
    sr = int(decoded.sample_rate)
    audio = np.asarray(decoded.samples, dtype=np.float32).reshape(-1, decoded.nchannels).T
    analysis_sr = 22050
    mono = librosa.resample(np.mean(audio, axis=0), orig_sr=sr, target_sr=analysis_sr)
    duration = audio.shape[-1] / sr
    if progress:
        progress("Detectando pulsos y cambios de tempo…")
    _, beat_frames = librosa.beat.beat_track(y=mono, sr=analysis_sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=analysis_sr)
    if len(beat_times) < 4:
        raise RuntimeError("No pude detectar suficientes pulsos. Prueba con un MP3 más claro o con menos silencio inicial.")

    # Estimate tempo in overlapping 12-second windows. This is an aid for review,
    # not a claim that every musical transient is a beat.
    window = 12.0
    step = 6.0
    estimates: list[tuple[float, float, float]] = []
    for start in np.arange(0.0, max(0.0, duration - 2.0), step):
        end = min(duration, start + window)
        piece = mono[int(start * sr):int(end * sr)]
        if len(piece) < sr * 4:
            continue
        tempo = librosa.beat.tempo(y=piece, sr=analysis_sr, aggregate=np.median)
        bpm = float(np.asarray(tempo).reshape(-1)[0])
        estimates.append((float(start), float(end), bpm))
    return TrackAnalysis(sr, audio, duration, beat_times, estimates)


def local_conform(analysis: TrackAnalysis, bpm: float, start: float, end: float, progress=None):
    """Warp each detected beat interval to one target beat duration, preserving pitch."""
    librosa, np, _, _ = load_audio_tools()
    audio = analysis.audio
    if audio.ndim == 1:
        audio = audio[np.newaxis, :]
    target_len = max(1, round(analysis.sample_rate * 60.0 / bpm))
    marks = [float(start)]
    marks.extend(float(t) for t in analysis.beat_times if start + 0.025 < t < end - 0.025)
    marks.append(float(end))
    marks = sorted(set(marks))
    out = []
    for i, (left, right) in enumerate(zip(marks, marks[1:])):
        a = max(0, int(left * analysis.sample_rate))
        b = min(audio.shape[-1], int(right * analysis.sample_rate))
        chunk = audio[:, a:b]
        if chunk.shape[-1] < 32:
            continue
        if i == 0 or i == len(marks) - 2:
            # Keep intro/outro outside the detected beat grid at their original length.
            expected = chunk.shape[-1]
        else:
            expected = target_len
        rate = chunk.shape[-1] / expected
        rate = min(2.0, max(0.5, rate))
        stretched = librosa.effects.time_stretch(chunk, rate=rate, n_fft=2048, hop_length=512)
        if stretched.shape[-1] > expected:
            stretched = stretched[:, :expected]
        elif stretched.shape[-1] < expected:
            stretched = np.pad(stretched, ((0, 0), (0, expected - stretched.shape[-1])))
        out.append(stretched)
        if progress and i % 32 == 0:
            progress(f"Alineando pulso {i + 1} de {len(marks) - 1}…")
    if not out:
        raise RuntimeError("No hay audio suficiente en el tramo seleccionado.")
    # Short edge fades soften boundaries without shortening the target beat intervals.
    fade = max(32, int(analysis.sample_rate * 0.012))
    merged = out[0]
    for piece in out[1:]:
        n = min(fade, merged.shape[-1], piece.shape[-1])
        if n:
            x = np.linspace(0.0, np.pi / 2.0, n, endpoint=False)
            merged[:, -n:] *= np.cos(x)[None, :]
            piece[:, :n] *= np.sin(x)[None, :]
        merged = np.concatenate((merged, piece), axis=1)
    return merged


def save_mp3(audio, sr: int, destination: Path):
    _, np, _, lameenc = load_audio_tools()
    if audio.ndim == 2:
        channels = audio.shape[0]
        interleaved = audio.T
    else:
        channels = 1
        interleaved = audio.reshape(-1, 1)
    pcm = (np.clip(interleaved, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    encoder = lameenc.Encoder()
    encoder.set_in_sample_rate(int(sr))
    encoder.set_channels(int(channels))
    encoder.set_bit_rate(320)
    encoder.set_quality(2)
    encoded = encoder.encode(pcm) + encoder.flush()
    destination.write_bytes(encoded)


def version_tuple(value: str) -> tuple[int, ...]:
    cleaned = value.strip().lstrip("vV").split("-", 1)[0]
    return tuple(int(part) for part in cleaned.split(".") if part.isdigit())


def check_for_update() -> dict:
    repository = release_repository()
    if not repository or "/" not in repository:
        raise RuntimeError("Las actualizaciones se activarán cuando el proyecto se publique en un repositorio de versiones.")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Tempo-Fijo-Updater"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        release = json.loads(response.read().decode("utf-8"))
    tag = str(release.get("tag_name", ""))
    if version_tuple(tag) <= version_tuple(APP_VERSION):
        return {"available": False, "version": tag}
    assets = {item.get("name"): item.get("browser_download_url") for item in release.get("assets", [])}
    installer_url = assets.get("TempoFijoSetup.exe")
    checksum_url = assets.get("TempoFijoSetup.exe.sha256")
    if not installer_url or not checksum_url:
        raise RuntimeError("La versión nueva no incluye el instalador y su archivo de verificación.")
    return {"available": True, "version": tag, "installer_url": installer_url,
            "checksum_url": checksum_url, "notes": release.get("body", "")}


def install_update(release: dict):
    def fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Tempo-Fijo-Updater"})
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()

    installer_data = fetch(release["installer_url"])
    checksum_text = fetch(release["checksum_url"]).decode("ascii", errors="ignore").strip().split()[0]
    if hashlib.sha256(installer_data).hexdigest().lower() != checksum_text.lower():
        raise RuntimeError("La verificación de seguridad del instalador no coincide.")
    installer = Path(tempfile.gettempdir()) / "TempoFijoSetup.exe"
    installer.write_bytes(installer_data)
    subprocess.Popen([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], close_fds=True)


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class TempoFijoApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x650")
        self.minsize(680, 580)
        self.configure(bg="#f5f6f8")
        self.file_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="analyze")
        self.bpm_var = tk.StringVar(value="128")
        self.bpm2_var = tk.StringVar(value="126")
        self.change_var = tk.StringVar(value="60")
        self.status_var = tk.StringVar(value="Selecciona un MP3 para comenzar.")
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self._build_ui()
        self.after(120, self._poll_events)

    def _build_ui(self):
        root = ttk.Frame(self, padding=24)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="Tempo Fijo", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(root, text="Prepara copias de tus MP3 para que el beatgrid siga el tempo que indiques.",
                  font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 18))

        file_frame = ttk.LabelFrame(root, text="Archivo de audio", padding=12)
        file_frame.pack(fill="x", pady=(0, 14))
        row = ttk.Frame(file_frame)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.file_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Elegir MP3…", command=self._pick_file).pack(side="left", padx=(8, 0))

        modes = ttk.LabelFrame(root, text="Elige un modo", padding=12)
        modes.pack(fill="x", pady=(0, 14))
        choices = [
            ("analyze", "1. Analizar variaciones", "Muestra BPM estimado por tramos y detecta posibles cambios."),
            ("fixed", "2. Corregir a BPM fijo", "Crea una copia alineando cada pulso al BPM que indiques."),
            ("transition", "3. Corregir transición de dos BPM", "Alinea dos secciones con BPM distintos y un punto de cambio."),
        ]
        for value, title, desc in choices:
            line = ttk.Frame(modes)
            line.pack(fill="x", pady=3)
            ttk.Radiobutton(line, text=title, variable=self.mode_var, value=value,
                            command=self._update_fields).pack(anchor="w")
            ttk.Label(line, text=desc, foreground="#555555").pack(anchor="w", padx=(28, 0))

        settings = ttk.LabelFrame(root, text="Parámetros", padding=12)
        settings.pack(fill="x", pady=(0, 14))
        self.fixed_row = ttk.Frame(settings)
        ttk.Label(self.fixed_row, text="BPM objetivo:").pack(side="left")
        ttk.Entry(self.fixed_row, textvariable=self.bpm_var, width=9).pack(side="left", padx=8)
        ttk.Label(self.fixed_row, text="Ejemplo: 128").pack(side="left")
        self.transition_row = ttk.Frame(settings)
        ttk.Label(self.transition_row, text="Primer BPM:").pack(side="left")
        ttk.Entry(self.transition_row, textvariable=self.bpm_var, width=8).pack(side="left", padx=6)
        ttk.Label(self.transition_row, text="Segundo BPM:").pack(side="left", padx=(8, 0))
        ttk.Entry(self.transition_row, textvariable=self.bpm2_var, width=8).pack(side="left", padx=6)
        ttk.Label(self.transition_row, text="Cambio en segundo:").pack(side="left", padx=(8, 0))
        ttk.Entry(self.transition_row, textvariable=self.change_var, width=8).pack(side="left", padx=6)
        ttk.Label(settings, text="Se conserva el tono. El original no se modifica; el MP3 corregido se guarda como copia.",
                  foreground="#555555").pack(anchor="w", pady=(10, 0))

        action_row = ttk.Frame(root)
        action_row.pack(fill="x", pady=(0, 10))
        self.run_button = ttk.Button(action_row, text="Analizar MP3", command=self._run)
        self.run_button.pack(side="left")
        self.update_button = ttk.Button(action_row, text="Buscar actualizaciones", command=self._check_updates)
        self.update_button.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(action_row, mode="indeterminate", length=190)
        self.progress.pack(side="left", padx=12)
        ttk.Label(action_row, textvariable=self.status_var).pack(side="left", fill="x", expand=True)

        result_frame = ttk.LabelFrame(root, text="Resultado", padding=8)
        result_frame.pack(fill="both", expand=True)
        self.result = tk.Text(result_frame, height=12, wrap="word", font=("Consolas", 10), state="disabled")
        self.result.pack(fill="both", expand=True)
        self._update_fields()

    def _check_updates(self):
        self.update_button.configure(state="disabled")
        self.status_var.set("Buscando actualizaciones…")
        def worker():
            try:
                result = check_for_update()
                self.events.put(("update", result))
            except Exception as exc:
                self.events.put(("update_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _install_update_worker(self, release):
        try:
            install_update(release)
            self.events.put(("update_installed", None))
        except Exception as exc:
            self.events.put(("update_install_error", str(exc)))

    def _pick_file(self):
        name = filedialog.askopenfilename(title="Selecciona un archivo MP3", filetypes=[("MP3", "*.mp3")])
        if name:
            self.file_var.set(name)

    def _update_fields(self):
        mode = self.mode_var.get()
        self.fixed_row.pack_forget()
        self.transition_row.pack_forget()
        if mode == "fixed":
            self.fixed_row.pack(anchor="w")
        elif mode == "transition":
            self.transition_row.pack(anchor="w")
        self.run_button.configure(text="Analizar MP3" if mode == "analyze" else "Crear MP3 corregido")

    def _set_result(self, text):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)
        self.result.configure(state="disabled")

    def _run(self):
        if self.busy:
            return
        source = Path(self.file_var.get().strip())
        if not source.is_file() or source.suffix.lower() != ".mp3":
            messagebox.showerror(APP_TITLE, "Selecciona un archivo MP3 válido.")
            return
        mode = self.mode_var.get()
        try:
            bpm1 = float(self.bpm_var.get())
            bpm2 = float(self.bpm2_var.get())
            change = float(self.change_var.get())
            if not 40 <= bpm1 <= 240 or not 40 <= bpm2 <= 240:
                raise ValueError("Los BPM deben estar entre 40 y 240.")
            if change <= 0:
                raise ValueError("El punto de cambio debe ser un segundo mayor que cero.")
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, f"Revisa los parámetros: {exc}")
            return
        self.busy = True
        self.progress.start(12)
        self.run_button.configure(state="disabled")
        self.status_var.set("Iniciando…")
        self._set_result("")
        threading.Thread(target=self._worker, args=(source, mode, bpm1, bpm2, change), daemon=True).start()

    def _worker(self, source: Path, mode: str, bpm1: float, bpm2: float, change: float):
        try:
            def progress(message):
                self.events.put(("status", message))
            analysis = analyze_track(source, progress)
            if mode == "analyze":
                lines = [f"Duración: {format_time(analysis.duration)}", "", "BPM estimado por tramo (estimación automática):"]
                lines.extend(f"{format_time(a)}–{format_time(b)}   {bpm:.2f} BPM" for a, b, bpm in analysis.segments)
                diffs = [bpm for _, _, bpm in analysis.segments]
                if diffs:
                    lines += ["", f"Rango estimado: {min(diffs):.2f}–{max(diffs):.2f} BPM"]
                lines += ["", "El análisis es una referencia; escucha el resultado y confirma el BPM musical."]
                report = source.with_name(source.stem + "_analisis_tempo.txt")
                report.write_text("\n".join(lines), encoding="utf-8")
                self.events.put(("done", "\n".join(lines) + f"\n\nReporte guardado en:\n{report}"))
                return
            if mode == "fixed":
                self.events.put(("status", f"Corrigiendo a {bpm1:g} BPM…"))
                output = local_conform(analysis, bpm1, 0.0, analysis.duration, progress)
            else:
                if change >= analysis.duration - 1:
                    raise RuntimeError("El punto de cambio debe quedar antes del final de la canción.")
                cut_idx = max(0, min(len(analysis.beat_times) - 1,
                                     next((i for i, t in enumerate(analysis.beat_times) if t >= change), len(analysis.beat_times) - 1)))
                cut = float(analysis.beat_times[cut_idx])
                first = local_conform(analysis, bpm1, 0.0, cut, progress)
                second = local_conform(analysis, bpm2, cut, analysis.duration, progress)
                _, np, _, _ = load_audio_tools()
                output = np.concatenate((first, second), axis=-1)
            destination = source.with_name(source.stem + (f"_tempo_{bpm1:g}" if mode == "fixed" else f"_transicion_{bpm1:g}_{bpm2:g}") + "_corregido.mp3")
            self.events.put(("status", "Guardando MP3 corregido…"))
            save_mp3(output, analysis.sample_rate, destination)
            msg = (f"MP3 corregido guardado en:\n{destination}\n\n"
                   f"BPM objetivo: {bpm1:g}" + (f" → {bpm2:g}\nCambio alineado cerca de {format_time(cut)}." if mode == "transition" else "") +
                   "\n\nVuelve a analizar esta copia en Rekordbox y revisa el beatgrid.")
            self.events.put(("done", msg))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(value)
                elif kind == "update":
                    self.update_button.configure(state="normal")
                    if not value["available"]:
                        self.status_var.set("Ya tienes la versión más reciente.")
                        messagebox.showinfo(APP_TITLE, f"Ya tienes la versión más reciente ({APP_VERSION}).")
                    else:
                        self.status_var.set(f"Actualización disponible: {value['version']}")
                        if messagebox.askyesno(APP_TITLE, f"Hay una actualización {value['version']}. ¿Instalarla ahora?"):
                            self.update_button.configure(state="disabled")
                            self.status_var.set("Descargando y verificando el instalador…")
                            threading.Thread(target=self._install_update_worker, args=(value,), daemon=True).start()
                elif kind == "update_error":
                    self.update_button.configure(state="normal")
                    self.status_var.set("No se pudo buscar actualizaciones")
                    messagebox.showinfo(APP_TITLE, value)
                elif kind == "update_installed":
                    self.destroy()
                elif kind == "update_install_error":
                    self.update_button.configure(state="normal")
                    self.status_var.set("No se pudo instalar la actualización")
                    messagebox.showerror(APP_TITLE, value)
                else:
                    self.busy = False
                    self.progress.stop()
                    self.run_button.configure(state="normal")
                    self.status_var.set("Listo" if kind == "done" else "No se pudo terminar")
                    self._set_result(value)
                    if kind == "error":
                        messagebox.showerror(APP_TITLE, value)
        except queue.Empty:
            pass
        self.after(120, self._poll_events)


if __name__ == "__main__":
    TempoFijoApp().mainloop()
