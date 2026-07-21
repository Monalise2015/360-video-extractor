# Video 360° Frame Extractor

Extrae frames equirectangulares de videos 360° (Insta360 X5 y similares) e inyecta coordenadas GPS y heading en el EXIF de cada imagen, listas para fotogrametría, COLMAP, 3D Gaussian Splatting o tours virtuales.

**Versión:** 3.0.0 · **Licencia:** Bureau Veritas · **Plataformas:** Windows / macOS / Linux

---

## Características

- **Extracción FFmpeg** de frames equirectangulares (5.7K, 4K, 2K) a intervalo configurable
- **Inyección GPS en EXIF** (latitud, longitud, altitud, heading) desde archivo GPX **o desde el GPS embebido del INSV** (sin necesidad de GPX externo)
- **Heading preciso por giroscopio** leyendo el trailer INSV del archivo Insta360 X5 (fusión Madgwick AHRS + anclaje GPS continuo)
- **Heading fallback por GPS bearing** suavizado con ventana Gaussiana cuando no hay INSV
- **Extracción rectilínea** opcional: convierte panoramas 360° en N vistas tipo "drone" (configurable yaw + pitch + FOV)
- **GUI** (tkinter) y **CLI** en el mismo binario
- **Multithreaded** EXIF injection
- **Auto-instalación** de dependencias Python al primer arranque
- **Log a archivo** (`<output>/_extractor.log`) con rotación para diagnóstico post-ejecución

---

## Instalación

### Dependencias del sistema

| Componente | Windows | macOS | Linux |
|------------|---------|-------|-------|
| Python 3.8+ | [python.org](https://www.python.org/downloads/) | `brew install python` | `sudo apt install python3 python3-tk` |
| FFmpeg | `winget install ffmpeg` | `brew install ffmpeg` | `sudo apt install ffmpeg` |

### Dependencias Python

```bash
pip install -r requirements.txt
```

(También se instalan automáticamente al primer arranque del script.)

---

## Uso

### Modo GUI (doble click o sin argumentos)

```bash
python video360_frame_extractor.py
```

Selecciona el video MP4, el track GPX y opcionalmente el archivo INSV original (para heading preciso por giroscopio). Configura intervalo, resolución, calidad y pulsa **Procesar**.

### Modo CLI

```bash
# Básico — extrae frame cada 2 segundos, calidad alta, resolución original
python video360_frame_extractor.py recorrido.mp4 track.gpx

# Sin GPX — usa el GPS embebido del INSV como track (y su giroscopio para heading)
python video360_frame_extractor.py recorrido.mp4 --insv recorrido.insv

# Con todos los parámetros
python video360_frame_extractor.py recorrido.mp4 track.gpx \
    --interval 2 \
    --quality 4 \
    --resolution 3840x1920 \
    --threads 8 \
    --start "2025-03-15 10:30:00" \
    --offset 0 \
    --insv recorrido.insv \
    --output ./frames_recorrido
```

### Argumentos CLI

| Flag | Default | Descripción |
|------|---------|-------------|
| `video` | — | Video 360° (`.mp4`, `.mov`, `.insv`) **[obligatorio]** |
| `gpx` | — | Track GPS (`.gpx`) — opcional si se pasa `--insv` con GPS embebido |
| `-i`, `--interval` | `2` | Segundos entre frames (1, 2, 3, 5, 10, 15, 30, 60) |
| `-q`, `--quality` | `4` | Calidad JPEG (qscale: 2=máxima, 15=baja) |
| `-r`, `--resolution` | `original` | `original`, `5760x2880`, `3840x1920`, `2048x1024` |
| `-o`, `--output` | `frames_<video>` | Carpeta de salida |
| `-p`, `--prefix` | `FRAME` | Prefijo de nombre (`FRAME_0001.jpg`) |
| `--start` | auto | Timestamp UTC manual (`YYYY-MM-DD HH:MM:SS`) |
| `--offset` | `0` | Segundos de desfase video↔GPX |
| `--tolerance` | `30` | Segundos máx. fuera del rango GPX antes de marcar "sin GPS" |
| `--threads` | auto | Hilos para inyección EXIF |
| `--no-exif` | off | Solo CSV, sin escribir EXIF |
| `--insv` | none | Archivo `.insv` original: heading por giroscopio + GPS embebido como track si no hay GPX |
| `--rectilinear` | off | Activa extracción de vistas tipo drone |
| `--splits` | `8` | Vistas por frame (8 = cada 45°) |
| `--fov` | `90` | Campo de visión por vista (grados) |
| `--baseline` | `1.0` | Desplazamiento GPS en metros por dirección |
| `--pitch-angles` | `0.0` | Ángulos de elevación CSV (`-30,0,30`) |
| `--cuda` | off | Aceleración por hardware NVIDIA |

---

## Salida

```
frames_recorrido/
├── FRAME_0001.jpg          ← Frame equirectangular con EXIF GPS + heading
├── FRAME_0002.jpg
├── ...
├── _coordinates.csv        ← Manifiesto: index, filename, time, lat, lon, alt, yaw, has_gps
├── _report.txt             ← Reporte legible: configuración + estadísticas
└── _extractor.log          ← Log completo de ejecución (rotativo, 5 MB × 3 backups)
```

---

## Pipeline de heading

El heading (orientación de la cámara) se calcula con la siguiente prioridad:

1. **IMU del INSV** (más preciso, ±1°): si se proporciona el archivo `.insv` original, se lee el trailer del giroscopio (~1000 Hz), se fusiona con AHRS Madgwick y se ancla a norte verdadero usando el GPS embebido.
2. **GPS bearing suavizado** (fallback): si no hay INSV, se calcula bearing entre puntos GPS consecutivos con ventana Gaussiana y promedio circular forward/backward (70/30).

Nota: El INSV de Insta360 X5 contiene tanto el giroscopio como un GPS independiente al GPX externo. El sistema los correlaciona temporalmente.

## Track GPS: GPX vs GPS embebido del INSV

Prioridad de fuente de coordenadas:

1. **GPX externo** (si se proporciona): normalmente más denso y confiable (teléfono/GPS dedicado).
2. **GPS embebido del INSV** (fallback automático): la X5 registra muestras a ~10 Hz pero **repitiendo el último fix real** (~0.3–1 Hz efectivos). El extractor deduplica por timestamp y usa los fixes únicos como track. En modo INSV el inicio del video se ancla al primer fix GPS del propio archivo (el `creation_time` del MP4 exportado suele ser la fecha de export, no de grabación).

Advertencia: con mala visibilidad de cielo (cámara sumergida, cubierta, interiores) la X5 congela el fix — el extractor lo detecta, avisa en el log, y el filtro de tolerancia (`--tolerance`, 30 s por defecto) descarta frames lejos de cualquier fix real en vez de inventar coordenadas interpoladas.

---

## Integración con GeoPhoto360

Este extractor es la primera fase del pipeline `Insta360 → 3D Gaussian Splatting`:

```
Insta360 X5 (captura)
    ↓ MP4 + GPX (+ INSV)
360 Video Extractor          ← este repo
    ↓ Frames JPEG geotaggeados
GeoPhoto360                  ← https://github.com/Monalise2015/GeoPhoto360
    ↓ COLMAP → 3DGS Training → SuperSplat Viewer
```

---

## Diagnóstico

Si el pipeline falla, abre `<output>/_extractor.log` — contiene la traza completa con timestamps, exit codes de FFmpeg y diagnóstico de cada paso.

Casos comunes:
- **"FFmpeg no encontrado"**: instala FFmpeg y agrégalo al PATH.
- **"GPX vacío o sin timestamps"**: el GPX no tiene `<time>` en los trackpoints.
- **"0 frames extraídos"**: el video puede estar corrupto o el codec no soportado; revisa el final del log.
- **"FFmpeg salió con código X"**: revisa las últimas 5 líneas de stderr en el log.

---

## Estructura del proyecto

```
.
├── video360_frame_extractor.py    Script único (GUI + CLI + motor)
├── MasterPrompt_*.md              Especificación técnica completa
├── requirements.txt               Dependencias Python
├── .gitignore
└── README.md
```

---

## Créditos

Parser INSV basado en la especificación del proyecto [Gyroflow](https://gyroflow.xyz/) (Adrian Eddy).
Fusión IMU mediante [imufusion](https://github.com/xioTechnologies/Fusion) (Madgwick AHRS).
