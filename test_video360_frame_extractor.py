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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
