# MASTERPROMPT — Video 360° Frame Extractor con GPS
## Archivo: `video360_frame_extractor.py`
### Versión actual: v2.0 | Última actualización: Marzo 2026

---

## 1. DESCRIPCIÓN GENERAL

Aplicación **Python** que extrae frames equirectangulares de un video 360° y les asigna coordenadas GPS interpoladas desde un track GPX, inyectándolas directamente en el EXIF de cada imagen JPEG resultante.

Diseñada para el flujo de trabajo de **Bureau Veritas — Digitalización e Inspección**, complementando el Tour Virtual 360 BV v4. Las imágenes generadas se cargan directamente en el tour virtual.

**Modos de operación:**
- **GUI (tkinter)** — doble click para abrir interfaz gráfica
- **CLI** — uso por línea de comandos con argumentos

---

## 2. FLUJO DE PROCESAMIENTO

```
VIDEO 360° (.mp4)  ──┐
                      ├──→ FFmpeg extrae frames cada N segundos
TRACK GPS (.gpx)   ──┤     → Interpola GPS por timestamp para cada frame
                      │     → Inyecta lat/lon/alt/heading en EXIF (multithreaded)
                      └──→ Carpeta con JPGs geotaggeados + CSV + reporte
```

### Pipeline detallado:
1. **Auto-install** — Detecta e instala `piexif` y `Pillow` si faltan
2. **Verificación FFmpeg** — Busca en PATH y ubicaciones comunes del OS
3. **Parseo GPX** — Extrae trackpoints con timestamps
4. **Análisis video** — Duración, resolución, codec, fps, creation_time (via ffprobe)
5. **Determinación timestamp inicio** — Manual > video metadata > primer punto GPX
6. **Extracción FFmpeg** — `fps=1/N` genera 1 frame cada N segundos
7. **GPS matching** — Interpolación lineal entre trackpoints por timestamp
8. **Inyección EXIF** — Multithreaded con `ThreadPoolExecutor` + `piexif`
9. **Outputs** — JPGs con GPS en EXIF + `_coordinates.csv` + `_report.txt`

---

## 3. DEPENDENCIAS

### 3.1 Python (auto-instaladas)
```
piexif          — Inyección de GPS en EXIF JPEG
Pillow          — Manipulación de imágenes (soporte futuro)
```

**Auto-install:** Al arrancar, el script detecta módulos faltantes y ejecuta `pip install` automáticamente. Intenta primero sin flags, luego con `--break-system-packages` para pip modernos.

### 3.2 Sistema
```
FFmpeg          — Extracción de frames de video (debe estar en PATH)
ffprobe         — Análisis de metadata del video (viene con FFmpeg)
tkinter         — GUI (incluido en Python estándar, excepto algunas distros Linux)
```

### 3.3 Detección de FFmpeg
```python
# Orden de búsqueda:
1. shutil.which("ffmpeg")               # PATH del sistema
2. Ubicaciones comunes por OS:
   Windows: C:\ffmpeg\bin\, Program Files\, ~\scoop\shims\
   Mac:     /usr/local/bin/, /opt/homebrew/bin/
   Linux:   /usr/bin/, /usr/local/bin/, /snap/bin/
```

### 3.4 Auto-install FFmpeg
Si no encuentra FFmpeg, intenta instalar automáticamente según el OS:
```
Windows: winget install ffmpeg → choco install ffmpeg → scoop install ffmpeg
Mac:     brew install ffmpeg
Linux:   sudo apt install ffmpeg → sudo dnf → sudo pacman
```
Si falla, muestra instrucciones de instalación manual.

---

## 4. ARQUITECTURA DEL CÓDIGO

### 4.1 Módulos y Funciones Core

```
auto_install_packages()      — Instala pip packages faltantes
find_ffmpeg()                — Busca FFmpeg en el sistema
install_ffmpeg_hint()        — Instrucciones de instalación por OS
try_auto_install_ffmpeg()    — Intento automático de instalación

parse_gpx(filepath)          — Parser GPX → [{lat, lon, alt, time, time_ms}]
bearing(lat1, lon1, lat2, lon2) — Calcula heading entre dos puntos
interpolate_gps(timestamp_ms, track) — Interpolación lineal GPS
get_video_info(filepath)     — ffprobe → {duration, width, height, codec, fps, creation_time}

_to_rational(value)          — Float → tupla DMS racional para piexif
inject_gps_exif(filepath, lat, lon, alt, yaw) — Escribe GPS en EXIF (thread-safe)

write_csv(filepath, frames_data) — Genera CSV de coordenadas
format_time(seconds)         — Segundos → "H:MM:SS" o "MM:SS"
```

### 4.2 Clase `FrameExtractor`

Motor principal de procesamiento, usado tanto por GUI como por CLI.

```python
class FrameExtractor:
    def __init__(self, video_path, gpx_path, output_dir,
                 interval=2, quality=4, resolution="original",
                 prefix="FRAME", start_time=None, offset=0,
                 tolerance=30, inject_exif=True, max_workers=None)

    # Callbacks (asignados por GUI o CLI):
    self.on_log = lambda msg, level: None      # "info", "ok", "warn", "err", "step"
    self.on_progress = lambda pct, msg: None   # 0-100
    self.on_done = lambda success, msg: None

    def run(self)      # Ejecutar pipeline completo (llamar desde thread)
    def cancel(self)   # Cancelar procesamiento
```

### 4.3 Entry Point
```python
if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        run_cli()       # Argumentos posicionales → modo CLI
    elif "--cli" in sys.argv:
        run_cli()       # Flag explícito
    else:
        try:
            import tkinter
            run_gui()   # Sin argumentos → GUI
        except ImportError:
            # Muestra instrucciones para instalar tkinter
```

---

## 5. PARÁMETROS DE CONFIGURACIÓN

| Parámetro | CLI Flag | Default | Opciones | Descripción |
|-----------|----------|---------|----------|-------------|
| Intervalo | `-i` `--interval` | 2 | 1,2,3,5,10,15,30,60 | Segundos entre frames |
| Calidad | `-q` `--quality` | 4 | 2=máx, 4=alta, 8=media, 15=baja | qscale de FFmpeg |
| Resolución | `-r` `--resolution` | original | 5760x2880, 3840x1920, 2048x1024 | Escala de salida |
| Output | `-o` `--output` | frames_{video} | ruta | Carpeta de salida |
| Prefijo | `-p` `--prefix` | FRAME | texto | Prefijo de archivos |
| Start time | `--start` | auto | "YYYY-MM-DD HH:MM:SS" | Timestamp inicio video |
| Offset | `--offset` | 0 | float (seg) | Desfase video↔GPX |
| Tolerancia | `--tolerance` | 30 | float (seg) | Máx distancia temporal al GPX |
| Threads | `--threads` | min(CPU, 8) | int | Threads para EXIF |
| No EXIF | `--no-exif` | false | flag | Solo CSV, sin inyectar EXIF |

---

## 6. INTERPOLACIÓN GPS

### 6.1 Algoritmo
```
Para cada frame en posición temporal T:
  1. T_ms = start_time_ms + (frame_index * interval * 1000) + offset_ms
  2. Buscar trackpoints A y B tal que A.time ≤ T_ms ≤ B.time
  3. ratio = (T_ms - A.time) / (B.time - A.time)
  4. lat = A.lat + (B.lat - A.lat) * ratio    (interpolación lineal)
  5. lon = A.lon + (B.lon - A.lon) * ratio
  6. alt = A.alt + (B.alt - A.alt) * ratio
  7. yaw = bearing(A.lat, A.lon, B.lat, B.lon)  (dirección de movimiento)
```

### 6.2 Bearing (heading)
```python
def bearing(lat1, lon1, lat2, lon2):
    # Fórmula de Haversine para bearing inicial
    # Retorna grados 0-360 (Norte = 0, Este = 90)
```

### 6.3 Tolerancia
Si el frame cae fuera del rango temporal del GPX por más de `tolerance` segundos, se marca como "Sin GPS" (coordenadas 0,0).

### 6.4 Sincronización Temporal
Prioridad para determinar el inicio del video:
1. `--start` manual (UTC)
2. `creation_time` del metadata del video (ffprobe)
3. Primer trackpoint del GPX (fallback)

---

## 7. INYECCIÓN EXIF GPS

### 7.1 Campos Escritos
```python
GPS.GPSLatitudeRef       # "N" / "S"
GPS.GPSLatitude          # DMS rational: ((d,1),(m,1),(s,10000))
GPS.GPSLongitudeRef      # "E" / "W"
GPS.GPSLongitude         # DMS rational
GPS.GPSAltitudeRef       # 0 (sobre nivel mar) / 1 (bajo)
GPS.GPSAltitude          # (abs(alt)*100, 100) — rational
GPS.GPSImgDirectionRef   # "T" (true north)
GPS.GPSImgDirection      # (yaw*100, 100) — rational
```

### 7.2 Thread Safety
`inject_gps_exif()` es thread-safe porque cada invocación opera sobre un archivo diferente. No hay estado compartido entre threads.

### 7.3 Multithreading
```python
with ThreadPoolExecutor(max_workers=N) as executor:
    futures = {executor.submit(_inject_one, fd): fd for fd in gps_frames}
    for future in as_completed(futures):
        # Progress callback cada 5 archivos
```

**Performance medido:** ~1000 archivos/segundo con 4 threads.

---

## 8. GUI (TKINTER)

### 8.1 Layout
```
┌─────────────────────────────────────────────┐
│  BV  VIDEO 360° → FRAMES CON GPS      v2.0 │  ← Topbar rojo
├─────────────────────────────────────────────┤
│ ① ARCHIVOS DE ENTRADA                      │
│  [🎬 Seleccionar video]  [🛰 Seleccionar GPX] │
├─────────────────────────────────────────────┤
│ ② CONFIGURACIÓN                             │
│  Intervalo | Calidad | Resolución           │
│  Prefijo | Threads | Carpeta salida         │
│  Inicio video | Offset                      │
├─────────────────────────────────────────────┤
│ ③ PROCESAMIENTO                             │
│  [████████████░░░░░░] 65% FFmpeg: 01:30/03:00│
│  Log con colores (ok/err/warn/info/step)    │
│  [▶ PROCESAR] [✕ Cancelar] [📁 Abrir carpeta]│
├─────────────────────────────────────────────┤
│ FFmpeg: /usr/bin/ffmpeg  piexif: ✓  CPU: 8  │  ← Status bar
└─────────────────────────────────────────────┘
```

### 8.2 Paleta (idéntica al Tour Virtual)
```python
BG = "#0A1628"; BG2 = "#112240"; BG3 = "#1A3254"
FG = "#E8EDF4"; GRAY = "#7A8FA6"; ACCENT = "#3B8FDB"
GREEN = "#22C55E"; ORANGE = "#F59E0B"; RED = "#E3001B"
```

### 8.3 Thread Safety GUI
Los callbacks de `FrameExtractor` usan `root.after(0, lambda: ...)` para actualizar la UI desde el thread de procesamiento al thread principal de tkinter.

### 8.4 Auto-detección
- Al seleccionar GPX → auto-rellena campo "Inicio video" con timestamp del primer trackpoint
- Al seleccionar video → auto-genera nombre de carpeta de salida
- Status bar muestra estado de FFmpeg y piexif al arrancar

---

## 9. CLI

### 9.1 Ejemplos de Uso
```bash
# Básico — cada 2 seg, calidad alta
python video360_frame_extractor.py recorrido.mp4 track.gpx

# Cada 5 seg, calidad máxima, 4K
python video360_frame_extractor.py recorrido.mp4 track.gpx -i 5 -q 2 -r 3840x1920

# Con offset y más threads
python video360_frame_extractor.py recorrido.mp4 track.gpx --offset 3.5 --threads 12

# Prefijo y carpeta personalizados
python video360_frame_extractor.py recorrido.mp4 track.gpx -o ./TK101_frames -p TK101

# Timestamp manual
python video360_frame_extractor.py recorrido.mp4 track.gpx --start "2025-03-15 10:30:00"
```

### 9.2 Salida CLI
```
[██████████████████████████████] 100% Completado
  ✓ COMPLETADO: 150 frames · 142 GPS · 385.2 MB · 02:34
  Carpeta: frames_recorrido
```

---

## 10. ARCHIVOS DE SALIDA

```
frames_{video}/
├── FRAME_0001.jpg        ← Frame equirectangular con GPS en EXIF
├── FRAME_0002.jpg
├── FRAME_0003.jpg
├── ...
├── _coordinates.csv      ← index, filename, time_sec, time_str, lat, lon, alt, yaw, has_gps
└── _report.txt           ← Resumen: config, video info, GPX info, resultados
```

### 10.1 Integración con Tour Virtual
Las imágenes se cargan directamente en `tour_virtual_360_BV_v4.html`:
- **Opción A:** Pestaña "Insta360 X5" → sub-card "Imágenes" → arrastrar JPGs (GPS se lee del EXIF)
- **Opción B:** Pestaña "Register 360" → cargar `_coordinates.csv` + imágenes

---

## 11. DECISIÓN TÉCNICA: ¿POR QUÉ PYTHON Y NO HTML?

| Aspecto | Python (nativo) | HTML (FFmpeg.wasm) |
|---------|----------------|-------------------|
| RAM | Sin límite (disco) | ~2 GB browser |
| Videos grandes | 10+ GB sin problema | Falla con >2 GB |
| H.265/HEVC | Soporte completo | Parcial/no soportado |
| Velocidad | Velocidad nativa del codec | 5-10x más lento |
| Requisito servidor | Ninguno | Necesita localhost (SharedArrayBuffer) |
| Multithreading | ThreadPoolExecutor real | Web Workers limitados |
| Persistencia | Archivos en disco | Pierde todo al cerrar pestaña |

**Conclusión:** Para videos 360° de Insta360 X5 (5.7K, H.265, 5-15 GB), Python es la única opción viable.

---

## 12. POSIBLES MEJORAS FUTURAS

- [ ] **Extracción de GPS interno del .insv** — Leer track GPS embebido en el video Insta360 sin necesitar GPX externo
- [ ] **Preview de frames en GUI** — Mostrar thumbnails de los frames extraídos
- [ ] **Mapa en GUI** — Minimap Leaflet/tkintermapview mostrando el recorrido
- [ ] **Modo por distancia** — Extraer frame cada X metros en vez de cada X segundos
- [ ] **Detección de movimiento** — Saltar frames donde la cámara está quieta
- [ ] **Batch processing** — Procesar múltiples videos+GPX en secuencia
- [ ] **Corrección de heading** — Offset manual de heading para cámaras montadas rotadas
- [ ] **Soporte .insv directo** — Procesar formato nativo Insta360 sin exportar a MP4
- [ ] **Stitching dual-lens** — Para .insv con dos streams (fisheye → equirectangular)
- [ ] **Compresión WebP** — Opción de salida en WebP para menor peso
- [ ] **Progress bar en terminal** — Barra más detallada con ETA
- [ ] **Integración directa con tour** — Botón "Abrir en Tour Virtual" que lance el HTML con las imágenes

---

## 13. INSTRUCCIONES PARA EL ASISTENTE

Cuando el usuario pida modificar o mejorar esta aplicación:

1. **El archivo actual es `video360_frame_extractor.py`** — versión 2.0
2. **Dual mode: GUI (tkinter) + CLI** — ambos usan la misma clase `FrameExtractor`
3. **Auto-install de dependencias** — piexif, Pillow se instalan automáticamente
4. **FFmpeg es requisito externo** — el script lo busca pero no puede instalarlo siempre
5. **Multithreading** — solo para inyección EXIF (archivos independientes, thread-safe)
6. **FFmpeg se ejecuta como subprocess** — no como librería Python
7. **La GUI actualiza UI vía `root.after()`** — nunca tocar widgets desde otro thread
8. **Callbacks pattern** — `on_log`, `on_progress`, `on_done` permiten reusar el motor
9. **Mantener branding BV** — misma paleta de colores que el tour virtual
10. **Probar cambios** verificando que tanto GUI como CLI funcionan correctamente
11. **Las imágenes de salida deben ser compatibles** con el Tour Virtual 360 BV v4

---

*MasterPrompt generado: Marzo 2026*
*Autor: Daniel Andrés Poidevin Piedrahíta — Bureau Veritas Colombia*
*Asistencia: Claude (Anthropic)*
