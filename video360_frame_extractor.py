#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════
  VIDEO 360° → FRAMES CON GPS — Bureau Veritas
  Extractor de frames equirectangulares con coordenadas GPS en EXIF
  v3.0 — Heading preciso por giroscopio INSV + GUI + Multithreading
═══════════════════════════════════════════════════════════════════════

  Doble click para abrir, o desde terminal:
    python video360_frame_extractor.py
    python video360_frame_extractor.py video.mp4 track.gpx [opciones CLI]

  Dependencias (se instalan automáticamente):
    - piexif          (inyección GPS en EXIF)
    - Pillow          (manipulación de imágenes)
    - numpy + imufusion (fusión IMU para heading preciso desde INSV)
    - FFmpeg          (extracción de frames — se detecta o guía instalación)
═══════════════════════════════════════════════════════════════════════
"""

import os
import sys
import csv
import math
import json
import struct
import shutil
import subprocess
import platform
import threading
import time
import argparse
import bisect
import logging
from logging.handlers import RotatingFileHandler
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ═══════════════════════════════════════════════
# LOGGING — File logger del módulo (additivo a callbacks GUI/CLI)
# ═══════════════════════════════════════════════
logger = logging.getLogger("video360_extractor")
logger.setLevel(logging.DEBUG)
logger.propagate = False  # No interferir con loggers raíz si la app se importa

# Mapeo niveles del callback "level" -> logging level
_LEVEL_MAP = {
    "ok":   logging.INFO,
    "info": logging.INFO,
    "step": logging.INFO,
    "warn": logging.WARNING,
    "err":  logging.ERROR,
}


def _attach_file_logger(output_dir):
    """Adjunta un RotatingFileHandler al logger del módulo dentro de output_dir.
    Idempotente: si ya hay handler para esa ruta, no duplica.
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
        log_path = os.path.join(output_dir, "_extractor.log")
        # Evitar duplicar handler para el mismo archivo
        for h in logger.handlers:
            if getattr(h, "baseFilename", None) == os.path.abspath(log_path):
                return log_path
        fh = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(fh)
        return log_path
    except Exception:
        # Si falla el log, no bloquear la ejecución
        return None

# ═══════════════════════════════════════════════
# AUTO-INSTALL DE DEPENDENCIAS
# ═══════════════════════════════════════════════
REQUIRED_PACKAGES = {
    "piexif": "piexif",
    "PIL": "Pillow",
    "numpy": "numpy",
    "imufusion": "imufusion",
}

def auto_install_packages():
    """Instala paquetes pip faltantes automáticamente."""
    missing = []
    for module_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(module_name)
        except ImportError:
            missing.append((module_name, pip_name))

    if not missing:
        return True

    print(f"\n  Instalando dependencias: {', '.join(p for _, p in missing)}…")
    for module_name, pip_name in missing:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pip_name, "--quiet",
                 "--disable-pip-version-check", "--no-warn-script-location"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print(f"  ✓ {pip_name} instalado")
        except subprocess.CalledProcessError:
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", pip_name, "--quiet",
                     "--break-system-packages", "--disable-pip-version-check"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                print(f"  ✓ {pip_name} instalado")
            except Exception as e:
                print(f"  ✗ Error instalando {pip_name}: {e}")
                return False
    return True


def find_ffmpeg():
    """Busca FFmpeg en el sistema. Retorna path o None."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    common_paths = []
    system = platform.system()
    if system == "Windows":
        common_paths = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
            os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe"),
            os.path.expanduser(r"~\scoop\shims\ffmpeg.exe"),
        ]
    elif system == "Darwin":
        common_paths = ["/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]
    else:
        common_paths = ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/snap/bin/ffmpeg"]
    for p in common_paths:
        if os.path.isfile(p):
            return p
    return None


def install_ffmpeg_hint():
    """Retorna instrucciones para instalar FFmpeg según el OS."""
    system = platform.system()
    if system == "Windows":
        return (
            "FFmpeg no encontrado. Opciones:\n"
            "  1. winget install ffmpeg\n"
            "  2. choco install ffmpeg\n"
            "  3. scoop install ffmpeg\n"
            "  4. https://ffmpeg.org/download.html -> agregar bin/ al PATH"
        )
    elif system == "Darwin":
        return "FFmpeg no encontrado. Instalar con:\n  brew install ffmpeg"
    else:
        return (
            "FFmpeg no encontrado. Instalar con:\n"
            "  sudo apt install ffmpeg        (Ubuntu/Debian)\n"
            "  sudo dnf install ffmpeg        (Fedora)\n"
            "  sudo pacman -S ffmpeg          (Arch)"
        )


def try_auto_install_ffmpeg():
    """Intenta instalar FFmpeg automáticamente."""
    system = platform.system()
    print("\n  Intentando instalar FFmpeg automáticamente…")
    commands = []
    if system == "Windows":
        if shutil.which("winget"):
            commands.append(["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--accept-source-agreements"])
        if shutil.which("choco"):
            commands.append(["choco", "install", "ffmpeg", "-y"])
        if shutil.which("scoop"):
            commands.append(["scoop", "install", "ffmpeg"])
    elif system == "Darwin":
        if shutil.which("brew"):
            commands.append(["brew", "install", "ffmpeg"])
    else:
        if shutil.which("apt"):
            commands.append(["sudo", "apt", "install", "-y", "ffmpeg"])
        elif shutil.which("dnf"):
            commands.append(["sudo", "dnf", "install", "-y", "ffmpeg"])
        elif shutil.which("pacman"):
            commands.append(["sudo", "pacman", "-S", "--noconfirm", "ffmpeg"])
    for cmd in commands:
        try:
            print(f"  Ejecutando: {' '.join(cmd)}")
            result = subprocess.run(cmd, timeout=300, capture_output=True, text=True)
            if result.returncode == 0 and find_ffmpeg():
                print("  ✓ FFmpeg instalado correctamente")
                return True
        except Exception as e:
            print(f"  ⚠ Falló: {e}")
    return False


# ═══════════════════════════════════════════════
# Importar después de auto-install
# ═══════════════════════════════════════════════
auto_install_packages()

try:
    import piexif
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import numpy as np
    import imufusion
    HAS_IMU = True
except ImportError:
    HAS_IMU = False


# ═══════════════════════════════════════════════
# INSV PARSER — Extrae giroscopio + GPS del trailer Insta360
# Basado en la especificación del proyecto Gyroflow (Adrian Eddy)
# ═══════════════════════════════════════════════
INSV_MAGIC = b'8db42d694ccc418790edff439fe026bf'

# IDs de registros del trailer INSV
INSV_REC_OFFSETS  = 0
INSV_REC_METADATA = 1
INSV_REC_GYRO     = 3
INSV_REC_EXPOSURE = 4
INSV_REC_GPS      = 7


def parse_insv_trailer(filepath, log_fn=None):
    """Parsea el trailer de un archivo INSV. Retorna dict con offsets de cada registro."""
    if log_fn is None:
        log_fn = lambda msg, lvl: None

    fsize = os.path.getsize(filepath)
    if fsize < 100:
        return None

    with open(filepath, 'rb') as f:
        # Verificar magic (últimos 32 bytes)
        f.seek(-32, 2)
        magic = f.read(32)
        if magic != INSV_MAGIC:
            log_fn("No es un archivo INSV válido (magic no coincide)", "err")
            return None

        # Header: [32 padding][extra_size(4)][version(4)][magic(32)] = 72 bytes al final
        f.seek(-72, 2)
        f.read(32)  # padding
        extra_size = struct.unpack('<I', f.read(4))[0]
        version = struct.unpack('<I', f.read(4))[0]
        extra_start = fsize - extra_size

        log_fn(f"INSV trailer: {extra_size/1024/1024:.1f}MB, versión {version}", "info")

        # Leer registros hacia atrás desde el header
        cursor = fsize - 72
        records = {}

        for _ in range(50):  # max 50 registros
            if cursor <= extra_start + 6:
                break
            f.seek(cursor - 6)
            suffix = f.read(6)
            rec_fmt = suffix[0]
            rec_id = suffix[1]
            rec_size = struct.unpack('<I', suffix[2:6])[0]
            data_start = cursor - 6 - rec_size

            if rec_size > extra_size or data_start < extra_start or rec_size == 0:
                break

            records[rec_id] = {
                'size': rec_size, 'offset': data_start,
                'format': rec_fmt, 'extra_start': extra_start
            }
            cursor = data_start

        # Si encontramos tabla de offsets (ID=0), usarla para registros más profundos
        if INSV_REC_OFFSETS in records and records[INSV_REC_OFFSETS]['size'] > 0:
            off_rec = records[INSV_REC_OFFSETS]
            f.seek(off_rec['offset'])
            off_data = f.read(off_rec['size'])
            num_entries = off_rec['size'] // 10

            for i in range(num_entries):
                entry = off_data[i*10:(i+1)*10]
                eid = entry[0]
                efmt = entry[1]
                esize = struct.unpack('<I', entry[2:6])[0]
                eoffset = struct.unpack('<I', entry[6:10])[0]

                if eid == 0 or esize == 0:
                    continue
                if eid not in records:  # No sobreescribir los ya encontrados
                    records[eid] = {
                        'size': esize, 'offset': extra_start + eoffset,
                        'format': efmt, 'extra_start': extra_start
                    }

        type_names = {0:'Offsets', 1:'Metadata', 2:'Thumbnail', 3:'Gyro',
                      4:'Exposure', 7:'GPS', 9:'AAAData', 11:'AAASimul',
                      12:'ExposureSec', 13:'Magnetic', 14:'Euler',
                      16:'Speed', 18:'Quaternions'}

        for rid, info in sorted(records.items()):
            name = type_names.get(rid, f'Unknown_{rid}')
            log_fn(f"  Registro {rid:>2} ({name}): {info['size']:,} bytes", "info")

    return records


def read_insv_gyro(filepath, records, log_fn=None):
    """Lee datos del giroscopio del INSV. Retorna (timestamps_ms, accel, gyro) como numpy arrays."""
    if log_fn is None:
        log_fn = lambda msg, lvl: None

    if not HAS_IMU:
        log_fn("numpy/imufusion no disponible — no se puede leer IMU", "warn")
        return None

    if INSV_REC_GYRO not in records:
        log_fn("No se encontró registro de giroscopio en el INSV", "warn")
        return None

    rec = records[INSV_REC_GYRO]
    gyro_size = rec['size']

    # Determinar formato: 56 bytes (standard float64) o 20 bytes (raw uint16)
    if gyro_size % 56 == 0:
        sample_size = 56
        is_raw = False
    elif gyro_size % 20 == 0:
        sample_size = 20
        is_raw = True
    else:
        log_fn(f"Tamaño de gyro ({gyro_size}) no es múltiplo de 56 ni 20", "err")
        return None

    n_samples = gyro_size // sample_size
    log_fn(f"Gyro: {n_samples:,} muestras ({sample_size}B {'raw' if is_raw else 'float'})", "info")

    timestamps = np.zeros(n_samples)
    accel = np.zeros((n_samples, 3))
    gyro = np.zeros((n_samples, 3))

    # Escalas por defecto para raw (Insta360 X5)
    gyro_range = 2000   # deg/s
    acc_range = 8       # g (X5 parece usar 8g para obtener ~1g de magnitud)
    gyro_scale = 32768.0 / gyro_range
    acc_scale = 32768.0 / acc_range

    with open(filepath, 'rb') as f:
        f.seek(rec['offset'])
        raw_all = f.read(gyro_size)

    for i in range(n_samples):
        off = i * sample_size
        ts = struct.unpack_from('<Q', raw_all, off)[0]
        timestamps[i] = ts / 1000.0  # microseconds → milliseconds

        if is_raw:
            vals = struct.unpack_from('<6H', raw_all, off + 8)
            accel[i] = [(vals[j] - 32768) / acc_scale for j in range(3)]   # g
            gyro[i] = [(vals[j+3] - 32768) / gyro_scale for j in range(3)]  # deg/s
        else:
            vals = struct.unpack_from('<6d', raw_all, off + 8)
            accel[i] = [vals[0], vals[1], vals[2]]  # g
            gyro[i] = [np.degrees(vals[3]), np.degrees(vals[4]), np.degrees(vals[5])]  # rad/s → deg/s

    # Insta360 X5 orientación IMU: "yzX" → x_out=-y, y_out=-z, z_out=+X
    accel_mapped = np.column_stack([-accel[:,1], -accel[:,2], accel[:,0]])
    gyro_mapped = np.column_stack([-gyro[:,1], -gyro[:,2], gyro[:,0]])

    duration = (timestamps[-1] - timestamps[0]) / 1000.0
    rate = n_samples / duration if duration > 0 else 0
    log_fn(f"Gyro: {duration:.1f}s, {rate:.0f}Hz", "ok")

    return {
        'timestamps_ms': timestamps,
        'accel': accel_mapped,     # g, NWU frame
        'gyro': gyro_mapped,       # deg/s, NWU frame
        'sample_rate': int(round(rate)),
        'n_samples': n_samples
    }


def read_insv_gps(filepath, records, log_fn=None):
    """Lee datos GPS embebidos del INSV. Retorna lista de puntos con track (heading)."""
    if log_fn is None:
        log_fn = lambda msg, lvl: None

    if INSV_REC_GPS not in records:
        log_fn("No se encontró registro GPS en el INSV", "warn")
        return []

    rec = records[INSV_REC_GPS]
    sample_size = 53
    n_samples = rec['size'] // sample_size

    points = []
    with open(filepath, 'rb') as f:
        f.seek(rec['offset'])
        raw_all = f.read(rec['size'])

    for i in range(n_samples):
        off = i * sample_size
        unix_ts = struct.unpack_from('<Q', raw_all, off)[0]
        ms = struct.unpack_from('<H', raw_all, off + 8)[0]
        fix = chr(raw_all[off + 10])
        lat = struct.unpack_from('<d', raw_all, off + 11)[0]
        lat_dir = chr(raw_all[off + 19])
        lon = struct.unpack_from('<d', raw_all, off + 20)[0]
        lon_dir = chr(raw_all[off + 28])
        speed = struct.unpack_from('<d', raw_all, off + 29)[0]
        track = struct.unpack_from('<d', raw_all, off + 37)[0]
        alt = struct.unpack_from('<d', raw_all, off + 45)[0]

        if fix != 'A':
            continue
        if lat_dir == 'S': lat = -lat
        if lon_dir == 'W': lon = -lon
        if speed < 0: speed = 0
        if track < 0: track = 0

        points.append({
            'unix_ts': unix_ts, 'ms': ms,
            'lat': lat, 'lon': lon, 'alt': alt,
            'speed': speed, 'track': track  # track = dirección de movimiento GPS
        })

    log_fn(f"GPS INSV: {len(points)} puntos con fix", "ok")
    return points


def insv_gps_to_track(gps_points, log_fn=None):
    """Convierte el GPS embebido del INSV al mismo formato de track que parse_gpx,
    para usarlo como fuente de coordenadas cuando no hay archivo GPX.

    La X5 registra a ~10Hz REPITIENDO el último fix GPS (~1-3Hz reales), y en
    condiciones sin cielo visible puede congelar el fix por completo → se
    deduplica por timestamp y se ordena cronológicamente.
    """
    if log_fn is None:
        log_fn = lambda msg, lvl: None

    track = []
    last_ms = None
    for p in gps_points:
        t_ms = p["unix_ts"] * 1000 + p.get("ms", 0)
        if t_ms == last_ms:
            continue
        last_ms = t_ms
        track.append({
            "lat": p["lat"], "lon": p["lon"], "alt": p.get("alt", 0.0),
            "time": datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc),
            "time_ms": t_ms,
        })
    track.sort(key=lambda x: x["time_ms"])

    if gps_points:
        log_fn(f"Track INSV: {len(track)} fixes únicos de {len(gps_points):,} muestras", "info")
    if len(track) == 1:
        log_fn("GPS INSV congelado (todas las muestras con el mismo timestamp) — "
               "todos los frames recibirán la misma coordenada", "warn")
    return track


def compute_imu_heading(imu_data, gps_points=None, log_fn=None):
    """
    Calcula heading absoluto por timestamp usando fusión IMU + anclaje GPS.

    El giroscopio da cambios de heading suaves y precisos (1000Hz),
    pero deriva con el tiempo. El GPS track ancla a norte verdadero
    cuando la velocidad es suficiente (>0.5 m/s).

    Retorna dict: {timestamp_ms: heading_degrees} para cada muestra IMU.
    """
    if log_fn is None:
        log_fn = lambda msg, lvl: None

    if not HAS_IMU:
        return None

    timestamps = imu_data['timestamps_ms']
    accel = imu_data['accel']
    gyro_data = imu_data['gyro']
    sample_rate = imu_data['sample_rate']
    n = imu_data['n_samples']

    # === Paso 1: Fusión AHRS (Madgwick) sin magnetómetro ===
    ahrs = imufusion.Ahrs()
    ahrs.settings = imufusion.Settings(
        imufusion.CONVENTION_NWU,
        0.5,              # gain
        2000,             # gyroscope range (deg/s)
        10,               # acceleration rejection (deg)
        10,               # magnetic rejection
        5 * sample_rate   # recovery trigger period
    )

    dt = 1.0 / sample_rate
    relative_yaw = np.zeros(n)

    for i in range(n):
        ahrs.update_no_magnetometer(gyro_data[i], accel[i], dt)
        euler = ahrs.quaternion.to_euler()
        relative_yaw[i] = euler[2]  # yaw en grados (relativo, deriva)

    log_fn(f"IMU fusión: yaw relativo [{relative_yaw.min():.1f}°, {relative_yaw.max():.1f}°]", "info")

    # === Paso 2: Anclar a GPS track cuando hay velocidad suficiente ===
    # Si no hay GPS o track, retornar yaw relativo sin anclar
    if not gps_points:
        log_fn("Sin GPS para anclar — heading será relativo", "warn")
        heading_map = {}
        for i in range(0, n, max(1, sample_rate // 10)):  # submuestrear a ~10Hz
            heading_map[timestamps[i]] = relative_yaw[i] % 360
        return heading_map

    # Encontrar puntos GPS con velocidad suficiente para anclar
    # GPS track = dirección de movimiento, necesitamos correlacionar con yaw IMU
    MIN_SPEED = 0.3  # m/s mínimo para confiar en GPS track

    # Calcular offset IMU→True North usando GPS track
    # offset = GPS_track - IMU_yaw  (en momentos de alta velocidad)
    video_start_ms = timestamps[0]
    offsets = []

    for gp in gps_points:
        if gp['speed'] < MIN_SPEED or gp['track'] <= 0:
            continue

        # GPS timestamp → ms relativos al video
        gps_ts_ms = gp['unix_ts'] * 1000.0 + gp['ms']

        # Buscar muestra IMU más cercana al timestamp GPS
        # Nota: timestamps IMU son relativos al video, GPS son unix epoch
        # Necesitamos alinear los dos sistemas de tiempo
        # Por ahora usamos el offset temporal entre primer GPS y primer IMU
        # (se refinará cuando se conozca la sincronización)

    # Método alternativo: calcular bearing GPS y correlacionar con yaw IMU
    # usando la dirección de cambio del yaw
    if len(gps_points) >= 2:
        # Encontrar offset constante entre yaw IMU y dirección geográfica
        # Usar los primeros puntos GPS donde hay movimiento
        gps_bearings = []
        for i in range(1, len(gps_points)):
            p0, p1 = gps_points[i-1], gps_points[i]
            dist = haversine(p0['lat'], p0['lon'], p1['lat'], p1['lon'])
            if dist > 1.0:  # Al menos 1m entre puntos
                brg = bearing(p0['lat'], p0['lon'], p1['lat'], p1['lon'])
                gps_bearings.append({
                    'bearing': brg,
                    'ts_ms': (p0['unix_ts'] * 1000.0 + p0['ms'] + p1['unix_ts'] * 1000.0 + p1['ms']) / 2,
                    'speed': (p0['speed'] + p1['speed']) / 2
                })

        if gps_bearings:
            # Estimar offset temporal: asumir que el video empieza cuando el GPS empieza
            # GPS times son unix epoch ms, IMU times son ms desde inicio del video
            # El primer GPS point con movimiento nos da la referencia
            first_moving_gps = gps_bearings[0]
            gps_epoch_start = gps_points[0]['unix_ts'] * 1000.0 + gps_points[0]['ms']

            # Calcular offset norte para cada punto GPS con movimiento
            north_offsets = []
            for gb in gps_bearings:
                gps_ms_from_start = gb['ts_ms'] - gps_epoch_start
                imu_ms = video_start_ms + gps_ms_from_start

                # Encontrar yaw IMU más cercano
                idx = np.searchsorted(timestamps, imu_ms)
                idx = min(max(idx, 0), n - 1)

                imu_yaw = relative_yaw[idx]
                gps_brg = gb['bearing']

                # offset = GPS_bearing - IMU_yaw (lo que hay que sumar al IMU para obtener norte)
                off = gps_brg - imu_yaw
                # Normalizar a [-180, 180]
                off = (off + 180) % 360 - 180
                north_offsets.append(off)

            if north_offsets:
                # === v3.1: Anclaje GPS CONTINUO (ventana deslizante) ===
                # En vez de un offset medio único (que no compensa drift del IMU),
                # usamos una secuencia temporal de offsets GPS->Norte y los
                # interpolamos para cada timestamp IMU. Así el drift gyro se
                # cancela tramo a tramo.

                # Timeline de offsets: [(ts_ms, offset, peso)]
                # ts_ms está en la escala de timestamps IMU (ms desde inicio video)
                offsets_timeline = []
                for k, gb in enumerate(gps_bearings):
                    gps_ms_from_start = gb['ts_ms'] - gps_epoch_start
                    imu_ts = video_start_ms + gps_ms_from_start
                    # Peso por velocidad: GPS lento tiene bearing ruidoso
                    w = max(0.2, min(1.0, gb['speed'] / 2.0))
                    offsets_timeline.append((imu_ts, north_offsets[k], w))

                offsets_timeline.sort(key=lambda x: x[0])

                # Filtro suavizante: para cada offset, lo reemplazamos por el
                # promedio circular ponderado con sus vecinos (ventana ±5).
                # Saca picos de GPS ruidoso en curvas bruscas.
                smoothed = []
                W = 5
                for k in range(len(offsets_timeline)):
                    lo = max(0, k - W)
                    hi = min(len(offsets_timeline), k + W + 1)
                    sin_s = 0.0; cos_s = 0.0; w_s = 0.0
                    for j in range(lo, hi):
                        ts_j, off_j, w_j = offsets_timeline[j]
                        sin_s += math.sin(math.radians(off_j)) * w_j
                        cos_s += math.cos(math.radians(off_j)) * w_j
                        w_s += w_j
                    if w_s > 0:
                        smoothed_off = math.degrees(math.atan2(sin_s / w_s, cos_s / w_s))
                        smoothed.append((offsets_timeline[k][0], smoothed_off))

                # Promedio circular global para fallback/extrapolación
                sin_sum = sum(math.sin(math.radians(o)) for o in north_offsets)
                cos_sum = sum(math.cos(math.radians(o)) for o in north_offsets)
                mean_offset = math.degrees(math.atan2(sin_sum, cos_sum))

                if smoothed:
                    diffs = [abs(((s[1] - mean_offset + 180) % 360) - 180) for s in smoothed]
                    spread = max(diffs) if diffs else 0
                    log_fn(
                        f"Offset IMU->Norte: medio {mean_offset:.1f}° | "
                        f"ventana deslizante: {len(smoothed)} anclas, "
                        f"variación {spread:.1f}° (drift compensado)",
                        "ok"
                    )
                else:
                    log_fn(f"Offset IMU->Norte: {mean_offset:.1f} deg (de {len(north_offsets)} puntos GPS)", "ok")

                def offset_at(imu_ts):
                    """Interpolación circular (shortest-arc) del offset en imu_ts."""
                    if not smoothed:
                        return mean_offset
                    # Fuera de rango → extrapolar con el extremo más cercano
                    if imu_ts <= smoothed[0][0]:
                        return smoothed[0][1]
                    if imu_ts >= smoothed[-1][0]:
                        return smoothed[-1][1]
                    # Búsqueda binaria
                    lo, hi = 0, len(smoothed) - 1
                    while lo + 1 < hi:
                        mid = (lo + hi) // 2
                        if smoothed[mid][0] <= imu_ts:
                            lo = mid
                        else:
                            hi = mid
                    t0, o0 = smoothed[lo]
                    t1, o1 = smoothed[hi]
                    if t1 == t0:
                        return o0
                    frac = (imu_ts - t0) / (t1 - t0)
                    # Interp circular shortest-arc
                    diff = ((o1 - o0 + 180) % 360) - 180
                    return o0 + frac * diff

                # Aplicar offset continuo y generar mapa de headings
                heading_map = {}
                for i in range(0, n, max(1, sample_rate // 10)):  # ~10Hz output
                    off_i = offset_at(timestamps[i])
                    absolute_heading = (relative_yaw[i] + off_i) % 360
                    heading_map[timestamps[i]] = round(absolute_heading, 1)

                return heading_map

    # Fallback: retornar yaw relativo
    log_fn("No se pudo anclar a GPS — heading relativo", "warn")
    heading_map = {}
    for i in range(0, n, max(1, sample_rate // 10)):
        heading_map[timestamps[i]] = round(relative_yaw[i] % 360, 1)
    return heading_map


def get_heading_at_time(heading_map, video_time_ms):
    """Obtiene el heading más cercano para un timestamp de video dado."""
    if not heading_map:
        return None
    keys = sorted(heading_map.keys())
    # Búsqueda binaria para el timestamp más cercano (bisect importado a nivel módulo)
    idx = bisect.bisect_left(keys, video_time_ms)
    if idx == 0:
        return heading_map[keys[0]]
    if idx >= len(keys):
        return heading_map[keys[-1]]
    # Elegir el más cercano
    before = keys[idx - 1]
    after = keys[idx]
    if (video_time_ms - before) <= (after - video_time_ms):
        return heading_map[before]
    return heading_map[after]


# ═══════════════════════════════════════════════
# CORE FUNCTIONS
# ═══════════════════════════════════════════════
VERSION = "3.0.0"

def format_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_gpx(filepath):
    """Parsea GPX, retorna lista de trackpoints con timestamps."""
    tree = ET.parse(filepath)
    root = tree.getroot()
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    points = []
    for tag in ["trkpt", "wpt", "rtept"]:
        for pt in root.iter(f"{ns}{tag}"):
            lat = float(pt.get("lat", 0))
            lon = float(pt.get("lon", 0))
            ele_el = pt.find(f"{ns}ele")
            alt = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
            time_el = pt.find(f"{ns}time")
            if time_el is not None and time_el.text:
                ts = time_el.text.strip()
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    for fmt in ["%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"]:
                        try:
                            dt = datetime.strptime(ts, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        continue
                points.append({"lat": lat, "lon": lon, "alt": alt,
                               "time": dt, "time_ms": int(dt.timestamp() * 1000)})
    points.sort(key=lambda p: p["time_ms"])
    return points


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def haversine(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos puntos GPS (Haversine)."""
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def gpx_video_overlap(video_start_ms, video_end_ms, track):
    """Solapamiento temporal entre la ventana del video y el track GPX.
    Retorna (segundos_solapados, fracción_del_video_cubierta).
    Detecta el caso típico: creation_time del MP4 = fecha de EXPORT
    (Insta360 Studio), no de grabación → 0 frames con GPS silenciosos.
    """
    if not track or video_end_ms <= video_start_ms:
        return 0.0, 0.0
    overlap_ms = min(video_end_ms, track[-1]["time_ms"]) - max(video_start_ms, track[0]["time_ms"])
    if overlap_ms <= 0:
        return 0.0, 0.0
    return overlap_ms / 1000.0, overlap_ms / (video_end_ms - video_start_ms)


def smooth_bearings(frames_data, window=5):
    """
    Recalcula bearings suavizados usando ventana Gaussiana sobre coordenadas.
    Resuelve el problema de desalineación en giros bruscos.

    Etapa 1: Suaviza coordenadas GPS con media móvil Gaussiana
    Etapa 2: Calcula bearings forward+backward promediados desde coords suavizadas
    """
    n = len(frames_data)
    if n < 2:
        return frames_data

    # Filtrar frames con GPS válido
    gps_indices = [i for i in range(n) if frames_data[i]['has_gps'] and frames_data[i]['lat'] != 0]
    if len(gps_indices) < 2:
        return frames_data

    # --- Etapa 1: Suavizar coordenadas con Gaussiana ---
    w = window if window % 2 == 1 else window + 1
    half = w // 2
    sigma = half / 1.5
    raw_weights = [math.exp(-(i ** 2) / (2 * sigma ** 2)) for i in range(-half, half + 1)]
    w_sum = sum(raw_weights)
    norm_weights = [wt / w_sum for wt in raw_weights]

    lats = [frames_data[i]['lat'] for i in range(n)]
    lons = [frames_data[i]['lon'] for i in range(n)]
    smooth_lats = list(lats)
    smooth_lons = list(lons)

    for i in gps_indices:
        if i == gps_indices[0] or i == gps_indices[-1]:
            continue  # Preservar extremos
        lat_acc = lon_acc = total_w = 0
        for j in range(-half, half + 1):
            idx = i + j
            # Reflejo en bordes
            if idx < 0:
                idx = -idx
            if idx >= n:
                idx = 2 * (n - 1) - idx
            if idx < 0 or idx >= n or not frames_data[idx]['has_gps'] or frames_data[idx]['lat'] == 0:
                continue
            wt = norm_weights[j + half]
            lat_acc += lats[idx] * wt
            lon_acc += lons[idx] * wt
            total_w += wt
        if total_w > 0:
            smooth_lats[i] = lat_acc / total_w
            smooth_lons[i] = lon_acc / total_w

    # --- Etapa 2: Calcular bearings desde coords suavizadas ---
    # Buscar referencia forward/backward con distancia minima de 1m.
    # IMPORTANTE: solo entre frames CON GPS — un fallback a un frame sin GPS
    # (lat=0, lon=0) daría un bearing absurdo hacia el golfo de Guinea.
    def find_fwd_ref(k):
        i = gps_indices[k]
        for j in gps_indices[k + 1:]:
            if haversine(smooth_lats[i], smooth_lons[i], smooth_lats[j], smooth_lons[j]) >= 1.0:
                return j
        return gps_indices[k + 1] if k + 1 < len(gps_indices) else i

    def find_bwd_ref(k):
        i = gps_indices[k]
        for j in reversed(gps_indices[:k]):
            if haversine(smooth_lats[i], smooth_lons[i], smooth_lats[j], smooth_lons[j]) >= 1.0:
                return j
        return gps_indices[k - 1] if k > 0 else i

    for k, i in enumerate(gps_indices):
        if k == 0:
            # Primer frame: solo bearing forward
            ref = find_fwd_ref(k)
            new_yaw = bearing(smooth_lats[i], smooth_lons[i], smooth_lats[ref], smooth_lons[ref])
        elif k == len(gps_indices) - 1:
            # Último frame: solo bearing backward
            ref = find_bwd_ref(k)
            new_yaw = bearing(smooth_lats[ref], smooth_lons[ref], smooth_lats[i], smooth_lons[i])
        else:
            # Intermedio: promedio circular forward + backward
            prev = find_bwd_ref(k)
            nxt = find_fwd_ref(k)
            b1 = bearing(smooth_lats[prev], smooth_lons[prev], smooth_lats[i], smooth_lons[i])
            b2 = bearing(smooth_lats[i], smooth_lons[i], smooth_lats[nxt], smooth_lons[nxt])
            # Promedio circular ponderado: 70% forward, 30% backward
            # La cámara apunta hacia donde VAS, no de donde vienes
            w_bwd, w_fwd = 0.3, 0.7
            sin_avg = w_bwd * math.sin(math.radians(b1)) + w_fwd * math.sin(math.radians(b2))
            cos_avg = w_bwd * math.cos(math.radians(b1)) + w_fwd * math.cos(math.radians(b2))
            new_yaw = (math.degrees(math.atan2(sin_avg, cos_avg)) + 360) % 360

        frames_data[i]['yaw'] = round(new_yaw, 1)
        frames_data[i]['has_heading'] = True

    return frames_data


def interpolate_gps(timestamp_ms, track):
    """Interpola posición GPS en `timestamp_ms`. Búsqueda binaria O(log n)."""
    if not track:
        return None
    if timestamp_ms <= track[0]["time_ms"]:
        p = track[0]
        # yaw=None: fuera del track no hay dirección de movimiento conocida
        return {"lat": p["lat"], "lon": p["lon"], "alt": p["alt"], "yaw": None, "ok": True,
                "dist_ms": track[0]["time_ms"] - timestamp_ms}
    if timestamp_ms >= track[-1]["time_ms"]:
        p = track[-1]
        return {"lat": p["lat"], "lon": p["lon"], "alt": p["alt"], "yaw": None, "ok": True,
                "dist_ms": timestamp_ms - track[-1]["time_ms"]}
    # Búsqueda binaria: encontrar el par (lo, hi=lo+1) tal que lo es el último índice
    # con track[lo]["time_ms"] < timestamp_ms. Si timestamp_ms coincide exacto con un
    # punto track[k], devuelve par (k-1, k) — equivalente al recorrido lineal previo,
    # de modo que bearing(a, b) sea idéntico al comportamiento O(n) original.
    lo, hi = 0, len(track) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if track[mid]["time_ms"] < timestamp_ms:
            lo = mid
        else:
            hi = mid
    a, b = track[lo], track[hi]
    dt = b["time_ms"] - a["time_ms"]
    ratio = (timestamp_ms - a["time_ms"]) / dt if dt else 0
    lat = a["lat"] + (b["lat"] - a["lat"]) * ratio
    lon = a["lon"] + (b["lon"] - a["lon"]) * ratio
    alt = a["alt"] + (b["alt"] - a["alt"]) * ratio
    yaw = bearing(a["lat"], a["lon"], b["lat"], b["lon"])
    # dist_ms = distancia temporal al fix real más cercano. En tracks con huecos
    # grandes (GPS INSV congelado) evita aceptar coordenadas interpoladas
    # inventadas: el filtro de tolerancia del pipeline las descarta.
    dist_ms = min(timestamp_ms - a["time_ms"], b["time_ms"] - timestamp_ms)
    return {"lat": round(lat, 7), "lon": round(lon, 7),
            "alt": round(alt, 1), "yaw": round(yaw, 1), "ok": True, "dist_ms": dist_ms}


def shift_coordinate(lat, lon, brng_degrees, distance_meters):
    """Calcula la nueva coordenada (lat, lon) desplazada una distancia en un rumbo específico (Haversine)."""
    if distance_meters == 0:
        return lat, lon
    R = 6378137.0  # Radio de la Tierra en metros
    brng = math.radians(brng_degrees)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    lat2 = math.asin(math.sin(lat1) * math.cos(distance_meters / R) +
                     math.cos(lat1) * math.sin(distance_meters / R) * math.cos(brng))
    lon2 = lon1 + math.atan2(math.sin(brng) * math.sin(distance_meters / R) * math.cos(lat1),
                             math.cos(distance_meters / R) - math.sin(lat1) * math.sin(lat2))
    
    return round(math.degrees(lat2), 7), round(math.degrees(lon2), 7)


def run_split_cmd(cmd, out_path):
    """Ejecuta un comando FFmpeg de split y verifica que produjo la salida.
    Retorna (ok, stderr). Detecta builds de FFmpeg sin filtro v360, fallos
    de CUDA, etc., que antes fallaban en silencio.
    """
    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            return False, stderr or f"FFmpeg salió con código {result.returncode}"
        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            return False, stderr or "FFmpeg no produjo el archivo de salida"
        return True, stderr
    except Exception as e:
        return False, str(e)


def get_video_info(filepath):
    probe = shutil.which("ffprobe")
    info = {"duration": 0, "width": 0, "height": 0, "codec": "unknown", "fps": 0, "creation_time": None}
    if not probe:
        return info
    try:
        cmd = [probe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(filepath)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        if "format" in data:
            info["duration"] = float(data["format"].get("duration", 0))
            tags = data["format"].get("tags", {})
            ct = tags.get("creation_time", tags.get("com.apple.quicktime.creationdate", ""))
            if ct:
                try:
                    info["creation_time"] = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                except Exception:
                    pass
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = int(stream.get("width", 0))
                info["height"] = int(stream.get("height", 0))
                info["codec"] = stream.get("codec_name", "unknown")
                r = stream.get("r_frame_rate", "0/1")
                if "/" in r:
                    n, d = r.split("/")
                    info["fps"] = round(int(n) / max(int(d), 1), 2)
                break
    except Exception:
        pass
    return info


def _to_rational(value):
    d = int(abs(value))
    m_float = (abs(value) - d) * 60
    m = int(m_float)
    s_float = (m_float - m) * 60
    s = int(s_float * 10000)
    return ((d, 1), (m, 1), (s, 10000))


def inject_gps_exif(filepath, lat, lon, alt=None, yaw=None):
    """Inyecta GPS en EXIF. Thread-safe.
    Errores se registran en el log de archivo (DEBUG) sin saturar la GUI.
    """
    if not HAS_PIEXIF:
        return False
    try:
        try:
            exif_dict = piexif.load(filepath)
        except Exception as e:
            logger.debug("piexif.load falló para %s (%s) — usando dict vacío", filepath, e)
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
        gps = exif_dict.get("GPS", {})
        gps[piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
        gps[piexif.GPSIFD.GPSLatitude] = _to_rational(lat)
        gps[piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
        gps[piexif.GPSIFD.GPSLongitude] = _to_rational(lon)
        if alt is not None:
            gps[piexif.GPSIFD.GPSAltitudeRef] = 0 if alt >= 0 else 1
            gps[piexif.GPSIFD.GPSAltitude] = (int(abs(alt) * 100), 100)
        # yaw=None significa "heading desconocido"; 0.0 es norte y SÍ se escribe
        if yaw is not None:
            gps[piexif.GPSIFD.GPSImgDirectionRef] = b"T"
            gps[piexif.GPSIFD.GPSImgDirection] = (int(yaw * 100), 100)
        exif_dict["GPS"] = gps
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, filepath)
        return True
    except Exception as e:
        logger.debug("inject_gps_exif falló para %s: %s", filepath, e)
        return False


def write_csv(filepath, frames_data):
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["index", "filename", "time_sec", "time_str", "lat", "lon", "alt", "yaw", "has_gps"])
        for fd in frames_data:
            writer.writerow([fd["index"], fd["filename"], fd["time_sec"], fd["time_str"],
                             fd["lat"], fd["lon"], fd["alt"], fd["yaw"], 1 if fd["has_gps"] else 0])


# ═══════════════════════════════════════════════
# PROCESSING ENGINE
# ═══════════════════════════════════════════════
class FrameExtractor:
    """Motor de extracción con callbacks para progreso. Usado por GUI y CLI."""

    def __init__(self, video_path, gpx_path, output_dir, interval=2, quality=4,
                 resolution="original", prefix="FRAME", start_time=None,
                 offset=0, tolerance=30, inject_exif=True, max_workers=None,
                 rectilinear=False, splits=8, fov=90, baseline=1.0,
                 cuda=False, pitch_angles="0.0", insv_path=None):
        self.video_path = video_path
        self.gpx_path = gpx_path
        self.insv_path = insv_path  # Archivo INSV para datos de giroscopio
        self.output_dir = output_dir
        self.interval = interval
        self.quality = quality
        self.resolution = resolution
        self.prefix = prefix
        self.start_time = start_time
        self.offset = offset
        self.tolerance = tolerance
        self.inject_exif = inject_exif
        self.max_workers = max_workers or min(os.cpu_count() or 4, 8)
        self.rectilinear = rectilinear
        self.splits = splits
        self.fov = fov
        self.baseline = baseline
        self.cuda = cuda
        if isinstance(pitch_angles, str):
            try:
                self.pitch_angles = [float(p.strip()) for p in pitch_angles.replace("°","").split(",") if p.strip()]
            except ValueError:
                self.pitch_angles = [0.0]
        else:
            self.pitch_angles = list(pitch_angles)
        if not self.pitch_angles:
            self.pitch_angles = [0.0]
        self.ffmpeg_path = find_ffmpeg()
        self.gpx_track = []
        self.track_source = None  # "gpx" | "insv"
        self.video_info = {}
        self.frames_data = []
        self.heading_map = None  # Heading IMU del INSV (si disponible)
        self.cancelled = False
        self.on_log = lambda msg, level: None
        self.on_progress = lambda pct, msg: None
        self.on_done = lambda success, msg: None

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            self._run_pipeline()
        except Exception as e:
            self.on_done(False, f"Error fatal: {e}")

    def _run_pipeline(self):
        t0 = time.time()

        # ── Setup file logger (escribe en <output_dir>/_extractor.log) ──
        # Hook el callback on_log para que cada mensaje vaya tanto a GUI/CLI como al archivo.
        log_path = _attach_file_logger(self.output_dir)
        _user_on_log = self.on_log

        def _logged_on_log(msg, level="info"):
            try:
                logger.log(_LEVEL_MAP.get(level, logging.INFO), msg)
            except Exception:
                pass
            _user_on_log(msg, level)

        self.on_log = _logged_on_log

        if log_path:
            self.on_log(f"Log: {log_path}", "info")
        self.on_log("=" * 55, "step")
        self.on_log(f"Video 360 Extractor v{VERSION} — Pipeline iniciado", "step")
        self.on_log(f"  Video:  {self.video_path}", "info")
        self.on_log(f"  GPX:    {self.gpx_path or '(ninguno — se usará el GPS del INSV)'}", "info")
        self.on_log(f"  Output: {self.output_dir}", "info")
        if self.insv_path:
            self.on_log(f"  INSV:   {self.insv_path}", "info")

        # ── FFmpeg ──
        if not self.ffmpeg_path:
            self.on_log("FFmpeg no encontrado", "err")
            self.on_log(install_ffmpeg_hint(), "warn")
            self.on_done(False, "FFmpeg no encontrado")
            return
        self.on_log(f"FFmpeg: {self.ffmpeg_path}", "ok")
        self.on_log(f"Threads EXIF: {self.max_workers}", "info")

        # ── INSV: giroscopio + GPS embebido (opcional) ──
        insv_gps = []
        if self.insv_path and os.path.isfile(self.insv_path):
            self.on_progress(2, "Leyendo INSV…")
            self.on_log("=== INSV (GIROSCOPIO + GPS) ===", "step")
            try:
                insv_records = parse_insv_trailer(self.insv_path, self.on_log)
                if insv_records:
                    imu_data = read_insv_gyro(self.insv_path, insv_records, self.on_log)
                    insv_gps = read_insv_gps(self.insv_path, insv_records, self.on_log)
                    if imu_data:
                        self.on_log("Calculando heading por fusión IMU+GPS…", "info")
                        self.heading_map = compute_imu_heading(imu_data, insv_gps, self.on_log)
                        if self.heading_map:
                            self.on_log(f"Heading IMU: {len(self.heading_map):,} puntos calculados ✓", "ok")
                        else:
                            self.on_log("No se pudo calcular heading IMU", "warn")
            except Exception as e:
                self.on_log(f"Error leyendo INSV: {e}", "warn")
                self.heading_map = None
                insv_gps = []
        if self.cancelled: return

        # ── Track GPS: GPX o, en su defecto, GPS embebido del INSV ──
        self.on_progress(3, "Preparando track GPS…")
        if self.gpx_path:
            self.track_source = "gpx"
            self.on_log("Parseando GPX…", "step")
            self.gpx_track = parse_gpx(self.gpx_path)
            if not self.gpx_track:
                self.on_done(False, "GPX vacío o sin timestamps")
                return
        elif insv_gps:
            self.track_source = "insv"
            self.on_log("Sin GPX — usando el GPS embebido del INSV como track", "step")
            self.gpx_track = insv_gps_to_track(insv_gps, self.on_log)
            if not self.gpx_track:
                self.on_done(False, "El GPS embebido del INSV no tiene fixes válidos")
                return
        else:
            self.on_done(False, "Sin track GPS: proporciona un GPX o un INSV con GPS embebido")
            return
        src_lbl = "GPX" if self.track_source == "gpx" else "GPS INSV"
        gpx_dur = (self.gpx_track[-1]["time_ms"] - self.gpx_track[0]["time_ms"]) / 1000
        self.on_log(f"{src_lbl}: {len(self.gpx_track)} pts en {format_time(gpx_dur)}", "ok")
        self.on_log(f"  {self.gpx_track[0]['time'].strftime('%Y-%m-%d %H:%M:%S')} -> {self.gpx_track[-1]['time'].strftime('%H:%M:%S')}", "info")
        if self.cancelled: return

        # ── Video ──
        self.on_progress(5, "Analizando video…")
        self.video_info = get_video_info(self.video_path)
        vi = self.video_info
        if vi["duration"]:
            self.on_log(f"Video: {format_time(vi['duration'])} — {vi['width']}×{vi['height']} — {vi['codec']} — {vi['fps']}fps", "ok")

        # ── Start time ──
        if self.start_time:
            start_ms = int(self.start_time.timestamp() * 1000)
            self.on_log(f"Inicio (manual): {self.start_time.isoformat()}", "info")
        elif self.track_source == "insv":
            # Track y video provienen de la MISMA grabación INSV: el primer fix
            # GPS es la mejor referencia del inicio (el creation_time del MP4
            # exportado suele ser la fecha de export, no de grabación).
            start_ms = self.gpx_track[0]["time_ms"]
            self.on_log(f"Inicio (primer fix GPS del INSV): {self.gpx_track[0]['time'].isoformat()}", "ok")
        elif vi.get("creation_time"):
            start_ms = int(vi["creation_time"].timestamp() * 1000)
            self.on_log(f"Inicio (video): {vi['creation_time'].isoformat()}", "ok")
        else:
            start_ms = self.gpx_track[0]["time_ms"]
            self.on_log(f"Inicio (GPX): {self.gpx_track[0]['time'].isoformat()}", "warn")
        start_ms += int(self.offset * 1000)
        if self.offset:
            self.on_log(f"Offset: {self.offset:+.1f}s", "info")

        # ── Sanidad: ¿el video y el GPX se solapan en el tiempo? ──
        if vi["duration"]:
            video_end_ms = start_ms + int(vi["duration"] * 1000)
            ov_secs, ov_ratio = gpx_video_overlap(start_ms, video_end_ms, self.gpx_track)
            if ov_ratio <= 0:
                v0 = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
                v1 = datetime.fromtimestamp(video_end_ms / 1000, tz=timezone.utc)
                self.on_log("⚠ EL VIDEO Y EL GPX NO SE SOLAPAN EN EL TIEMPO", "err")
                self.on_log(f"  Video: {v0.strftime('%Y-%m-%d %H:%M:%S')} → {v1.strftime('%H:%M:%S')} UTC", "warn")
                self.on_log(f"  GPX:   {self.gpx_track[0]['time'].strftime('%Y-%m-%d %H:%M:%S')} → "
                            f"{self.gpx_track[-1]['time'].strftime('%H:%M:%S')}", "warn")
                self.on_log("  Causa típica: el creation_time del MP4 es la fecha de EXPORT "
                            "(Insta360 Studio), no de grabación.", "warn")
                self.on_log("  Solución: fija 'Inicio video' (GUI) o --start/--offset (CLI). "
                            "Ningún frame recibirá GPS así.", "warn")
            elif ov_ratio < 0.5:
                self.on_log(f"⚠ Solo {ov_ratio*100:.0f}% del video se solapa con el GPX "
                            f"({ov_secs:.0f}s) — muchos frames quedarán sin GPS", "warn")

        if vi["duration"]:
            est = int(vi["duration"] / self.interval)
            self.on_log(f"Estimación: ~{est} frames", "info")
        if self.cancelled: return

        # ── FFmpeg extraction ──
        self.on_progress(8, "Extrayendo frames…")
        self.on_log("=== EXTRACCION FFmpeg ===", "step")
        os.makedirs(self.output_dir, exist_ok=True)
        pattern = os.path.join(self.output_dir, f"{self.prefix}_%04d.jpg")

        args = [self.ffmpeg_path, "-hide_banner", "-y", "-i", str(self.video_path)]
        vf = f"fps=1/{self.interval}"
        if self.resolution and self.resolution != "original":
            vf += f",scale={self.resolution.replace('x', ':')}"
        args += ["-vf", vf, "-q:v", str(self.quality), pattern]
        self.on_log(f"CMD: {' '.join(args)}", "info")

        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            stderr_tail = []  # Últimas líneas para diagnóstico si FFmpeg falla
            for line in process.stderr:
                if self.cancelled:
                    process.kill()
                    self.on_done(False, "Cancelado")
                    return
                line = line.strip()
                # Mantener cola de últimas 20 líneas de stderr para diagnóstico
                stderr_tail.append(line)
                if len(stderr_tail) > 20:
                    stderr_tail.pop(0)
                if "time=" in line and vi["duration"] > 0:
                    try:
                        t_str = line.split("time=")[1].split()[0]
                        parts = t_str.split(":")
                        secs = float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                        pct = 8 + int(secs / vi["duration"] * 55)
                        self.on_progress(min(pct, 63), f"FFmpeg: {format_time(secs)} / {format_time(vi['duration'])}")
                    except Exception:
                        pass
            process.wait()
            if process.returncode != 0:
                self.on_log(f"FFmpeg salió con código {process.returncode}", "warn")
                if stderr_tail:
                    self.on_log("Últimas líneas de FFmpeg stderr:", "warn")
                    for line in stderr_tail[-5:]:
                        if line:
                            self.on_log(f"  {line}", "warn")
        except Exception as e:
            self.on_log(f"Error FFmpeg: {e}", "err")
            self.on_done(False, str(e))
            return

        # Collect frames
        frames = []
        i = 1
        while True:
            fpath = os.path.join(self.output_dir, f"{self.prefix}_{i:04d}.jpg")
            if os.path.exists(fpath):
                frames.append(fpath)
                i += 1
            else:
                break
        if not frames:
            self.on_log("No se extrajeron frames", "err")
            self.on_done(False, "0 frames")
            return
        self.on_log(f"{len(frames)} frames extraídos ✓", "ok")
        if self.cancelled: return

        # ── GPS matching ──
        step_title = "Asignando GPS y vistas encuadradas…" if self.rectilinear else "Asignando GPS…"
        self.on_progress(65, step_title)
        self.on_log("=== " + ("RECTILINEAR + GPS" if self.rectilinear else "GPS MATCHING") + " ===", "step")
        
        original_frames_data = []
        with_gps = 0
        for idx, fpath in enumerate(frames):
            frame_sec = idx * self.interval
            frame_ms = start_ms + frame_sec * 1000
            gps = interpolate_gps(frame_ms, self.gpx_track)
            has_gps = False
            has_heading = False
            lat = lon = alt = yaw = 0
            if gps and gps["ok"] and gps.get("dist_ms", 0)/1000 <= self.tolerance:
                has_gps = True
                lat, lon, alt = gps["lat"], gps["lon"], gps["alt"]
                # yaw=None en extremos del track: coordenada válida, heading desconocido
                if gps["yaw"] is not None:
                    yaw = gps["yaw"]
                    has_heading = True
                with_gps += 1
            original_frames_data.append({
                "index": idx+1, "filename": os.path.basename(fpath),
                "filepath": fpath, "time_sec": frame_sec,
                "time_str": format_time(frame_sec),
                "lat": lat, "lon": lon, "alt": alt, "yaw": yaw,
                "has_gps": has_gps, "has_heading": has_heading
            })

        # ── Heading: IMU del giroscopio (preciso) o GPS bearing suavizado (fallback) ──
        gps_count = sum(1 for fd in original_frames_data if fd['has_gps'])
        imu_assigned = 0

        if self.heading_map:
            # Usar heading del giroscopio — mucho más preciso que GPS bearing
            self.on_log("=== ASIGNANDO HEADING IMU ===", "step")
            imu_start_ms = min(self.heading_map.keys())
            for fd in original_frames_data:
                # El frame está a frame_sec segundos del inicio del video
                # El heading_map usa timestamps en ms desde el inicio del video
                frame_video_ms = imu_start_ms + fd['time_sec'] * 1000
                imu_heading = get_heading_at_time(self.heading_map, frame_video_ms)
                if imu_heading is not None:
                    fd['yaw'] = imu_heading
                    fd['has_heading'] = True
                    fd['heading_source'] = 'imu'
                    imu_assigned += 1
            self.on_log(f"Heading IMU asignado a {imu_assigned}/{len(original_frames_data)} frames ✓", "ok")
        elif gps_count >= 3:
            # Fallback: suavizado Gaussiano de GPS bearing
            self.on_log("Suavizando bearings GPS (Gaussiano + forward/backward)...", "info")
            original_frames_data = smooth_bearings(original_frames_data, window=5)
            self.on_log("Bearings GPS suavizados OK (sin INSV — menos preciso)", "ok")

        self.frames_data = []

        if self.rectilinear:
            self.on_log(f"Generando {self.splits} splits rectilíneos por frame (FOV={self.fov}°, shift={self.baseline}m)...", "info")
            rect_frames_data = []
            
            # Determine fixed resolution for rectilinear patches to prevent aspect ratio distortion
            try:
                rect_res = int(self.resolution)
            except ValueError:
                rect_res = 1920 # Default square size if "Original" or invalid
            
            def process_split(fd, s, angle, pitch):
                abs_yaw = (fd["yaw"] + angle) % 360
                shift_lat, shift_lon = 0, 0
                if fd["has_gps"]:
                    shift_lat, shift_lon = shift_coordinate(fd["lat"], fd["lon"], abs_yaw, self.baseline)
                
                # Append pitch string to cleanly identify different elevs.
                pitch_str = f"_p{int(pitch):03d}" if pitch != 0 else ""
                out_name = f"{os.path.splitext(fd['filename'])[0]}_yaw_{int(angle):03d}{pitch_str}.jpg"
                out_path = os.path.join(self.output_dir, out_name)
                
                filter_str = f"v360=e:rectilinear:yaw={angle}:pitch={pitch}:h_fov={self.fov}:v_fov={self.fov}:w={rect_res}:h={rect_res}:interp=cubic"
                if self.cuda:
                    filter_str = f"hwdownload,format=nv12,{filter_str},format=yuvj420p"
                
                cmd = [self.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y"]
                if self.cuda:
                    cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
                cmd += ["-i", fd['filepath']]
                
                cmd += ["-vf", filter_str, "-q:v", str(self.quality), out_path]
                split_ok, split_err = run_split_cmd(cmd, out_path)

                return {
                    "index": fd["index"], "filename": out_name, "filepath": out_path,
                    "time_sec": fd["time_sec"], "time_str": fd["time_str"],
                    "lat": shift_lat, "lon": shift_lon, "alt": fd["alt"], "yaw": abs_yaw,
                    "has_gps": fd["has_gps"], "has_heading": fd.get("has_heading", False),
                    "split_ok": split_ok, "split_err": split_err
                }

            splits_to_do = []
            for fd in original_frames_data:
                for pitch in self.pitch_angles:
                    for s in range(self.splits):
                        angle = (360 / self.splits) * s
                        splits_to_do.append((fd, s, angle, pitch))
            
            split_errors = []
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {executor.submit(process_split, *args): args for args in splits_to_do}
                done_count = 0
                total_splits = len(splits_to_do)
                for future in as_completed(futures):
                    if self.cancelled:
                        executor.shutdown(wait=False, cancel_futures=True)
                        return
                    res = future.result()
                    if res.pop("split_ok"):
                        res.pop("split_err", None)
                        rect_frames_data.append(res)
                    else:
                        split_errors.append(f"{res['filename']}: {res['split_err']}")
                    done_count += 1
                    if done_count % 10 == 0 or done_count == total_splits:
                        pct = 65 + int((done_count / max(total_splits, 1)) * 6)
                        self.on_progress(pct, f"Splits: {done_count}/{total_splits}")

            if split_errors:
                self.on_log(f"⚠ {len(split_errors)}/{total_splits} splits fallaron", "warn")
                self.on_log(f"  Primer error: {split_errors[0][:300]}", "warn")
                if not rect_frames_data:
                    self.on_log("Todos los splits fallaron — se conservan los frames "
                                "equirectangulares originales", "err")
                    self.on_done(False, "Splits rectilíneos fallaron "
                                        "(¿FFmpeg sin filtro v360, o CUDA no disponible?)")
                    return

            for fd in original_frames_data:
               try: os.remove(fd["filepath"])
               except Exception: pass
               
            self.frames_data = sorted(rect_frames_data, key=lambda x: (x["index"], x["filename"]))
            with_gps = sum(1 for fd in self.frames_data if fd["has_gps"])
            frames_count = len(self.frames_data)
        else:
            self.frames_data = original_frames_data
            frames_count = len(frames)

        self.on_log(f"GPS: {with_gps}/{frames_count} con coordenadas", "ok")
        if self.cancelled: return

        # ── EXIF (MULTITHREADED) ──
        if HAS_PIEXIF and self.inject_exif:
            self.on_log(f"=== EXIF GPS - {self.max_workers} THREADS ===", "step")
            self.on_progress(72, "Inyectando EXIF…")
            gps_frames = [fd for fd in self.frames_data if fd["has_gps"]]
            exif_ok = 0
            total_gps = len(gps_frames)

            def _inject_one(fd):
                # Sin heading conocido → None (no escribir GPSImgDirection);
                # con heading, incluso 0.0 (norte) se escribe.
                yaw = fd["yaw"] if fd.get("has_heading") else None
                return inject_gps_exif(fd["filepath"], fd["lat"], fd["lon"], fd["alt"], yaw)

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {executor.submit(_inject_one, fd): fd for fd in gps_frames}
                done_count = 0
                for future in as_completed(futures):
                    if self.cancelled:
                        executor.shutdown(wait=False, cancel_futures=True)
                        return
                    done_count += 1
                    if future.result():
                        exif_ok += 1
                    if done_count % 5 == 0 or done_count == total_gps:
                        pct = 72 + int(done_count / max(total_gps, 1) * 22)
                        self.on_progress(pct, f"EXIF: {done_count}/{total_gps}")
            self.on_log(f"EXIF inyectado: {exif_ok}/{total_gps} ✓", "ok")
        elif not HAS_PIEXIF:
            self.on_log("piexif no disponible — GPS solo en CSV", "warn")
        if self.cancelled: return

        # ── CSV + Report ──
        csv_path = os.path.join(self.output_dir, "_coordinates.csv")
        write_csv(csv_path, self.frames_data)
        self.on_log(f"CSV: {csv_path}", "ok")

        elapsed = time.time() - t0
        rpt = os.path.join(self.output_dir, "_report.txt")
        self._write_report(rpt, elapsed, with_gps)
        self.on_log(f"Reporte: {rpt}", "ok")

        # ── Done ──
        self.on_progress(100, "Completado")
        total_size = sum(os.path.getsize(fd["filepath"]) for fd in self.frames_data if os.path.exists(fd["filepath"]))
        summary = f"{len(self.frames_data)} frames · {with_gps} GPS · {total_size/1024/1024:.1f} MB · {format_time(elapsed)}"
        self.on_log(f"COMPLETADO: {summary}", "ok")
        self.on_done(True, summary)

    def _write_report(self, filepath, elapsed, with_gps):
        vi = self.video_info
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("=" * 55 + "\n  VIDEO 360 → FRAMES GPS — Bureau Veritas\n" + "=" * 55 + "\n\n")
            f.write(f"  Fecha:       {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"  Duración:    {format_time(elapsed)}\n")
            f.write(f"  Video:       {self.video_path}\n")
            f.write(f"  GPX:         {self.gpx_path}\n")
            f.write(f"  Intervalo:   {self.interval}s | Calidad: q{self.quality} | Res: {self.resolution}\n")
            f.write(f"  Threads:     {self.max_workers}\n")
            f.write(f"  Frames:      {len(self.frames_data)} ({with_gps} con GPS)\n")
            if self.rectilinear:
                f.write(f"  Rectilinear: {self.splits} splits | FOV: {self.fov}° | Shift: {self.baseline}m\n")
            f.write(f"  Carpeta:     {self.output_dir}\n\n")
            for fd in self.frames_data[:20]:
                g = f"{fd['lat']:.6f}, {fd['lon']:.6f}" if fd["has_gps"] else "Sin GPS"
                f.write(f"  {fd['filename']:20s}  {fd['time_str']:8s}  {g}\n")
            if len(self.frames_data) > 20:
                f.write(f"  … +{len(self.frames_data)-20} más\n")


# ═══════════════════════════════════════════════
# GUI — TKINTER
# ═══════════════════════════════════════════════
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    BG = "#0A1628"; BG2 = "#112240"; BG3 = "#1A3254"
    FG = "#E8EDF4"; GRAY = "#7A8FA6"; ACCENT = "#3B8FDB"
    GREEN = "#22C55E"; ORANGE = "#F59E0B"; RED = "#E3001B"; BORDER = "#1E3A5F"

    root = tk.Tk()
    root.title("Video 360° → Frames GPS — Bureau Veritas v3.0")
    root.geometry("780x830")
    root.configure(bg=BG)
    root.minsize(650, 700)

    style = ttk.Style()
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=FG, fieldbackground=BG2, bordercolor=BORDER)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
    style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=8)
    style.configure("TCombobox", fieldbackground=BG3, foreground=FG, selectbackground=ACCENT)
    style.configure("TEntry", fieldbackground=BG3, foreground=FG)
    style.map("TCombobox", fieldbackground=[("readonly", BG3)])
    style.configure("green.Horizontal.TProgressbar", troughcolor=BG3, background=GREEN)

    var_video = tk.StringVar(); var_gpx = tk.StringVar(); var_insv = tk.StringVar()
    var_interval = tk.StringVar(value="2"); var_quality = tk.StringVar(value="4 — Alta")
    var_resolution = tk.StringVar(value="Original"); var_prefix = tk.StringVar(value="FRAME")
    var_output = tk.StringVar(); var_start = tk.StringVar(); var_offset = tk.StringVar(value="0")
    var_threads = tk.StringVar(value=str(min(os.cpu_count() or 4, 8)))
    var_rectilinear = tk.BooleanVar(value=False)
    var_splits = tk.StringVar(value="8"); var_fov = tk.StringVar(value="90")
    var_baseline = tk.StringVar(value="1.0")
    var_cuda = tk.BooleanVar(value=False); var_pitch_angles = tk.StringVar(value="0")
    extractor_ref = [None]

    # ═══ TOPBAR ═══
    topbar = tk.Frame(root, bg=RED, height=44); topbar.pack(fill="x"); topbar.pack_propagate(False)
    tk.Label(topbar, text="  BV", bg=RED, fg="white", font=("Segoe UI", 16, "bold")).pack(side="left")
    tk.Label(topbar, text="  VIDEO 360° → FRAMES CON GPS", bg=RED, fg="white", font=("Segoe UI", 11)).pack(side="left", padx=10)
    tk.Label(topbar, text="v3.0  ", bg=RED, fg="#ffcccc", font=("Consolas", 9)).pack(side="right")

    # ═══ SCROLLABLE ═══
    outer = tk.Frame(root, bg=BG); outer.pack(fill="both", expand=True)
    cvs = tk.Canvas(outer, bg=BG, highlightthickness=0)
    sb = ttk.Scrollbar(outer, orient="vertical", command=cvs.yview)
    inner = tk.Frame(cvs, bg=BG)
    inner.bind("<Configure>", lambda e: cvs.configure(scrollregion=cvs.bbox("all")))
    cvs.create_window((0, 0), window=inner, anchor="nw")
    cvs.configure(yscrollcommand=sb.set)
    cvs.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
    def _wheel(e): cvs.yview_scroll(int(-1*(e.delta/120)), "units")
    cvs.bind_all("<MouseWheel>", _wheel)

    def make_card(parent):
        c = tk.Frame(parent, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        c.pack(fill="x", padx=14, pady=6); return c

    # ═══ CARD 1: FILES ═══
    c1 = make_card(inner)
    tk.Label(c1, text="① ARCHIVOS DE ENTRADA", bg=BG2, fg=ACCENT, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(10,4))

    def pick_video():
        f = filedialog.askopenfilename(title="Video 360°", filetypes=[("Video","*.mp4 *.mov *.insv *.MP4 *.MOV"),("Todos","*.*")])
        if f:
            var_video.set(f); lv.config(text=os.path.basename(f), fg=GREEN)
            if not var_output.get(): var_output.set(os.path.join(os.path.dirname(f), f"frames_{Path(f).stem}"))

    def pick_gpx():
        f = filedialog.askopenfilename(title="Track GPX", filetypes=[("GPX","*.gpx *.GPX"),("Todos","*.*")])
        if f:
            var_gpx.set(f); lg.config(text=os.path.basename(f), fg=GREEN)
            try:
                pts = parse_gpx(f)
                if pts: var_start.set(pts[0]["time"].strftime("%Y-%m-%d %H:%M:%S"))
            except Exception: pass

    rf = tk.Frame(c1, bg=BG2); rf.pack(fill="x", padx=12, pady=6)
    # Video
    fv = tk.Frame(rf, bg=BG2); fv.pack(side="left", fill="x", expand=True, padx=(0,6))
    tk.Label(fv, text="Video 360° (.mp4)", bg=BG2, fg=GRAY, font=("Consolas", 9)).pack(anchor="w")
    tk.Button(fv, text="🎬  Seleccionar video…", bg=BG3, fg=FG, bd=0, font=("Segoe UI",10), cursor="hand2", padx=12, pady=6, command=pick_video).pack(fill="x", pady=2)
    lv = tk.Label(fv, text="Sin archivo", bg=BG2, fg=GRAY, font=("Consolas",9)); lv.pack(anchor="w")
    # GPX
    fg_ = tk.Frame(rf, bg=BG2); fg_.pack(side="left", fill="x", expand=True, padx=(6,0))
    tk.Label(fg_, text="Track GPS (.gpx — opcional con INSV)", bg=BG2, fg=GRAY, font=("Consolas", 9)).pack(anchor="w")
    tk.Button(fg_, text="🛰  Seleccionar GPX…", bg=BG3, fg=FG, bd=0, font=("Segoe UI",10), cursor="hand2", padx=12, pady=6, command=pick_gpx).pack(fill="x", pady=2)
    lg = tk.Label(fg_, text="Sin archivo", bg=BG2, fg=GRAY, font=("Consolas",9)); lg.pack(anchor="w")

    # INSV (opcional — giroscopio)
    def pick_insv():
        f = filedialog.askopenfilename(title="Archivo INSV (giroscopio)", filetypes=[("INSV","*.insv *.INSV"),("Todos","*.*")])
        if f:
            var_insv.set(f); li.config(text=os.path.basename(f), fg=GREEN)

    ri = tk.Frame(c1, bg=BG2); ri.pack(fill="x", padx=12, pady=(2,6))
    tk.Label(ri, text="📐 Archivo INSV original (opcional — heading del giroscopio + GPS embebido)", bg=BG2, fg=ORANGE, font=("Consolas", 9)).pack(anchor="w")
    tk.Button(ri, text="🔧  Seleccionar INSV…", bg=BG3, fg=FG, bd=0, font=("Segoe UI",10), cursor="hand2", padx=12, pady=4, command=pick_insv).pack(fill="x", pady=2)
    li = tk.Label(ri, text="Sin INSV — se usará GPS bearing (menos preciso)", bg=BG2, fg=GRAY, font=("Consolas",9)); li.pack(anchor="w")
    tk.Frame(c1, bg=BG2, height=6).pack()

    # ═══ CARD 2: CONFIG ═══
    c2 = make_card(inner)
    tk.Label(c2, text="② CONFIGURACIÓN", bg=BG2, fg=ACCENT, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(10,4))

    r1 = tk.Frame(c2, bg=BG2); r1.pack(fill="x", padx=12, pady=4)
    for lbl, var, vals in [
        ("Intervalo (seg)", var_interval, ["1","2","3","5","10","15","30","60"]),
        ("Calidad JPEG", var_quality, ["2 — Máxima","4 — Alta","8 — Media","15 — Baja"]),
        ("Resolución", var_resolution, ["Original","5760x2880 (5.7K)","3840x1920 (4K)","2048x1024 (2K)"]),
    ]:
        f = tk.Frame(r1, bg=BG2); f.pack(side="left", fill="x", expand=True, padx=3)
        tk.Label(f, text=lbl, bg=BG2, fg=GRAY, font=("Consolas",9)).pack(anchor="w")
        ttk.Combobox(f, textvariable=var, values=vals, state="readonly", width=18).pack(fill="x", pady=2)

    r2 = tk.Frame(c2, bg=BG2); r2.pack(fill="x", padx=12, pady=4)
    for lbl, var, w in [("Prefijo", var_prefix, 12), (f"Threads ({os.cpu_count()} CPU)", var_threads, 6)]:
        f = tk.Frame(r2, bg=BG2); f.pack(side="left", fill="x", expand=True, padx=3)
        tk.Label(f, text=lbl, bg=BG2, fg=GRAY, font=("Consolas",9)).pack(anchor="w")
        if lbl.startswith("Threads"):
            ttk.Combobox(f, textvariable=var, values=["1","2","4","6","8","12","16"], state="readonly", width=w).pack(fill="x", pady=2)
        else:
            ttk.Entry(f, textvariable=var, width=w).pack(fill="x", pady=2)

    def pick_out():
        d = filedialog.askdirectory(title="Carpeta de salida")
        if d: var_output.set(d)
    fo = tk.Frame(r2, bg=BG2); fo.pack(side="left", fill="x", expand=True, padx=3)
    tk.Label(fo, text="Carpeta salida", bg=BG2, fg=GRAY, font=("Consolas",9)).pack(anchor="w")
    ro = tk.Frame(fo, bg=BG2); ro.pack(fill="x", pady=2)
    ttk.Entry(ro, textvariable=var_output, width=20).pack(side="left", fill="x", expand=True)
    tk.Button(ro, text="📁", bg=BG3, fg=FG, bd=0, padx=6, command=pick_out).pack(side="left", padx=2)

    r3 = tk.Frame(c2, bg=BG2); r3.pack(fill="x", padx=12, pady=4)
    for lbl, var, w in [("Inicio video (auto-GPX)", var_start, 22), ("Offset seg", var_offset, 8)]:
        f = tk.Frame(r3, bg=BG2); f.pack(side="left", fill="x", expand=True, padx=3)
        tk.Label(f, text=lbl, bg=BG2, fg=GRAY, font=("Consolas",9)).pack(anchor="w")
        ttk.Entry(f, textvariable=var, width=w).pack(fill="x", pady=2)

    r4 = tk.Frame(c2, bg=BG2); r4.pack(fill="x", padx=12, pady=2)
    chk_rect = tk.Checkbutton(r4, text="Extracción Rectilínea (Vista de Drone)", variable=var_rectilinear, bg=BG2, fg=ACCENT, selectcolor=BG, activebackground=BG2, font=("Segoe UI", 9, "bold"))
    chk_rect.pack(side="left", padx=3)
    chk_cuda = tk.Checkbutton(r4, text="Usar CUDA", variable=var_cuda, bg=BG2, fg="#A3E635", selectcolor=BG, activebackground=BG2, font=("Segoe UI", 9, "bold"))
    chk_cuda.pack(side="right", padx=12)
    
    f_rect = tk.Frame(c2, bg=BG2); f_rect.pack(fill="x", padx=12, pady=4)
    for lbl, var, w in [("Pitch Angles (ej. -30,0)", var_pitch_angles, 20), ("Vistas (Splits)", var_splits, 6), ("FOV (°)", var_fov, 6), ("Desliz (m)", var_baseline, 6)]:
        f = tk.Frame(f_rect, bg=BG2); f.pack(side="left", fill="x", expand=True, padx=3)
        tk.Label(f, text=lbl, bg=BG2, fg=GRAY, font=("Consolas",9)).pack(anchor="w")
        ttk.Entry(f, textvariable=var, width=w).pack(fill="x", pady=2)
        
    tk.Frame(c2, bg=BG2, height=8).pack()

    # ═══ CARD 3: PROGRESS ═══
    c3 = make_card(inner)
    tk.Label(c3, text="③ PROCESAMIENTO", bg=BG2, fg=ACCENT, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(10,4))
    pbar = ttk.Progressbar(c3, style="green.Horizontal.TProgressbar", length=400, mode="determinate")
    pbar.pack(fill="x", padx=12, pady=4)
    lbl_prog = tk.Label(c3, text="Esperando…", bg=BG2, fg=GRAY, font=("Consolas",9)); lbl_prog.pack(anchor="w", padx=12)

    log_w = tk.Text(c3, bg="#050D18", fg=GRAY, font=("Consolas",9), height=14, wrap="word", bd=0, padx=8, pady=6)
    log_w.pack(fill="x", padx=12, pady=6)
    for t, c in [("ok",GREEN),("err",RED),("warn",ORANGE),("info",ACCENT),("step","#C084FC")]:
        log_w.tag_configure(t, foreground=c)

    bf = tk.Frame(c3, bg=BG2); bf.pack(fill="x", padx=12, pady=(2,10))

    def start():
        if not var_video.get(): messagebox.showwarning("","Selecciona un video"); return
        if not var_gpx.get() and not var_insv.get():
            messagebox.showwarning("","Selecciona un GPX, o un INSV con GPS embebido"); return
        if not var_output.get(): var_output.set(os.path.join(os.path.dirname(var_video.get()), f"frames_{Path(var_video.get()).stem}"))

        interval = int(var_interval.get())
        quality = int(var_quality.get().split()[0])
        res_raw = var_resolution.get()
        resolution = "original"
        if "x" in res_raw: resolution = res_raw.split()[0]
        start_time = None
        if var_start.get().strip():
            try:
                start_time = datetime.fromisoformat(var_start.get().strip())
                if start_time.tzinfo is None: start_time = start_time.replace(tzinfo=timezone.utc)
            except Exception:
                messagebox.showerror("Error", f"Fecha inválida: {var_start.get()}"); return

        log_w.delete("1.0","end"); pbar["value"]=0
        btn_go.config(state="disabled"); btn_stop.config(state="normal")

        ext = FrameExtractor(
            var_video.get(), var_gpx.get() or None, var_output.get(),
            interval, quality, resolution, var_prefix.get() or "FRAME",
            start_time, float(var_offset.get() or 0), 30, True,
            int(var_threads.get()),
            rectilinear=var_rectilinear.get(),
            splits=int(var_splits.get()),
            fov=int(var_fov.get()),
            baseline=float(var_baseline.get()),
            cuda=var_cuda.get(),
            pitch_angles=var_pitch_angles.get(),
            insv_path=var_insv.get() or None
        )
        extractor_ref[0] = ext

        def gl(msg, level="info"):
            tag = level if level in ("ok","err","warn","info","step") else ""
            root.after(0, lambda: (log_w.insert("end", msg+"\n", tag), log_w.see("end")))
        def gp(pct, msg):
            root.after(0, lambda: (pbar.__setitem__("value", pct), lbl_prog.config(text=msg)))
        def gd(ok, msg):
            def _do():
                btn_go.config(state="normal"); btn_stop.config(state="disabled")
                if ok:
                    lbl_prog.config(text=f"✓ {msg}", fg=GREEN); btn_folder.config(state="normal")
                    messagebox.showinfo("Completado", f"{msg}\n\nCarpeta: {var_output.get()}")
                else:
                    lbl_prog.config(text=f"✗ {msg}", fg=RED)
            root.after(0, _do)

        ext.on_log=gl; ext.on_progress=gp; ext.on_done=gd
        threading.Thread(target=ext.run, daemon=True).start()

    def stop():
        if extractor_ref[0]: extractor_ref[0].cancel(); lbl_prog.config(text="Cancelando…", fg=ORANGE)

    def open_dir():
        d = var_output.get()
        if d and os.path.isdir(d):
            if platform.system()=="Windows": os.startfile(d)
            elif platform.system()=="Darwin": subprocess.Popen(["open",d])
            else: subprocess.Popen(["xdg-open",d])

    btn_go = tk.Button(bf, text="▶  PROCESAR", bg=ACCENT, fg="white", bd=0, font=("Segoe UI",11,"bold"), cursor="hand2", padx=18, pady=8, command=start)
    btn_go.pack(side="left")
    btn_stop = tk.Button(bf, text="✕ Cancelar", bg=BG3, fg=RED, bd=0, font=("Segoe UI",10), padx=12, pady=6, state="disabled", command=stop)
    btn_stop.pack(side="left", padx=8)
    btn_folder = tk.Button(bf, text="📁 Abrir carpeta", bg=BG3, fg=GREEN, bd=0, font=("Segoe UI",10), padx=12, pady=6, state="disabled", command=open_dir)
    btn_folder.pack(side="right")

    # ═══ STATUS BAR ═══
    sbar = tk.Frame(root, bg=BG3, height=26); sbar.pack(fill="x", side="bottom"); sbar.pack_propagate(False)
    ff = find_ffmpeg()
    tk.Label(sbar, text=f"FFmpeg: {ff or '✗ NO ENCONTRADO'}", bg=BG3, fg=GREEN if ff else RED, font=("Consolas",8)).pack(side="left", padx=8)
    tk.Label(sbar, text=f"piexif: {'✓' if HAS_PIEXIF else '✗'}", bg=BG3, fg=GREEN if HAS_PIEXIF else RED, font=("Consolas",8)).pack(side="left", padx=8)
    tk.Label(sbar, text=f"CPU: {os.cpu_count()} cores", bg=BG3, fg=GRAY, font=("Consolas",8)).pack(side="right", padx=8)

    if not ff:
        root.after(500, lambda: messagebox.showwarning("FFmpeg", install_ffmpeg_hint()+"\n\nReinicia la app después de instalar."))

    root.mainloop()


# ═══════════════════════════════════════════════
# CLI MODE
# ═══════════════════════════════════════════════
def run_cli():
    print(f"\n{'='*55}\n  VIDEO 360 -> FRAMES GPS - Bureau Veritas v{VERSION} CLI\n{'='*55}\n")

    parser = argparse.ArgumentParser(
        description="Extrae frames de video 360° con GPS desde GPX o desde el GPS embebido del INSV.")
    parser.add_argument("video", help="Video 360° (.mp4, .mov, .insv)")
    parser.add_argument("gpx", nargs="?", default=None,
                        help="Track GPS (.gpx) — opcional si se pasa --insv con GPS embebido")
    parser.add_argument("-i","--interval", type=int, default=2, choices=[1,2,3,5,10,15,30,60])
    parser.add_argument("-q","--quality", type=int, default=4)
    parser.add_argument("-r","--resolution", default="original")
    parser.add_argument("-o","--output", default=None)
    parser.add_argument("-p","--prefix", default="FRAME")
    parser.add_argument("--start", default=None)
    parser.add_argument("--offset", type=float, default=0)
    parser.add_argument("--tolerance", type=float, default=30)
    parser.add_argument("--threads", type=int, default=min(os.cpu_count() or 4, 8))
    parser.add_argument("--no-exif", action="store_true")
    parser.add_argument("--rectilinear", action="store_true", help="Activar extracción rectilínea")
    parser.add_argument("--splits", type=int, default=8, help="Número de vistas (ej. 8)")
    parser.add_argument("--fov", type=int, default=90, help="Campo de visión en grados (ej. 90)")
    parser.add_argument("--baseline", type=float, default=1.0, help="Desplazamiento GPS en metros (ej. 1.0)")
    parser.add_argument("--cuda", action="store_true", help="Habilitar aceleración por hardware CUDA")
    parser.add_argument("--pitch-angles", default="0.0", help="Ángulos de elevación separados por comas (ej. '-30,0,30')")
    parser.add_argument("--insv", default=None, help="Archivo INSV original para heading preciso del giroscopio")
    args = parser.parse_args()

    if not args.gpx and not args.insv:
        print("  ✗ Se necesita un GPX o un --insv con GPS embebido"); sys.exit(1)
    checks = [(args.video, "Video")]
    if args.gpx: checks.append((args.gpx, "GPX"))
    if args.insv: checks.append((args.insv, "INSV"))
    for f, n in checks:
        if not os.path.isfile(f): print(f"  ✗ {n} no encontrado: {f}"); sys.exit(1)
    if not args.output: args.output = f"frames_{Path(args.video).stem}"

    start_time = None
    if args.start:
        start_time = datetime.fromisoformat(args.start)
        if start_time.tzinfo is None: start_time = start_time.replace(tzinfo=timezone.utc)

    ext = FrameExtractor(args.video, args.gpx, args.output, args.interval, args.quality,
                          args.resolution, args.prefix, start_time, args.offset,
                          args.tolerance, not args.no_exif, args.threads,
                          rectilinear=args.rectilinear, splits=args.splits, fov=args.fov, baseline=args.baseline,
                          cuda=args.cuda, pitch_angles=args.pitch_angles,
                          insv_path=args.insv)

    syms = {"info":"●","ok":"✓","warn":"⚠","err":"✗","step":"►"}
    def cl(msg,lv="info"):
        print(f"  {syms.get(lv,'·')} [{datetime.now().strftime('%H:%M:%S')}] {msg}")
    def cp(pct,msg):
        bl=30; fi=int(bl*pct/100); bar="█"*fi+"░"*(bl-fi)
        sys.stdout.write(f"\r  [{bar}] {pct:3d}% {msg}   "); sys.stdout.flush()
        if pct>=100: print()
    def cd(ok,msg):
        print()
        if ok: print(f"  ✓ COMPLETADO: {msg}\n  Carpeta: {args.output}")
        else: print(f"  ✗ FALLÓ: {msg}")

    ext.on_log=cl; ext.on_progress=cp; ext.on_done=cd
    ext.run()


# ═══════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    if len(sys.argv) > 1 and ("--cli" in sys.argv or sys.argv[1] in ("-h", "--help") or not sys.argv[1].startswith("-")):
        if "--cli" in sys.argv:
            sys.argv.remove("--cli")
        run_cli()
    else:
        try:
            import tkinter
            run_gui()
        except ImportError:
            print("\n  ⚠ tkinter no disponible para GUI")
            print("    Ubuntu/Debian: sudo apt install python3-tk")
            print("    Fedora:        sudo dnf install python3-tkinter")
            print("    Mac:           brew install python-tk")
            print("\n  Modo CLI: python video360_frame_extractor.py video.mp4 track.gpx\n")
