"""EXIF-based rule for image folders.

The trip-photos signature is: every sampled photo has GPS coords clustered
within a small radius and the EXIF date span is narrow (<=2 weeks). When
that holds we can confidently call a folder a single trip without sending
any pixels to the LLM.

Implementation notes
- ctx.sample_exif is populated upstream (by the sampler / Phase A); this
  rule does not parse EXIF itself. If sample_exif is empty we bail.
- We are tolerant of partial EXIF: a sample where some images lack GPS but
  the rest cluster tightly is still a trip. We require >=3 GPS-bearing
  samples to draw the conclusion (one or two could be coincidence).
- GPS distance is great-circle (haversine), in km.
- Pillow / exifread are not imported here; this rule operates on already-
  parsed dicts. That keeps it cheap and testable without image fixtures.
"""

from __future__ import annotations

import math
from datetime import datetime

from .base import Classification, Rule, UnitContext

IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".png", ".raw", ".dng", ".cr2", ".nef"}

GPS_CLUSTER_KM = 50.0  # max great-circle distance between sample photos
DATE_SPAN_DAYS = 14  # max EXIF date span for "single trip"
MIN_GPS_SAMPLES = 3  # don't draw conclusions from one or two photos


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two GPS coords, in kilometres."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _extract_gps(exif: dict) -> tuple[float, float] | None:
    """Pull (lat, lon) from a parsed EXIF dict.

    Accepts either Pillow's nested {'GPSInfo': {...}} shape or a flat
    {'gps_latitude': ..., 'gps_longitude': ...} shape. Returns None when
    no usable coords are present.
    """
    if not exif:
        return None
    # Flat shape — already-decoded floats.
    if "gps_latitude" in exif and "gps_longitude" in exif:
        try:
            return float(exif["gps_latitude"]), float(exif["gps_longitude"])
        except (TypeError, ValueError):
            return None
    # Pillow nested shape — GPSInfo dict with rationals.
    gps = exif.get("GPSInfo")
    if isinstance(gps, dict):
        lat = gps.get("lat") or gps.get(2)
        lon = gps.get("lon") or gps.get(4)
        lat_ref = gps.get("lat_ref") or gps.get(1) or "N"
        lon_ref = gps.get("lon_ref") or gps.get(3) or "E"
        try:
            if lat is None or lon is None:
                return None
            lat_f = float(lat)
            lon_f = float(lon)
            if isinstance(lat_ref, str) and lat_ref.upper() == "S":
                lat_f = -lat_f
            if isinstance(lon_ref, str) and lon_ref.upper() == "W":
                lon_f = -lon_f
            return lat_f, lon_f
        except (TypeError, ValueError):
            return None
    return None


def _extract_datetime(exif: dict) -> datetime | None:
    """Pull a datetime from EXIF — DateTimeOriginal preferred, else DateTime."""
    if not exif:
        return None
    raw = exif.get("DateTimeOriginal") or exif.get("DateTime") or exif.get("datetime")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    # EXIF spec: "YYYY:MM:DD HH:MM:SS"
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(raw), fmt)
        except ValueError:
            continue
    return None


def _is_image_unit(ctx: UnitContext) -> bool:
    if ctx.kind != "folder":
        return False
    if not ctx.extensions:
        return False
    total = sum(ctx.extensions.values())
    if total == 0:
        return False
    image_count = sum(c for ext, c in ctx.extensions.items() if ext.lower() in IMAGE_EXTS)
    return image_count / total >= 0.85


class ExifTripRule(Rule):
    name = "by_exif"

    def applies(self, ctx: UnitContext) -> bool:
        # Skip non-image folders entirely. Skip if Phase A didn't manage to
        # parse any EXIF — that's how we degrade gracefully when Pillow /
        # exifread couldn't read the formats.
        return _is_image_unit(ctx) and bool(ctx.sample_exif)

    def evaluate(self, ctx: UnitContext) -> Classification | None:
        gps_points: list[tuple[str, float, float]] = []
        timestamps: list[tuple[str, datetime]] = []

        for path, exif in ctx.sample_exif.items():
            coords = _extract_gps(exif)
            if coords is not None:
                gps_points.append((str(path), coords[0], coords[1]))
            ts = _extract_datetime(exif)
            if ts is not None:
                timestamps.append((str(path), ts))

        if len(gps_points) < MIN_GPS_SAMPLES:
            return None

        # Centroid + max distance from centroid (cheaper than all-pairs).
        avg_lat = sum(p[1] for p in gps_points) / len(gps_points)
        avg_lon = sum(p[2] for p in gps_points) / len(gps_points)
        max_dist = max(_haversine_km(avg_lat, avg_lon, lat, lon) for _, lat, lon in gps_points)
        if max_dist > GPS_CLUSTER_KM:
            return None

        # Date span check — if we have timestamps, enforce the window.
        date_span_days: float | None = None
        if timestamps:
            earliest = min(ts for _, ts in timestamps)
            latest = max(ts for _, ts in timestamps)
            date_span_days = (latest - earliest).total_seconds() / 86400.0
            if date_span_days > DATE_SPAN_DAYS:
                return None

        evidence: list[tuple[str, str]] = [
            (
                "exif:GPSInfo",
                f"{len(gps_points)} samples cluster within {max_dist:.1f} km of centroid "
                f"({avg_lat:.4f}, {avg_lon:.4f})",
            ),
        ]
        if date_span_days is not None and timestamps:
            earliest = min(ts for _, ts in timestamps)
            latest = max(ts for _, ts in timestamps)
            evidence.append(
                (
                    "exif:DateTimeOriginal",
                    f"date span {date_span_days:.1f} days "
                    f"({earliest.date()} to {latest.date()}) across {len(timestamps)} samples",
                )
            )
        # Cite the specific photos so the user can audit.
        for path, lat, lon in gps_points[:3]:
            evidence.append((path, f"GPS ({lat:.4f}, {lon:.4f})"))

        return Classification(
            rule_name=self.name,
            confidence=0.93,
            class_id="trip-photos",
            evidence=evidence,
        )
