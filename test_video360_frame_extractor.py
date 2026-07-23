#!/usr/bin/env python3
"""Tests del extractor — enfocados en el contrato que consume bv-tour-builder:
EXIF GPSLatitude/GPSLongitude + GPSImgDirection, y robustez del pipeline.

Ejecutar:  python -m pytest test_video360_frame_extractor.py -v
"""
import os
import sys

import piexif
import pytest
from PIL import Image

import video360_frame_extractor as vfe


# ═══════════════════════════════════════════════
# Fix 1 — heading: yaw=0 (norte) es válido y debe escribirse;
#          None es el centinela de "sin heading"
# ═══════════════════════════════════════════════
class TestInjectGpsExifHeading:
    def _make_jpeg(self, tmp_path):
        p = str(tmp_path / "frame.jpg")
        Image.new("RGB", (16, 8)).save(p, "JPEG")
        return p

    def test_yaw_cero_norte_se_escribe(self, tmp_path):
        """Un frame mirando exactamente al norte (yaw=0.0) DEBE llevar GPSImgDirection."""
        p = self._make_jpeg(tmp_path)
        assert vfe.inject_gps_exif(p, 40.0, -3.0, 650.0, yaw=0.0)
        gps = piexif.load(p)["GPS"]
        assert piexif.GPSIFD.GPSImgDirection in gps, "yaw=0 (norte) no se escribió"
        assert gps[piexif.GPSIFD.GPSImgDirection] == (0, 100)
        assert gps[piexif.GPSIFD.GPSImgDirectionRef] == b"T"

    def test_yaw_none_no_se_escribe(self, tmp_path):
        """yaw=None significa 'heading desconocido' → no escribir el tag."""
        p = self._make_jpeg(tmp_path)
        assert vfe.inject_gps_exif(p, 40.0, -3.0, 650.0, yaw=None)
        gps = piexif.load(p)["GPS"]
        assert piexif.GPSIFD.GPSImgDirection not in gps

    def test_yaw_normal_se_escribe(self, tmp_path):
        p = self._make_jpeg(tmp_path)
        assert vfe.inject_gps_exif(p, -33.45, 70.66, None, yaw=123.4)
        gps = piexif.load(p)["GPS"]
        assert gps[piexif.GPSIFD.GPSImgDirection] == (12340, 100)
        # Refs de hemisferio correctos (exifr en bv-tour-builder los usa para el signo)
        assert gps[piexif.GPSIFD.GPSLatitudeRef] == b"S"
        assert gps[piexif.GPSIFD.GPSLongitudeRef] == b"E"


class TestInterpolateGpsEdges:
    TRACK = [
        {"lat": 40.0, "lon": -3.0, "alt": 600.0, "time_ms": 1000},
        {"lat": 40.001, "lon": -3.0, "alt": 610.0, "time_ms": 11000},
    ]

    def test_antes_del_track_yaw_none(self):
        """Antes del inicio del track no hay dirección de movimiento → yaw None, no 0."""
        r = vfe.interpolate_gps(0, self.TRACK)
        assert r["ok"]
        assert r["lat"] == 40.0
        assert r["yaw"] is None

    def test_despues_del_track_yaw_none(self):
        r = vfe.interpolate_gps(99999, self.TRACK)
        assert r["ok"]
        assert r["lat"] == 40.001
        assert r["yaw"] is None

    def test_interpolacion_media_yaw_valido(self):
        """En medio del track el yaw es el bearing real (aquí: norte ≈ 0°)."""
        r = vfe.interpolate_gps(6000, self.TRACK)
        assert r["ok"]
        assert abs(r["lat"] - 40.0005) < 1e-6
        assert r["yaw"] is not None
        assert min(r["yaw"], 360 - r["yaw"]) < 1.0  # ≈ norte

    def test_dist_ms_refleja_huecos_interiores(self):
        """Track INSV real (018): huecos de hasta 683s entre fixes. Un frame en
        medio del hueco NO debe pasar el filtro de tolerancia como si tuviera
        un fix al lado: dist_ms = distancia temporal al fix más cercano."""
        track = [
            {"lat": 40.0, "lon": -3.0, "alt": 0.0, "time_ms": 0},
            {"lat": 40.001, "lon": -3.0, "alt": 0.0, "time_ms": 700_000},
        ]
        r_medio = vfe.interpolate_gps(350_000, track)   # a 350s de ambos fixes
        assert r_medio["dist_ms"] == 350_000
        r_cerca = vfe.interpolate_gps(5_000, track)     # a 5s del primer fix
        assert r_cerca["dist_ms"] == 5_000


# ═══════════════════════════════════════════════
# Fix 2 — smooth_bearings: el fallback de referencia nunca debe
#          usar un frame SIN GPS (bearing hacia lat 0, lon 0)
# ═══════════════════════════════════════════════
class TestSmoothBearingsFallback:
    @staticmethod
    def _frame(idx, lat, lon, has_gps):
        return {
            "index": idx + 1, "filename": f"F_{idx:04d}.jpg", "filepath": "",
            "time_sec": idx * 2, "time_str": "0:00", "lat": lat, "lon": lon,
            "alt": 0.0, "yaw": 0, "has_gps": has_gps,
        }

    def test_fallback_ignora_frames_sin_gps(self):
        """Frame GPS con vecino <1m y frame intermedio sin GPS:
        el bearing debe calcularse hacia el otro frame GPS (norte ≈ 0°),
        NO hacia (0,0) — que daría ≈175° (golfo de Guinea)."""
        frames = [
            self._frame(0, 40.0, -3.0, True),        # A
            self._frame(1, 0.0, 0.0, False),         # sin GPS
            self._frame(2, 40.000005, -3.0, True),   # B, ~0.55m al norte de A
        ]
        out = vfe.smooth_bearings(frames, window=5)
        yaw0 = out[0]["yaw"]
        # Circular: distancia angular a 0° (norte) debe ser pequeña
        assert min(yaw0, 360 - yaw0) < 10.0, (
            f"yaw={yaw0}° — se calculó bearing hacia un frame sin GPS (lat 0, lon 0)"
        )

    def test_caso_normal_sigue_funcionando(self):
        """Regresión: track normal hacia el norte, todos con GPS y >1m."""
        frames = [
            self._frame(0, 40.0000, -3.0, True),
            self._frame(1, 40.0002, -3.0, True),   # ~22m norte
            self._frame(2, 40.0004, -3.0, True),
        ]
        out = vfe.smooth_bearings(frames, window=5)
        for fd in out:
            assert min(fd["yaw"], 360 - fd["yaw"]) < 5.0, f"yaw={fd['yaw']}° debería ser ≈ norte"


# ═══════════════════════════════════════════════
# Fix 3 — solapamiento temporal video ↔ GPX
# ═══════════════════════════════════════════════
class TestGpxVideoOverlap:
    TRACK = [
        {"lat": 40.0, "lon": -3.0, "alt": 0.0, "time_ms": 0},
        {"lat": 40.1, "lon": -3.0, "alt": 0.0, "time_ms": 100_000},
    ]

    def test_solapamiento_total(self):
        secs, ratio = vfe.gpx_video_overlap(0, 100_000, self.TRACK)
        assert secs == pytest.approx(100.0)
        assert ratio == pytest.approx(1.0)

    def test_sin_solapamiento(self):
        """Caso típico: creation_time del MP4 = fecha de export, no de grabación."""
        secs, ratio = vfe.gpx_video_overlap(200_000, 300_000, self.TRACK)
        assert secs == 0.0
        assert ratio == 0.0

    def test_solapamiento_parcial(self):
        secs, ratio = vfe.gpx_video_overlap(50_000, 150_000, self.TRACK)
        assert secs == pytest.approx(50.0)
        assert ratio == pytest.approx(0.5)

    def test_track_vacio(self):
        secs, ratio = vfe.gpx_video_overlap(0, 100_000, [])
        assert secs == 0.0
        assert ratio == 0.0


# ═══════════════════════════════════════════════
# Fix 4 — los splits rectilíneos deben detectar fallos de FFmpeg
# ═══════════════════════════════════════════════
class TestRunSplitCmd:
    def test_comando_exitoso_con_salida(self, tmp_path):
        out = str(tmp_path / "ok.jpg")
        cmd = [sys.executable, "-c", f"open(r'{out}', 'w').write('x')"]
        ok, err = vfe.run_split_cmd(cmd, out)
        assert ok is True

    def test_comando_falla_reporta_stderr(self, tmp_path):
        out = str(tmp_path / "nope.jpg")
        cmd = [sys.executable, "-c", "import sys; sys.stderr.write('boom v360'); sys.exit(1)"]
        ok, err = vfe.run_split_cmd(cmd, out)
        assert ok is False
        assert "boom v360" in err

    def test_exit_cero_sin_archivo_es_fallo(self, tmp_path):
        """FFmpeg puede salir 0 sin producir el archivo (filtro inexistente + loglevel error)."""
        out = str(tmp_path / "ghost.jpg")
        cmd = [sys.executable, "-c", "pass"]
        ok, err = vfe.run_split_cmd(cmd, out)
        assert ok is False


# ═══════════════════════════════════════════════
# Feature 5 — GPS embebido del INSV como track alternativo al GPX
# Patrones reales de la X5 (D:\Ballenas): muestras a ~10Hz repitiendo
# el último fix, y tracks congelados (mismo ts en todas las muestras)
# ═══════════════════════════════════════════════
class TestInsvGpsToTrack:
    @staticmethod
    def _pt(ts, ms, lat, lon, alt=0.0):
        return {"unix_ts": ts, "ms": ms, "lat": lat, "lon": lon,
                "alt": alt, "speed": 0.5, "track": 90.0}

    def test_convierte_al_formato_de_track_gpx(self):
        pts = [self._pt(1784559476, 993, 11.695897, -72.721963, -15.3),
               self._pt(1784559477, 993, 11.695900, -72.721970, -15.5)]
        track = vfe.insv_gps_to_track(pts)
        assert len(track) == 2
        p = track[0]
        assert p["time_ms"] == 1784559476993
        assert p["lat"] == 11.695897
        assert p["lon"] == -72.721963
        assert p["alt"] == -15.3
        # Mismo formato que parse_gpx: interpolate_gps debe funcionar tal cual
        r = vfe.interpolate_gps(1784559477493, track)
        assert r["ok"]
        assert 11.695897 < r["lat"] < 11.695900

    def test_dedupe_muestras_repetidas_10hz(self):
        """La X5 registra ~10Hz repitiendo el último fix (~1-3Hz reales)."""
        pts = ([self._pt(100, 500, 11.0, -72.0)] * 3 +
               [self._pt(101, 500, 11.001, -72.0)] * 3 +
               [self._pt(102, 500, 11.002, -72.0)] * 4)
        track = vfe.insv_gps_to_track(pts)
        assert len(track) == 3
        assert [p["time_ms"] for p in track] == [100500, 101500, 102500]

    def test_track_congelado_queda_degenerado(self):
        """Archivo 021 real: 7060 muestras, todas el mismo ts/posición → 1 fix único."""
        pts = [self._pt(200, 0, 11.6955, -72.7246)] * 50
        track = vfe.insv_gps_to_track(pts)
        assert len(track) == 1

    def test_ordena_cronologicamente(self):
        pts = [self._pt(300, 0, 11.2, -72.0),
               self._pt(100, 0, 11.0, -72.0),
               self._pt(200, 0, 11.1, -72.0)]
        track = vfe.insv_gps_to_track(pts)
        assert [p["time_ms"] for p in track] == [100000, 200000, 300000]

    def test_vacio(self):
        assert vfe.insv_gps_to_track([]) == []


# ═══════════════════════════════════════════════
# Ejecutable congelado (PyInstaller): sin auto-install de pip
# ═══════════════════════════════════════════════
class TestFrozenExecutable:
    def test_auto_install_se_salta_en_exe_congelado(self, monkeypatch):
        """En un .exe de PyInstaller las dependencias van empaquetadas y
        sys.executable no es Python: jamás debe invocarse pip."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setitem(vfe.REQUIRED_PACKAGES, "modulo_inexistente_xyz", "paquete-fantasma")
        llamadas = []
        monkeypatch.setattr(vfe.subprocess, "check_call",
                            lambda *a, **k: llamadas.append(a))
        assert vfe.auto_install_packages() is True
        assert llamadas == [], "auto_install invocó pip dentro de un exe congelado"


class TestPiexifObligatorio:
    """Caso real (ejercicio Ballenas 023/024/025): la app corrió con un Python
    sin piexif (MSYS2), extrajo 330 frames en ~10 min y los dejó SIN GPS con
    solo un warning enterrado en el log. Si el usuario pidió EXIF (default),
    la falta de piexif debe ABORTAR al inicio, no degradar en silencio."""

    def test_pipeline_aborta_sin_piexif_cuando_se_pide_exif(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vfe, "HAS_PIEXIF", False)
        ext = vfe.FrameExtractor("no_existe.mp4", "no_existe.gpx", str(tmp_path / "out"))
        resultado = []
        ext.on_done = lambda ok, msg: resultado.append((ok, msg))
        ext.run()
        assert resultado, "el pipeline no reportó resultado"
        ok, msg = resultado[0]
        assert ok is False
        assert "piexif" in msg.lower()

    def test_pipeline_no_aborta_por_piexif_con_no_exif(self, tmp_path, monkeypatch):
        """Con --no-exif la falta de piexif es legítima: el pipeline sigue
        (y fallará después por el GPX inexistente, no por piexif)."""
        monkeypatch.setattr(vfe, "HAS_PIEXIF", False)
        ext = vfe.FrameExtractor("no_existe.mp4", "no_existe.gpx", str(tmp_path / "out"),
                                 inject_exif=False)
        resultado = []
        ext.on_done = lambda ok, msg: resultado.append((ok, msg))
        ext.run()
        assert resultado and resultado[0][0] is False
        assert "piexif" not in resultado[0][1].lower()


class TestConsoleUtf8:
    def test_auto_install_fallido_no_revienta_en_cp1252(self, tmp_path, monkeypatch):
        """Caso real (MSYS2 + consola cp1252): pip falla instalando imufusion y
        el print del error con '✗' moría con UnicodeEncodeError ANTES de abrir
        la app. auto_install debe reconfigurar la consola antes de imprimir."""
        import io
        import subprocess as sp
        buf = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="cp1252"))
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(vfe, "REQUIRED_PACKAGES", {"modulo_inexistente_xyz": "paquete-fantasma"})

        def pip_falla(cmd, **kw):
            raise sp.CalledProcessError(1, cmd)
        monkeypatch.setattr(vfe.subprocess, "check_call", pip_falla)

        assert vfe.auto_install_packages() is False   # no debe lanzar UnicodeEncodeError

    def test_instalacion_fallida_no_se_reintenta_en_cada_arranque(self, tmp_path, monkeypatch):
        """Caso real (MSYS2): imufusion no compila en MinGW y el reintento de
        pip en cada arranque costaba ~80s ANTES de abrir la GUI. Un paquete que
        ya falló en este intérprete se salta en arranques siguientes."""
        import subprocess as sp
        monkeypatch.setattr(vfe, "_PIP_FAILURE_CACHE", str(tmp_path / "fallos.json"))
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(vfe, "REQUIRED_PACKAGES", {"modulo_inexistente_xyz": "paquete-fantasma"})
        llamadas = []

        def pip_falla(cmd, **kw):
            llamadas.append(cmd)
            raise sp.CalledProcessError(1, cmd)
        monkeypatch.setattr(vfe.subprocess, "check_call", pip_falla)

        assert vfe.auto_install_packages() is False
        n_primer_arranque = len(llamadas)
        assert n_primer_arranque > 0, "el primer arranque debe intentar instalar"

        assert vfe.auto_install_packages() is False   # segundo arranque
        assert len(llamadas) == n_primer_arranque, "reintentó pip para un paquete ya fallido"

    def test_simbolos_no_revientan_en_consola_cp1252(self, monkeypatch):
        """Consolas Windows legacy (cp1252/cp850) no codifican ●✓✗█ — el CLI
        debe reconfigurar stdout para no morir en el primer log."""
        import io
        buf = io.BytesIO()
        fake_stdout = io.TextIOWrapper(buf, encoding="cp1252")
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        vfe.configure_console_utf8()
        print("  ● [12:00:00] ✓ █░ ✗ FALLÓ")   # no debe lanzar UnicodeEncodeError
        sys.stdout.flush()
        assert buf.getvalue()  # algo se escribió


# ═══════════════════════════════════════════════
# Gyro INSV: detección de formato por CONTENIDO, no solo por tamaño
# (los registros de la X5 son múltiplos de 280 → divisibles por 56 Y 20)
# ═══════════════════════════════════════════════
class TestInsvGyroFormatDetection:
    @staticmethod
    def _gyro_file(tmp_path, blob):
        p = tmp_path / "gyro.bin"
        p.write_bytes(blob)
        records = {vfe.INSV_REC_GYRO: {"size": len(blob), "offset": 0,
                                       "format": 0, "extra_start": 0}}
        return str(p), records

    def test_detecta_raw_20B_aunque_divisible_por_56(self, tmp_path):
        """Caso real X5 (archivo 026): interpretar el registro raw como float64
        daba duración de 9.2e12 s → rate 0 Hz → division by zero."""
        import struct as st
        n = 1400  # 28000 bytes: divisible por 56 y por 20
        blob = bytearray()
        for i in range(n):
            blob += st.pack("<Q", 1_000_000 + i * 1000)      # µs, 1 kHz
            blob += st.pack("<6H", 32768, 32768, 32768 + 4096,  # accel (0,0,1g) @8g
                            32768, 32768, 32768)                # gyro 0 °/s
        path, records = self._gyro_file(tmp_path, bytes(blob))
        imu = vfe.read_insv_gyro(path, records)
        assert imu is not None
        assert imu["n_samples"] == n
        assert 900 <= imu["sample_rate"] <= 1100

    def test_detecta_float_56B_valido(self, tmp_path):
        import struct as st
        n = 500  # 28000 bytes: también divisible por ambos
        blob = bytearray()
        for i in range(n):
            blob += st.pack("<Q", 2_000_000 + i * 1000)
            blob += st.pack("<6d", 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
        path, records = self._gyro_file(tmp_path, bytes(blob))
        imu = vfe.read_insv_gyro(path, records)
        assert imu is not None
        assert imu["n_samples"] == n
        assert 900 <= imu["sample_rate"] <= 1100

    def test_timestamps_congelados_retorna_none(self, tmp_path):
        """Duración 0 debe dar None (sin heading), nunca division by zero."""
        import struct as st
        blob = bytearray()
        for _ in range(100):  # 2000 bytes: solo divisible por 20
            blob += st.pack("<Q", 5_000_000)
            blob += st.pack("<6H", 32768, 32768, 36864, 32768, 32768, 32768)
        path, records = self._gyro_file(tmp_path, bytes(blob))
        assert vfe.read_insv_gyro(path, records) is None


class TestComputeImuHeadingGuards:
    def test_sample_rate_cero_no_divide_por_cero(self):
        import numpy as np
        imu = {"timestamps_ms": np.array([0.0, 0.0]), "accel": np.zeros((2, 3)),
               "gyro": np.zeros((2, 3)), "sample_rate": 0, "n_samples": 2}
        assert vfe.compute_imu_heading(imu, []) is None


# ═══════════════════════════════════════════════
# Prefijo de nombre de archivo: sanear caracteres ilegales de Windows
# (caso real: usuario escribió 'Ballenas 24"' → FFmpeg error -22)
# ═══════════════════════════════════════════════
class TestSanitizeFilenamePrefix:
    def test_elimina_comilla_doble_caso_real(self):
        """'Ballenas 24\"' (24 pulgadas) rompía FFmpeg en Windows."""
        assert vfe.sanitize_filename_prefix('Ballenas 24"') == 'Ballenas 24'

    def test_elimina_todos_los_ilegales_windows(self):
        assert vfe.sanitize_filename_prefix('a<b>c:d"e/f\\g|h?i*j') == 'abcdefghij'

    def test_prefijo_valido_intacto(self):
        assert vfe.sanitize_filename_prefix('FRAME') == 'FRAME'
        assert vfe.sanitize_filename_prefix('Ballenas_24') == 'Ballenas_24'

    def test_espacios_internos_se_conservan(self):
        assert vfe.sanitize_filename_prefix('Tanque 12') == 'Tanque 12'

    def test_recorta_espacios_y_puntos_finales(self):
        """Windows elimina espacios/puntos finales de los nombres."""
        assert vfe.sanitize_filename_prefix('Tanque.') == 'Tanque'
        assert vfe.sanitize_filename_prefix('  Tanque  ') == 'Tanque'

    def test_vacio_o_solo_ilegales_cae_a_FRAME(self):
        assert vfe.sanitize_filename_prefix('') == 'FRAME'
        assert vfe.sanitize_filename_prefix('"') == 'FRAME'
        assert vfe.sanitize_filename_prefix('///') == 'FRAME'
        assert vfe.sanitize_filename_prefix(None) == 'FRAME'

    def test_extractor_sanitiza_el_prefijo(self):
        """FrameExtractor guarda el prefijo ya saneado (usado en el patrón FFmpeg)."""
        ext = vfe.FrameExtractor('v.mp4', 'g.gpx', 'out', prefix='Ballenas 24"')
        assert ext.prefix == 'Ballenas 24'
        assert '"' not in ext.prefix
        assert ext._prefix_input == 'Ballenas 24"'  # se recuerda el original para avisar


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
