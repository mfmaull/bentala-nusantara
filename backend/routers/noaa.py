"""NOAA/Open-Meteo hazard aggregation for Bentala Nusantara.

The public api.weather.gov/NWS alert and grid products are geospatial, but their
operational coverage is the United States and territories. For Indonesia-wide
coverage this router uses Open-Meteo forecast variables backed by NOAA/NCEP
models where available, then aggregates the signal into Indonesian GADM
administrative areas.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional, Set, Tuple

import httpx
import numpy as np
import rasterio
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image
from rasterio.features import geometry_mask, rasterize
from rasterio.windows import Window
from rasterio.windows import transform as compute_window_transform
from rasterio.transform import Affine
from shapely.geometry import Point, mapping, shape
from shapely.ops import unary_union

router = APIRouter()
logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "BentalaNusantara/1.0 (early-warning-dashboard)"
FORECAST_TIMEZONE = "Asia/Jakarta"
CACHE_MAX_AGE = timedelta(minutes=60)
# Batch size reduced to 20 to avoid overly large payloads to Open-Meteo
BATCH_SIZE = 20
# Max 1 concurrent request to comply with Open-Meteo rate limit (1 concurrent per IP)
MAX_CONCURRENT_BATCHES = 1
# Retry config for rate limiting
FORECAST_RETRY_ATTEMPTS = 3
FORECAST_RETRY_BASE_DELAY = 1.0  # seconds
COMPOSITE_BATCH_SIZE = 10
COMPOSITE_REQUEST_DELAY = 0.3
COMPOSITE_RETRY_ATTEMPTS = 2
COMPOSITE_MAX_KABUPATEN = 5

INDONESIA_LAT_MIN = -12.0
INDONESIA_LAT_MAX = 7.0
INDONESIA_LON_MIN = 94.0
INDONESIA_LON_MAX = 142.0

THRESHOLDS = {
    "soil_moisture": {"medium": 0.35, "high": 0.42},
    "soil_moisture_mid": {"medium": 0.30, "high": 0.38},
    "soil_moisture_deep": {"medium": 0.25, "high": 0.32},
    "rainfall_3day": {"medium": 75.0, "high": 150.0},
    "rainfall_intensity": {"medium": 10.0, "high": 20.0},
    "cape": {"medium": 500.0, "high": 1500.0},
    "wind_gusts": {"medium": 15.0, "high": 25.0},
    "glofas_flood_prob": {"medium": 0.20, "high": 0.50},
    "usgs_nowcast": {"medium": 0.30, "high": 0.60},
}

RISK_MODEL = {
    "LOW": {
        "level": 1,
        "label": "Low Risk",
        "color": "#4ade80",
        "recommendation": "Kondisi relatif terkendali. Tetap pantau pembaruan prakiraan.",
    },
    "MEDIUM": {
        "level": 2,
        "label": "Medium Risk",
        "color": "#facc15",
        "recommendation": "Waspada peningkatan risiko. Periksa drainase, lereng, dan area rawan.",
    },
    "HIGH": {
        "level": 3,
        "label": "High Risk",
        "color": "#ef4444",
        "recommendation": "Siaga. Siapkan tindakan lapangan dan komunikasi publik untuk wilayah rawan.",
    },
}

TERRAIN_WEIGHT = {
    ("steep", "high"): 1.8,
    ("steep", "medium"): 1.5,
    ("moderate", "high"): 1.4,
    ("moderate", "medium"): 1.2,
    ("gentle", "high"): 1.1,
    ("gentle", "medium"): 1.0,
    ("flat", "high"): 0.9,
    ("flat", "medium"): 0.8,
    ("flat", "low"): 0.7,
}

ELEVATION_RAMP = [
    (0,    (20,  100, 20)),    # hijau tua       — sea level
    (100,  (50,  150, 50)),    # hijau            — dataran rendah
    (300,  (120, 180, 60)),    # hijau muda       — bukit rendah
    (600,  (180, 200, 80)),    # kuning hijau     — bukit
    (1000, (220, 200, 80)),    # kuning            — pegunungan rendah
    (1500, (200, 150, 60)),    # coklat muda      — pegunungan
    (2000, (180, 100, 50)),    # coklat            — pegunungan tinggi
    (3000, (160, 60,  40)),    # coklat tua        — sangat tinggi
    (5000, (200, 200, 210)),   # abu/putih         — puncak salju
]

# ========== PATH CONFIG ==========
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
GADM_CANDIDATES = [
    os.path.join(PROJECT_ROOT, "assets", "json", "gadm36_IDN_2.json"),
    os.path.join(BACKEND_ROOT, "assets", "json", "gadm36_IDN_2.json"),
    "/app/assets/json/gadm36_IDN_2.json",
]
ASSETS_DIR = os.path.join(BACKEND_ROOT, "assets", "json")
SRTM_PATH = os.path.join(ASSETS_DIR, "srtm_indonesia.tif")
BNPB_PATH = os.path.join(ASSETS_DIR, "bnpb_susceptibility.tif")
USGS_PATH = os.path.join(ASSETS_DIR, "usgs_nowcast.tif")

# ========== GLOBAL CACHE ==========
_admin_cache: Dict[str, Any] = {"features": None, "loaded_at": None}
_hazard_cache: Dict[str, Dict[str, Any]] = {}
_terrain_cache: Dict[str, Any] = {
    "elevation": None,
    "slope": None,
    "hillshade_png": None,
    "hillshade_bbox": None,
    "srtm_loaded": False,
    "bnpb_loaded": False,
    "usgs_loaded": False,
}
_bmkg_alert_cache: Dict[str, Any] = {"data": None, "fetched_at": None}
# Per-kabupaten forecast cache with TTL 60 minutes to reduce API calls
_forecast_cache: Dict[str, Dict[str, Any]] = {}
_forecast_cache_lock = asyncio.Lock()
_lock = asyncio.Lock()
_lock_terrain = asyncio.Lock()
_lock_bmkg = asyncio.Lock()


def _is_valid_indonesia_coordinate(lat: float, lon: float) -> bool:
    return (
        INDONESIA_LAT_MIN <= lat <= INDONESIA_LAT_MAX
        and INDONESIA_LON_MIN <= lon <= INDONESIA_LON_MAX
    )


def _find_gadm_path() -> str:
    for path in GADM_CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("GADM boundary file not found in known asset locations")


def _load_admin_features() -> List[Dict[str, Any]]:
    if _admin_cache["features"] is not None:
        return _admin_cache["features"]

    gadm_path = _find_gadm_path()
    logger.info("[NOAA] Loading GADM boundaries from %s", gadm_path)
    with open(gadm_path, encoding="utf-8") as file:
        geojson = json.load(file)

    features: List[Dict[str, Any]] = []
    for index, feature in enumerate(geojson.get("features", [])):
        props = feature.get("properties") or {}
        geom = feature.get("geometry")
        if not geom:
            continue
        try:
            shp = shape(geom)
        except Exception:
            logger.warning("[NOAA] Invalid geometry skipped: %s", props.get("GID_2"))
            continue
        if shp.is_empty or not shp.is_valid:
            continue

        point = shp.representative_point()
        province_id = props.get("GID_1") or props.get("NAME_1") or "UNKNOWN"
        region_id = props.get("GID_2") or f"IDN.ADM2.{index}"
        features.append(
            {
                "id": region_id,
                "province_id": province_id,
                "province_name": props.get("NAME_1") or "Tidak diketahui",
                "name": props.get("NAME_2") or "Tidak diketahui",
                "type": props.get("TYPE_2") or props.get("ENGTYPE_2") or "Kabupaten/Kota",
                "geometry": shp,
                "representative_lat": point.y,
                "representative_lon": point.x,
            }
        )

    _admin_cache["features"] = features
    _admin_cache["loaded_at"] = datetime.now(timezone.utc)
    logger.info("[NOAA] Loaded %s administrative features", len(features))
    return features


def _iter_batches(items: List[Dict[str, Any]], batch_size: int) -> Iterable[List[Dict[str, Any]]]:
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


# ========== BATCH 1: EXTENDED OPEN-METEO ==========

async def _fetch_forecast_batch(
    client: httpx.AsyncClient,
    batch: List[Dict[str, Any]],
    forecast_days: int,
) -> List[Optional[Dict[str, Any]]]:
    """Fetch one batch from Open-Meteo with retry + exponential backoff on 429/5xx."""
    params = {
        "latitude": ",".join(f"{item['representative_lat']:.5f}" for item in batch),
        "longitude": ",".join(f"{item['representative_lon']:.5f}" for item in batch),
        "hourly": "soil_moisture_0_to_7cm,soil_moisture_7_to_28cm,soil_moisture_28_to_100cm,rain,precipitation_probability,cape,wind_gusts_10m",
        "forecast_days": forecast_days,
        "timezone": FORECAST_TIMEZONE,
    }
    last_exc = None
    for attempt in range(1, FORECAST_RETRY_ATTEMPTS + 1):
        try:
            response = await client.get(OPEN_METEO_URL, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < FORECAST_RETRY_ATTEMPTS:
                    delay = FORECAST_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning("[NOAA] Batch retry %s/%s after HTTP %s, waiting %.1fs",
                                   attempt, FORECAST_RETRY_ATTEMPTS, response.status_code, delay)
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
            response.raise_for_status()
            payload = response.json()
            rows = payload if isinstance(payload, list) else [payload]
            if len(rows) != len(batch):
                logger.warning("[NOAA] Batch response size mismatch: expected %s got %s", len(batch), len(rows))
            return [_extract_forecast_metrics(row) if isinstance(row, dict) else None for row in rows]
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            last_exc = exc
            if attempt < FORECAST_RETRY_ATTEMPTS:
                delay = FORECAST_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("[NOAA] Batch retry %s/%s after error: %s, waiting %.1fs",
                               attempt, FORECAST_RETRY_ATTEMPTS, exc, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("[NOAA] Batch failed after %s attempts: %s", FORECAST_RETRY_ATTEMPTS, exc)
    return [None] * len(batch)


def _extract_forecast_metrics(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    soil_shallow = hourly.get("soil_moisture_0_to_7cm") or []
    soil_mid = hourly.get("soil_moisture_7_to_28cm") or []
    soil_deep = hourly.get("soil_moisture_28_to_100cm") or []
    rain = hourly.get("rain") or []
    probability = hourly.get("precipitation_probability") or []
    cape = hourly.get("cape") or []
    wind_gusts = hourly.get("wind_gusts_10m") or []
    hours = min(len(times), len(soil_shallow), len(rain), 72)
    if hours <= 0:
        return None

    soil_shallow_values = [float(v) for v in soil_shallow[:hours] if v is not None]
    soil_mid_values = [float(v) for v in soil_mid[:hours] if v is not None]
    soil_deep_values = [float(v) for v in soil_deep[:hours] if v is not None]
    rain_values = [float(v) for v in rain[:hours] if v is not None]
    probability_values = [float(v) for v in probability[:hours] if v is not None]
    cape_values = [float(v) for v in cape[:hours] if v is not None]
    wind_gusts_values = [float(v) for v in wind_gusts[:hours] if v is not None]

    if not soil_shallow_values and not rain_values:
        return None

    soil_shallow_max = max(soil_shallow_values) if soil_shallow_values else 0.0
    soil_mid_max = max(soil_mid_values) if soil_mid_values else 0.0
    soil_deep_max = max(soil_deep_values) if soil_deep_values else 0.0
    rain_3day = sum(rain_values) if rain_values else 0.0
    rain_intensity = max(rain_values) if rain_values else 0.0
    probability_max = max(probability_values) if probability_values else None
    cape_max = max(cape_values) if cape_values else 0.0
    wind_gusts_max = max(wind_gusts_values) if wind_gusts_values else 0.0

    risk_level, risk_score, drivers = _calculate_risk(
        soil_shallow_max, rain_3day, rain_intensity, probability_max,
        soil_mid_max, soil_deep_max, cape_max, wind_gusts_max,
    )

    return {
        "soil_moisture_max_3d": round(soil_shallow_max, 4),
        "soil_moisture_mid_max_3d": round(soil_mid_max, 4),
        "soil_moisture_deep_max_3d": round(soil_deep_max, 4),
        "rainfall_3day_mm": round(rain_3day, 1),
        "rainfall_intensity_max_mm_per_hour": round(rain_intensity, 1),
        "precipitation_probability_max": round(probability_max, 1)
        if probability_max is not None
        else None,
        "cape_max": round(cape_max, 1),
        "wind_gusts_max_ms": round(wind_gusts_max, 1),
        "risk_level": risk_level,
        "risk_level_numeric": RISK_MODEL[risk_level]["level"],
        "risk_score": round(risk_score, 2),
        "risk_drivers": drivers,
        "recommendation": RISK_MODEL[risk_level]["recommendation"],
        "forecast_hours": hours,
    }


def _calculate_risk(
    soil_moisture: float,
    rainfall_3day: float,
    rainfall_intensity: float,
    precipitation_probability: Optional[float],
    soil_moisture_mid: float = 0.0,
    soil_moisture_deep: float = 0.0,
    cape: float = 0.0,
    wind_gusts: float = 0.0,
    glofas_flood_prob: Optional[float] = None,
    usgs_nowcast: Optional[float] = None,
    bmkg_alert_boost: float = 1.0,
    terrain_multiplier: float = 1.0,
    bnpb_susceptibility_score: float = 0.5,
) -> Tuple[str, float, List[str]]:
    score = 0.0
    drivers: List[str] = []

    if soil_moisture >= THRESHOLDS["soil_moisture"]["high"]:
        score += 2.0
        drivers.append("Kelembaban tanah dangkal sangat tinggi")
    elif soil_moisture >= THRESHOLDS["soil_moisture"]["medium"]:
        score += 1.0
        drivers.append("Kelembaban tanah dangkal meningkat")

    if soil_moisture_mid >= THRESHOLDS["soil_moisture_mid"]["high"]:
        score += 1.5
        drivers.append("Kelembaban tanah lapisan tengah sangat tinggi")
    elif soil_moisture_mid >= THRESHOLDS["soil_moisture_mid"]["medium"]:
        score += 0.8
        drivers.append("Kelembaban tanah lapisan tengah meningkat")

    if soil_moisture_deep >= THRESHOLDS["soil_moisture_deep"]["high"]:
        score += 1.0
        drivers.append("Kelembaban tanah lapisan dalam sangat tinggi")
    elif soil_moisture_deep >= THRESHOLDS["soil_moisture_deep"]["medium"]:
        score += 0.5
        drivers.append("Kelembaban tanah lapisan dalam meningkat")

    if rainfall_3day >= THRESHOLDS["rainfall_3day"]["high"]:
        score += 2.0
        drivers.append("Akumulasi hujan 3 hari tinggi")
    elif rainfall_3day >= THRESHOLDS["rainfall_3day"]["medium"]:
        score += 1.0
        drivers.append("Akumulasi hujan 3 hari sedang")

    if rainfall_intensity >= THRESHOLDS["rainfall_intensity"]["high"]:
        score += 2.0
        drivers.append("Intensitas hujan per jam tinggi")
    elif rainfall_intensity >= THRESHOLDS["rainfall_intensity"]["medium"]:
        score += 1.0
        drivers.append("Intensitas hujan per jam sedang")

    if precipitation_probability is not None and precipitation_probability >= 80:
        score += 0.5
        drivers.append("Peluang presipitasi tinggi")

    if cape >= THRESHOLDS["cape"]["high"]:
        score += 1.5
        drivers.append("Potensi konvektif ekstrem (CAPE tinggi)")
    elif cape >= THRESHOLDS["cape"]["medium"]:
        score += 0.8
        drivers.append("Potensi badai petir (CAPE sedang)")

    if wind_gusts >= THRESHOLDS["wind_gusts"]["high"]:
        score += 1.0
        drivers.append("Angin gusts ekstrem")
    elif wind_gusts >= THRESHOLDS["wind_gusts"]["medium"]:
        score += 0.5
        drivers.append("Angin gusts cukup kencang")

    # BNPB susceptibility base contribution
    if bnpb_susceptibility_score >= 0.7:
        score += 1.0
        drivers.append("Wilayah dengan kerawanan longsor tinggi (BNPB)")
    elif bnpb_susceptibility_score >= 0.4:
        score += 0.5
        drivers.append("Wilayah dengan kerawanan longsor sedang (BNPB)")

    # USGS nowcast boost
    if usgs_nowcast is not None and usgs_nowcast >= THRESHOLDS["usgs_nowcast"]["high"]:
        score += 1.0
        drivers.append("Sinyal nowcast longsor USGS tinggi")
    elif usgs_nowcast is not None and usgs_nowcast >= THRESHOLDS["usgs_nowcast"]["medium"]:
        score += 0.5
        drivers.append("Sinyal nowcast longsor USGS sedang")

    # GloFAS flood boost
    if glofas_flood_prob is not None and glofas_flood_prob >= THRESHOLDS["glofas_flood_prob"]["high"]:
        score += 1.0
        drivers.append("Probabilitas banjir GloFAS tinggi")
    elif glofas_flood_prob is not None and glofas_flood_prob >= THRESHOLDS["glofas_flood_prob"]["medium"]:
        score += 0.5
        drivers.append("Probabilitas banjir GloFAS sedang")

    # BMKG alert boost
    if bmkg_alert_boost > 1.0:
        score += 1.5
        drivers.append("Peringatan dini BMKG aktif di wilayah ini")

    # Terrain multiplier
    base_score = score
    score = base_score * terrain_multiplier

    if score >= 3.5:
        return "HIGH", score, drivers or ["Parameter cuaca menunjukkan risiko tinggi"]
    if score >= 1.5:
        return "MEDIUM", score, drivers or ["Parameter cuaca menunjukkan risiko sedang"]
    return "LOW", score, drivers or ["Parameter cuaca berada pada batas rendah"]


# ========== BATCH 2: SRTM TERRAIN ENGINE ==========

def _load_srtm_raster() -> Optional[Any]:
    """Load SRTM raster, return dataset or None if file missing."""
    if not os.path.exists(SRTM_PATH):
        logger.warning("[NOAA] SRTM file not found: %s — terrain features disabled", SRTM_PATH)
        return None
    try:
        src = rasterio.open(SRTM_PATH)
        logger.info("[NOAA] SRTM loaded: %s x %s, crs=%s", src.width, src.height, src.crs)
        return src
    except Exception as exc:
        logger.warning("[NOAA] Failed to load SRTM: %s", exc)
        return None


def _compute_hillshade(elevation: np.ndarray, azimuth: float = 315, zenith: float = 45) -> np.ndarray:
    """Compute hillshade from elevation array. Returns uint8 [0-255]."""
    zenith_rad = np.radians(zenith)
    azimuth_rad = np.radians(azimuth)
    dy, dx = np.gradient(elevation.astype(np.float64))
    slope = np.arctan(np.sqrt(dx**2 + dy**2))
    aspect = np.arctan2(-dy, dx)
    hillshade = (
        np.cos(zenith_rad) * np.cos(slope)
        + np.sin(zenith_rad) * np.sin(slope) * np.cos(azimuth_rad - aspect)
    )
    hillshade = np.clip(hillshade, 0, 1)
    return (hillshade * 255).astype(np.uint8)


def _elevation_to_color(elev_array: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized elevation-to-color via np.interp using ELEVATION_RAMP.
    Returns (R, G, B) uint8 arrays."""
    elevations = [e[0] for e in ELEVATION_RAMP]
    r_vals = [e[1][0] for e in ELEVATION_RAMP]
    g_vals = [e[1][1] for e in ELEVATION_RAMP]
    b_vals = [e[1][2] for e in ELEVATION_RAMP]
    r = np.interp(elev_array, elevations, r_vals).astype(np.uint8)
    g = np.interp(elev_array, elevations, g_vals).astype(np.uint8)
    b = np.interp(elev_array, elevations, b_vals).astype(np.uint8)
    return r, g, b


def _elevation_color_value(elev_m: float) -> Tuple[int, int, int, int]:
    """Scalar fallback — interpolates using ELEVATION_RAMP."""
    elevations = [e[0] for e in ELEVATION_RAMP]
    r_vals = [e[1][0] for e in ELEVATION_RAMP]
    g_vals = [e[1][1] for e in ELEVATION_RAMP]
    b_vals = [e[1][2] for e in ELEVATION_RAMP]
    # Scalar interpolation fallback
    arr = np.array([elev_m], dtype=np.float64)
    r = int(np.interp(arr, elevations, r_vals)[0])
    g = int(np.interp(arr, elevations, g_vals)[0])
    b = int(np.interp(arr, elevations, b_vals)[0])
    return (r, g, b, 220)


def _compute_elevation_slope_lookup() -> None:
    """Compute elevation + slope lookup table from SRTM for all GADM admin areas."""
    src = _load_srtm_raster()
    if src is None:
        _terrain_cache["srtm_loaded"] = False
        return

    admin_features = _load_admin_features()
    elev_lookup: Dict[str, float] = {}
    slope_lookup: Dict[str, Dict[str, Any]] = {}

    for admin in admin_features:
        aid = admin["id"]
        lat, lon = admin["representative_lat"], admin["representative_lon"]
        try:
            row, col = src.index(lon, lat)
            if 0 <= row < src.height and 0 <= col < src.width:
                data = src.read(1, window=Window(col, row, 1, 1))
                elev = float(data[0, 0])
                nodata = src.nodata
                if (nodata is not None and elev == nodata) or elev <= -9999:
                    elev_lookup[aid] = 0.0
                    slope_lookup[aid] = {"slope_deg": 0.0, "slope_class": "flat"}
                else:
                    elev_lookup[aid] = elev
            else:
                elev_lookup[aid] = 0.0
                slope_lookup[aid] = {"slope_deg": 0.0, "slope_class": "flat"}
        except Exception:
            elev_lookup[aid] = 0.0
            slope_lookup[aid] = {"slope_deg": 0.0, "slope_class": "flat"}

    # Compute slope per admin using bounding box window
    # CRITICAL: numpy gradient operates in pixel units (degrees lon/lat).
    # Convert to meters before computing slope:
    #   1 deg latitude  ≈ 110,540 m
    #   1 deg longitude ≈ 111,320 * cos(lat_center) m
    pixel_size_deg = src.transform.a  # degrees per pixel (E-W)
    center_lat_admin = {}
    for admin in admin_features:
        center_lat_admin[admin["id"]] = admin["geometry"].centroid.y

    for admin in admin_features:
        aid = admin["id"]
        bounds = admin["geometry"].bounds
        try:
            row_min, col_min = src.index(bounds[0], bounds[3])
            row_max, col_max = src.index(bounds[2], bounds[1])
            row_min, row_max = max(0, min(row_min, row_max)), min(src.height, max(row_min, row_max))
            col_min, col_max = max(0, min(col_min, col_max)), min(src.width, max(col_min, col_max))
            if row_max - row_min < 2 or col_max - col_min < 2:
                slope_lookup[aid] = {"slope_deg": 0.0, "slope_class": "flat"}
                continue
            window = Window(col_min, row_min, col_max - col_min, row_max - row_min)
            elev_block = src.read(1, window=window).astype(np.float64)
            if elev_block.size < 4:
                slope_lookup[aid] = {"slope_deg": 0.0, "slope_class": "flat"}
                continue

            # Convert gradient from degree-pixels to meters
            center_lat = center_lat_admin.get(aid, -2.5)
            lat_rad = np.radians(center_lat)
            cell_size_y = 110540.0 * pixel_size_deg
            cell_size_x = 111320.0 * np.cos(lat_rad) * pixel_size_deg

            dy, dx = np.gradient(elev_block)
            dy_m = dy / cell_size_y
            dx_m = dx / cell_size_x

            slope_rad = np.arctan(np.sqrt(dx_m**2 + dy_m**2))
            slope_deg = min(float(np.mean(slope_rad) * 180.0 / np.pi), 89.0)
            if slope_deg < 5:
                slope_class = "flat"
            elif slope_deg < 15:
                slope_class = "gentle"
            elif slope_deg < 30:
                slope_class = "moderate"
            else:
                slope_class = "steep"
            slope_lookup[aid] = {"slope_deg": round(slope_deg, 1), "slope_class": slope_class}
        except Exception:
            slope_lookup[aid] = {"slope_deg": 0.0, "slope_class": "flat"}

    src.close()
    _terrain_cache["elevation"] = elev_lookup
    _terrain_cache["slope"] = slope_lookup
    _terrain_cache["srtm_loaded"] = True
    logger.info("[NOAA] Terrain lookup computed: %s elevations, %s slopes", len(elev_lookup), len(slope_lookup))


def generate_hillshade_image() -> None:
    """Generate hillshade PNG from SRTM with land mask and elevation color ramp.
    
    Performs:
    1. Downsample to max 2000px
    2. Compute hillshade from elevation
    3. Map elevation to color via ELEVATION_RAMP (smooth np.interp)
    4. Multiply-blend hillshade shadow (70%) + ambient light (30%)
    5. Clip to GADM land polygons (ocean = transparent alpha=0)
    6. Output RGBA PNG
    """
    global _terrain_cache
    src = _load_srtm_raster()
    if src is None:
        _terrain_cache["hillshade_png"] = None
        _terrain_cache["hillshade_bbox"] = None
        return

    try:
        data = src.read(1)
        height, width = data.shape
        bounds = src.bounds
        bbox = [bounds[0], bounds[3], bounds[2], bounds[1]]
        transform = src.transform
        MAX_SIZE = 2000

        if width <= MAX_SIZE and height <= MAX_SIZE:
            downsampled = data
            h, w = height, width
            stride_y = stride_x = 1
        else:
            if width > height:
                new_width = MAX_SIZE
                new_height = int(height * MAX_SIZE / width)
            else:
                new_height = MAX_SIZE
                new_width = int(width * MAX_SIZE / height)
            stride_y = max(1, height // new_height)
            stride_x = max(1, width // new_width)
            downsampled = data[::stride_y, ::stride_x][:new_height, :new_width]
            h, w = downsampled.shape

        # Replace NoData with 0
        nodata = src.nodata
        if nodata is not None:
            downsampled = np.where(downsampled == nodata, 0, downsampled)
        downsampled = np.where(downsampled < -999, 0, downsampled)
        elev_clipped = np.clip(downsampled, 0, None)  # negative → 0

        # 1. Land mask from GADM boundaries
        try:
            admin_features = _load_admin_features()
            land_geoms = [admin["geometry"] for admin in admin_features if admin["geometry"] is not None]
            if land_geoms:
                land_union = unary_union(land_geoms)
                # Match the downsampled grid; need adjusted transform
                if w <= 0 or h <= 0:
                    raise ValueError(f"Invalid window size: w={w}, h={h}")
                _win = rasterio.windows.Window(0, 0, w, h)
                win_transform = compute_window_transform(_win, transform)
                logger.info("[COMPOSITE] Window size: %s x %s, transform: %s", w, h, transform)
                land_mask = geometry_mask(
                    [land_union] if land_union.geom_type != "MultiPolygon" else list(land_union.geoms),
                    transform=win_transform,
                    invert=False,
                    out_shape=(h, w),
                )
            else:
                land_mask = np.zeros((h, w), dtype=bool)
        except Exception as exc:
            logger.warning("[NOAA] Land mask failed, using NoData fallback: %s", exc)
            land_mask = (elev_clipped <= 0)

        # 2. Hillshade
        hillshade = _compute_hillshade(downsampled)
        hs_norm = hillshade.astype(np.float64) / 255.0  # [0,1]

        # 3. Elevation color ramp (vectorized)
        r, g, b = _elevation_to_color(elev_clipped)

        # 4. Multiply blend: color = base * (hs * 0.7 + 0.3)
        AMBIENT = 0.3
        blend = hs_norm * (1.0 - AMBIENT) + AMBIENT
        r_blended = (r.astype(np.float64) * blend).astype(np.uint8)
        g_blended = (g.astype(np.float64) * blend).astype(np.uint8)
        b_blended = (b.astype(np.float64) * blend).astype(np.uint8)

        # 5. Alpha: land = 220, ocean = 0 (transparent)
        alpha = np.where(land_mask, 0, 220).astype(np.uint8)

        # 6. Compose RGBA
        img_array = np.stack([r_blended, g_blended, b_blended, alpha], axis=-1)

        img = Image.fromarray(img_array, "RGBA")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        _terrain_cache["hillshade_png"] = buffer.getvalue()
        _terrain_cache["hillshade_bbox"] = bbox
        logger.info("[NOAA] Hillshade PNG generated: %s x %s (land masked)", w, h)
    except Exception as exc:
        logger.warning("[NOAA] Failed to generate hillshade: %s", exc)
        _terrain_cache["hillshade_png"] = None
        _terrain_cache["hillshade_bbox"] = None
    finally:
        src.close()


def _get_terrain_info(admin_id: str) -> Dict[str, Any]:
    """Get elevation + slope for an admin area from cache."""
    elev = _terrain_cache.get("elevation", {})
    slope = _terrain_cache.get("slope", {})
    e = elev.get(admin_id, 0.0) if elev else 0.0
    s = slope.get(admin_id, {"slope_deg": 0.0, "slope_class": "flat"}) if slope else {"slope_deg": 0.0, "slope_class": "flat"}
    return {"elevation_m": round(e, 1), "slope_deg": s.get("slope_deg", 0.0), "slope_class": s.get("slope_class", "flat")}


# ========== BATCH 3: BNPB SUSCEPTIBILITY ==========

def _load_bnpb_susceptibility() -> Dict[str, Dict[str, Any]]:
    """Load BNPB susceptibility raster → lookup per admin area. Graceful fallback if file missing."""
    bnpb_lookup: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(BNPB_PATH):
        logger.warning("[NOAA] BNPB susceptibility file not found: %s", BNPB_PATH)
        _terrain_cache["bnpb_loaded"] = False
        return bnpb_lookup

    try:
        with rasterio.open(BNPB_PATH) as src:
            admin_features = _load_admin_features()
            for admin in admin_features:
                aid = admin["id"]
                lat, lon = admin["representative_lat"], admin["representative_lon"]
                try:
                    row, col = src.index(lon, lat)
                    if 0 <= row < src.height and 0 <= col < src.width:
                        data = src.read(1, window=Window(col, row, 1, 1))
                        val = float(data[0, 0])
                        nodata = src.nodata
                        if (nodata is not None and val == nodata) or val <= 0:
                            bnpb_lookup[aid] = {"score": 0.5, "class": "medium"}
                        else:
                            # Normalize: assume input 1-5 scale or 0-1
                            if val > 1.0:
                                score = min(val / 5.0, 1.0)
                            else:
                                score = min(val, 1.0)
                            if score >= 0.7:
                                cls = "high"
                            elif score >= 0.4:
                                cls = "medium"
                            else:
                                cls = "low"
                            bnpb_lookup[aid] = {"score": round(score, 2), "class": cls}
                    else:
                        bnpb_lookup[aid] = {"score": 0.5, "class": "medium"}
                except Exception:
                    bnpb_lookup[aid] = {"score": 0.5, "class": "medium"}
        _terrain_cache["bnpb_loaded"] = True
        logger.info("[NOAA] BNPB susceptibility loaded: %s entries", len(bnpb_lookup))
    except Exception as exc:
        logger.warning("[NOAA] Failed to load BNPB: %s", exc)
        _terrain_cache["bnpb_loaded"] = False

    return bnpb_lookup


# ========== BATCH 3: USGS NOWCAST ==========

def _load_usgs_nowcast_raster() -> Dict[str, Optional[float]]:
    """Load USGS nowcast raster → lookup per admin. Graceful fallback."""
    usgs_lookup: Dict[str, Optional[float]] = {}
    if not os.path.exists(USGS_PATH):
        logger.warning("[NOAA] USGS nowcast file not found: %s", USGS_PATH)
        _terrain_cache["usgs_loaded"] = False
        return usgs_lookup

    try:
        with rasterio.open(USGS_PATH) as src:
            admin_features = _load_admin_features()
            for admin in admin_features:
                aid = admin["id"]
                lat, lon = admin["representative_lat"], admin["representative_lon"]
                try:
                    row, col = src.index(lon, lat)
                    if 0 <= row < src.height and 0 <= col < src.width:
                        data = src.read(1, window=Window(col, row, 1, 1))
                        val = float(data[0, 0])
                        nodata = src.nodata
                        if (nodata is not None and val == nodata) or val < 0:
                            usgs_lookup[aid] = None
                        else:
                            usgs_lookup[aid] = round(min(val, 1.0), 2)
                    else:
                        usgs_lookup[aid] = None
                except Exception:
                    usgs_lookup[aid] = None
        _terrain_cache["usgs_loaded"] = True
        logger.info("[NOAA] USGS nowcast loaded: %s entries", len(usgs_lookup))
    except Exception as exc:
        logger.warning("[NOAA] Failed to load USGS nowcast: %s", exc)
        _terrain_cache["usgs_loaded"] = False

    return usgs_lookup


# ========== BATCH 3: BMKG ACTIVE ALERTS ==========

BMKG_RSS_URL = "https://www.bmkg.go.id/alerts/nowcast/id/rss.xml"
CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"
BMKG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Referer": "https://www.bmkg.go.id/",
    "Accept": "application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
}


async def _fetch_bmkg_rss_raw() -> Optional[str]:
    """Fetch BMKG RSS feed. Returns XML text or None."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(BMKG_RSS_URL, headers=BMKG_HEADERS)
            res.raise_for_status()
            return res.text
    except Exception as exc:
        logger.warning("[NOAA] BMKG RSS fetch failed: %s", exc)
        return None


async def _fetch_bmkg_cap_detail(url: str) -> Optional[Dict[str, Any]]:
    """Fetch and parse CAP XML detail for one alert. Returns polygons + areaDesc."""
    if "bmkg.go.id" not in url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=BMKG_HEADERS)
            res.raise_for_status()
            xml_text = res.text
    except Exception as exc:
        logger.warning("[NOAA] BMKG CAP fetch failed for %s: %s", url, exc)
        return None

    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        ns = {"cap": CAP_NS}

        polygons = []
        area_descs = []
        for area in root.findall(f".//cap:area", ns):
            ad = area.findtext("cap:areaDesc", default="", namespaces=ns)
            if ad:
                area_descs.append(ad.strip())
            for poly in area.findall("cap:polygon", ns):
                if not poly.text:
                    continue
                coords = []
                for pair in poly.text.strip().split():
                    parts = pair.split(",")
                    if len(parts) >= 2:
                        try:
                            lat, lon = float(parts[0]), float(parts[1])
                            coords.append([lon, lat])
                        except ValueError:
                            continue
                if len(coords) >= 3:
                    polygons.append(coords)

        return {"areaDesc": "; ".join(area_descs), "polygons": polygons}
    except Exception as exc:
        logger.warning("[NOAA] CAP XML parse failed: %s", exc)
        return None


async def _fetch_bmkg_active_alerts() -> Dict[str, Dict[str, Any]]:
    """Fetch BMKG active alerts and spatial-match to GADM admin areas.
    Returns {admin_id: {alert_active: bool, boost: float}}."""
    async with _lock_bmkg:
        now = datetime.now(timezone.utc)
        if _bmkg_alert_cache["data"] and _bmkg_alert_cache["fetched_at"]:
            age = now - _bmkg_alert_cache["fetched_at"]
            if age < timedelta(minutes=15):
                return _bmkg_alert_cache["data"]

    rss_xml = await _fetch_bmkg_rss_raw()
    if not rss_xml:
        logger.warning("[NOAA] BMKG RSS unavailable, alert boost disabled")
        return {}

    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(rss_xml)
        channel = root.find("channel")
        if channel is None:
            return {}
        cap_urls = []
        for item in channel.findall("item"):
            link = item.findtext("link", "")
            if link:
                cap_urls.append(link)
    except Exception:
        return {}

    # Fetch CAP details for all alerts (rate-limited)
    cap_details = []
    sem = asyncio.Semaphore(3)
    async def fetch_one(url: str) -> Optional[Dict[str, Any]]:
        async with sem:
            return await _fetch_bmkg_cap_detail(url)

    tasks = [fetch_one(url) for url in cap_urls[:10]]  # max 10 alerts
    results = await asyncio.gather(*tasks)
    cap_details = [r for r in results if r and r.get("polygons")]

    if not cap_details:
        return {}

    # Spatial match: check if any GADM admin point falls within CAP polygons
    admin_features = _load_admin_features()
    alert_map: Dict[str, Dict[str, Any]] = {}
    for admin in admin_features:
        pt = Point(admin["representative_lon"], admin["representative_lat"])
        matched = False
        for detail in cap_details:
            for poly_coords in detail["polygons"]:
                try:
                    from shapely.geometry import Polygon
                    cap_poly = Polygon(poly_coords)
                    if cap_poly.is_valid and cap_poly.contains(pt):
                        matched = True
                        break
                except Exception:
                    continue
            if matched:
                break
        if matched:
            alert_map[admin["id"]] = {"alert_active": True, "boost": 1.5}
        else:
            alert_map[admin["id"]] = {"alert_active": False, "boost": 1.0}

    async with _lock_bmkg:
        _bmkg_alert_cache["data"] = alert_map
        _bmkg_alert_cache["fetched_at"] = now

    logger.info("[NOAA] BMKG alerts spatial match: %s / %s admin areas matched",
                sum(1 for v in alert_map.values() if v["alert_active"]), len(alert_map))
    return alert_map


# ========== BATCH 3: GLOFAS FLOOD SIGNAL (DISABLED) ==========
#
# GloFAS (Global Flood Awareness System) real-time flood probability
# is available through the Copernicus Climate Data Store (CDS) at:
#   https://cds.climate.copernicus.eu/api/
#
# Access requires:
#   1. Registration at https://cds.climate.copernicus.eu/
#   2. CDS API key (stored in ~/.cdsapirc or CDSAPI_KEY env var)
#   3. Python package `cdsapi` (not installed in this project)
#
# For production deployment on Hugging Face Spaces with Copernicus key:
#   pip install cdsapi
#   Set CDSAPI_KEY as Space secret
#   Re-enable _fetch_glofas_signal() and _GLOFAS_ENABLED = True
#
# Free alternatives considered:
#   - JRC Global Surface Water: static occurrence, not real-time
#   - Overpass API OSM flood tags: complex spatial query per admin, rate-limited
#   - Open-Meteo: no flood-specific variable available
#
# Conclusion: GloFAS is disabled by default. All glofas_flood_prob
# fields return None. The field is retained in the response schema for
# forward compatibility — when CDS key becomes available, the data
# pipeline will populate it without any frontend changes.

_GLOFAS_ENABLED = False


async def _fetch_glofas_signal() -> Dict[str, Optional[float]]:
    """Return empty dict — GloFAS disabled by default.
    
    To enable: register at https://cds.climate.copernicus.eu/,
    install cdsapi, set CDSAPI_KEY, and set _GLOFAS_ENABLED = True.
    """
    if not _GLOFAS_ENABLED:
        logger.debug("[NOAA] GloFAS disabled — CDS API key not configured")
        return {}
    return {}


# ========== INIT MODULE ==========

def _init_terrain_module() -> None:
    """One-time init for all terrain + raster-based data."""
    _compute_elevation_slope_lookup()
    generate_hillshade_image()
    _load_bnpb_susceptibility()
    _load_usgs_nowcast_raster()


# ========== FORECAST FETCHING ==========

async def _fetch_admin_forecasts(
    admin_features: List[Dict[str, Any]],
    forecast_days: int,
) -> List[Dict[str, Any]]:
    """Fetch forecasts for admin areas with per-kabupaten cache (TTL 60 min).
    Only fetches from API for kabupaten not in cache or whose cache expired.
    """
    now = datetime.now(timezone.utc)
    results: List[Dict[str, Any]] = []
    needs_fetch: List[Dict[str, Any]] = []

    # Check per-kabupaten cache first
    async with _forecast_cache_lock:
        for admin in admin_features:
            aid = admin["id"]
            cached = _forecast_cache.get(aid)
            if cached and now - cached["fetched_at"] < CACHE_MAX_AGE:
                results.append(cached["data"])
            else:
                needs_fetch.append(admin)

    if not needs_fetch:
        logger.info("[NOAA] Forecast cache HIT for all %s kabupaten — skipping API call", len(admin_features))
        return results

    logger.info("[NOAA] Fetching forecasts for %s/%s kabupaten (%s batches of %s) — serial with 0.6s delay",
                len(needs_fetch), len(admin_features),
                (len(needs_fetch) + BATCH_SIZE - 1) // BATCH_SIZE, BATCH_SIZE)

    timeout = httpx.Timeout(30.0, connect=10.0)
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    fetched_at = now.isoformat()

    async with httpx.AsyncClient(timeout=timeout, limits=limits, headers={"User-Agent": USER_AGENT}) as client:
        for batch_idx, batch in enumerate(_iter_batches(needs_fetch, BATCH_SIZE)):
            try:
                metrics = await _fetch_forecast_batch(client, batch, forecast_days)
            except Exception as exc:
                logger.error("[NOAA] Forecast batch failed after retries: %s", exc)
                metrics = [None] * len(batch)

            for admin_metrics in zip(batch, metrics):
                admin, metric = admin_metrics
                if metric is None:
                    continue
                enriched_row = {
                    **metric,
                    "admin_id": admin["id"],
                    "admin_name": admin["name"],
                    "admin_type": admin["type"],
                    "province_id": admin["province_id"],
                    "province_name": admin["province_name"],
                    "lat": admin["representative_lat"],
                    "lon": admin["representative_lon"],
                    "fetched_at": fetched_at,
                }
                results.append(enriched_row)
                async with _forecast_cache_lock:
                    _forecast_cache[admin["id"]] = {"data": enriched_row, "fetched_at": now}

            # Delay between batches to respect rate limit
            if batch_idx < (len(needs_fetch) + BATCH_SIZE - 1) // BATCH_SIZE - 1:
                logger.info("[NOAA] Batch %s done, waiting 0.6s before next...", batch_idx + 1)
                await asyncio.sleep(0.6)

    logger.info("[NOAA] Forecast fetch complete: %s results (cached=%s, fresh=%s)",
                len(results), len(admin_features) - len(needs_fetch), len(results) - (len(admin_features) - len(needs_fetch)))
    return results


# ========== BUILD RESPONSE ==========

def _build_province_features(
    admin_features: List[Dict[str, Any]],
    kabupaten_results: List[Dict[str, Any]],
    include_geometry: bool,
) -> List[Dict[str, Any]]:
    grouped_admin: Dict[str, List[Dict[str, Any]]] = {}
    grouped_results: Dict[str, List[Dict[str, Any]]] = {}
    for admin in admin_features:
        grouped_admin.setdefault(admin["province_id"], []).append(admin)
    for row in kabupaten_results:
        grouped_results.setdefault(row["province_id"], []).append(row)

    features = []
    for province_id, admins in grouped_admin.items():
        rows = grouped_results.get(province_id, [])
        aggregate = _aggregate_rows(rows)
        province_name = admins[0]["province_name"]
        properties = {
            **aggregate,
            "admin_id": province_id,
            "admin_name": province_name,
            "admin_level": "province",
            "province_id": province_id,
            "province_name": province_name,
            "kabupaten_count": len(admins),
            "sampled_kabupaten_count": len(rows),
        }
        geometry = mapping(unary_union([admin["geometry"] for admin in admins])) if include_geometry else None
        features.append({"type": "Feature", "geometry": geometry, "properties": properties})
    return features


def _build_kabupaten_features(
    admin_features: List[Dict[str, Any]],
    results_by_admin: Dict[str, Dict[str, Any]],
    include_geometry: bool,
) -> List[Dict[str, Any]]:
    features = []
    for admin in admin_features:
        row = results_by_admin.get(admin["id"])
        aggregate = _aggregate_rows([row] if row else [])
        properties = {
            **aggregate,
            "admin_id": admin["id"],
            "admin_name": admin["name"],
            "admin_type": admin["type"],
            "admin_level": "kabupaten",
            "province_id": admin["province_id"],
            "province_name": admin["province_name"],
            "sample_lat": admin["representative_lat"],
            "sample_lon": admin["representative_lon"],
        }
        geometry = mapping(admin["geometry"]) if include_geometry else None
        features.append({"type": "Feature", "geometry": geometry, "properties": properties})
    return features


def _get_bnpb_info(admin_id: str) -> Dict[str, Any]:
    """Get BNPB susceptibility for admin. Fallback to default if not loaded."""
    bnpb_lookup = _terrain_cache.get("_bnpb_lookup")
    if bnpb_lookup and admin_id in bnpb_lookup:
        return bnpb_lookup[admin_id]
    return {"score": 0.5, "class": "medium"}


def _get_usgs_score(admin_id: str) -> Optional[float]:
    usgs_lookup = _terrain_cache.get("_usgs_lookup")
    if usgs_lookup and admin_id in usgs_lookup:
        return usgs_lookup[admin_id]
    return None


def _aggregate_rows(rows: List[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    valid_rows = [row for row in rows if row]
    if not valid_rows:
        return {
            "risk_level": "LOW",
            "risk_level_numeric": 1,
            "risk_score": 0.0,
            "risk_label": RISK_MODEL["LOW"]["label"],
            "risk_color": RISK_MODEL["LOW"]["color"],
            "hazard_count": 0,
            "monitored_count": 0,
            "rainfall_3day_mm": 0.0,
            "rainfall_intensity_max_mm_per_hour": 0.0,
            "soil_moisture_max_3d": 0.0,
            "soil_moisture_mid_max_3d": 0.0,
            "soil_moisture_deep_max_3d": 0.0,
            "cape_max": 0.0,
            "wind_gusts_max_ms": 0.0,
            "precipitation_probability_max": None,
            "glofas_flood_prob": None,
            "usgs_nowcast_score": None,
            "elevation_m": 0.0,
            "slope_deg": 0.0,
            "slope_class": "flat",
            "bnpb_susceptibility_class": "medium",
            "bnpb_susceptibility_score": 0.5,
            "terrain_weight": 1.0,
            "bmkg_alert_active": False,
            "bmkg_alert_boost": 1.0,
            "final_risk_score": 0.0,
            "risk_drivers": [],
            "recommendation": "Belum ada data prakiraan valid untuk wilayah ini.",
        }

    highest = max(valid_rows, key=lambda row: (row["risk_level_numeric"], row["risk_score"]))
    hazard_count = sum(1 for row in valid_rows if row["risk_level_numeric"] >= 2)
    drivers = sorted({driver for row in valid_rows for driver in row.get("risk_drivers", [])})
    probabilities = [
        row["precipitation_probability_max"]
        for row in valid_rows
        if row.get("precipitation_probability_max") is not None
    ]
    return {
        "risk_level": highest["risk_level"],
        "risk_level_numeric": highest["risk_level_numeric"],
        "risk_score": highest["risk_score"],
        "risk_label": RISK_MODEL[highest["risk_level"]]["label"],
        "risk_color": RISK_MODEL[highest["risk_level"]]["color"],
        "hazard_count": hazard_count,
        "monitored_count": len(valid_rows),
        "rainfall_3day_mm": round(max(row["rainfall_3day_mm"] for row in valid_rows), 1),
        "rainfall_intensity_max_mm_per_hour": round(
            max(row["rainfall_intensity_max_mm_per_hour"] for row in valid_rows), 1
        ),
        "soil_moisture_max_3d": round(max(row["soil_moisture_max_3d"] for row in valid_rows), 4),
        "soil_moisture_mid_max_3d": round(max(row.get("soil_moisture_mid_max_3d", 0) for row in valid_rows), 4),
        "soil_moisture_deep_max_3d": round(max(row.get("soil_moisture_deep_max_3d", 0) for row in valid_rows), 4),
        "cape_max": round(max(row.get("cape_max", 0) for row in valid_rows), 1),
        "wind_gusts_max_ms": round(max(row.get("wind_gusts_max_ms", 0) for row in valid_rows), 1),
        "precipitation_probability_max": round(max(probabilities), 1) if probabilities else None,
        "glofas_flood_prob": None,
        "usgs_nowcast_score": None,
        "elevation_m": 0.0,
        "slope_deg": 0.0,
        "slope_class": "flat",
        "bnpb_susceptibility_class": "medium",
        "bnpb_susceptibility_score": 0.5,
        "terrain_weight": 1.0,
        "bmkg_alert_active": False,
        "bmkg_alert_boost": 1.0,
        "final_risk_score": round(highest["risk_score"], 2),
        "risk_drivers": drivers[:8],
        "recommendation": RISK_MODEL[highest["risk_level"]]["recommendation"],
    }


def _build_markers(kabupaten_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": row["admin_id"],
            "name": row["admin_name"],
            "province_name": row["province_name"],
            "lat": round(row["lat"], 5),
            "lon": round(row["lon"], 5),
            "risk_level": row["risk_level"],
            "risk_level_numeric": row["risk_level_numeric"],
            "risk_score": row["risk_score"],
            "rainfall_3day_mm": row["rainfall_3day_mm"],
            "rainfall_intensity_max_mm_per_hour": row["rainfall_intensity_max_mm_per_hour"],
            "soil_moisture_max_3d": row["soil_moisture_max_3d"],
            "soil_moisture_mid_max_3d": row.get("soil_moisture_mid_max_3d", 0),
            "soil_moisture_deep_max_3d": row.get("soil_moisture_deep_max_3d", 0),
            "cape_max": row.get("cape_max", 0),
            "wind_gusts_max_ms": row.get("wind_gusts_max_ms", 0),
            "precipitation_probability_max": row.get("precipitation_probability_max"),
            "elevation_m": row.get("elevation_m", 0),
            "slope_deg": row.get("slope_deg", 0),
            "slope_class": row.get("slope_class", "flat"),
            "bnpb_susceptibility_class": row.get("bnpb_susceptibility_class", "medium"),
            "bnpb_susceptibility_score": row.get("bnpb_susceptibility_score", 0.5),
            "terrain_weight": row.get("terrain_weight", 1.0),
            "bmkg_alert_active": row.get("bmkg_alert_active", False),
            "glofas_flood_prob": row.get("glofas_flood_prob"),
            "recommendation": row["recommendation"],
            "risk_drivers": row["risk_drivers"],
        }
        for row in kabupaten_results
    ]


def _build_summary(kabupaten_results: List[Dict[str, Any]], province_features: List[Dict[str, Any]]) -> Dict[str, Any]:
    risk_counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    bmkg_alert_count = 0
    terrain_high_risk_count = 0
    for row in kabupaten_results:
        risk_counts[row["risk_level"]] += 1
        if row.get("bmkg_alert_active"):
            bmkg_alert_count += 1
        if row.get("slope_class") == "steep" and row.get("bnpb_susceptibility_class") == "high":
            terrain_high_risk_count += 1
    highest = "LOW"
    for level in ("HIGH", "MEDIUM", "LOW"):
        if risk_counts[level] > 0:
            highest = level
            break

    ranked_provinces = sorted(
        [feature["properties"] for feature in province_features],
        key=lambda props: (
            props["risk_level_numeric"],
            props["hazard_count"],
            props["risk_score"],
        ),
        reverse=True,
    )
    return {
        "overall_risk_level": highest,
        "overall_risk_level_numeric": RISK_MODEL[highest]["level"],
        "risk_counts": risk_counts,
        "active_hazard_count": risk_counts["MEDIUM"] + risk_counts["HIGH"],
        "high_hazard_count": risk_counts["HIGH"],
        "monitored_kabupaten_count": len(kabupaten_results),
        "affected_province_count": sum(
            1 for feature in province_features if feature["properties"]["risk_level_numeric"] >= 2
        ),
        "bmkg_alert_count": bmkg_alert_count,
        "terrain_high_risk_count": terrain_high_risk_count,
        "province_ranking": [
            {
                "province_id": props["province_id"],
                "province_name": props["province_name"],
                "risk_level": props["risk_level"],
                "risk_level_numeric": props["risk_level_numeric"],
                "hazard_count": props["hazard_count"],
                "monitored_count": props["monitored_count"],
                "rainfall_3day_mm": props["rainfall_3day_mm"],
                "soil_moisture_max_3d": props["soil_moisture_max_3d"],
                "elevation_m": props.get("elevation_m", 0),
                "slope_class": props.get("slope_class", "flat"),
            }
            for props in ranked_provinces[:10]
        ],
    }


async def _build_hazard_response(
    forecast_days: int,
    admin_level: Literal["province", "kabupaten"],
    include_geometry: bool,
) -> Dict[str, Any]:
    cache_key = f"{forecast_days}|{admin_level}|{include_geometry}"
    now = datetime.now(timezone.utc)

    async with _lock:
        cached = _hazard_cache.get(cache_key)
        if cached and now - cached["timestamp"] < CACHE_MAX_AGE:
            return cached["data"]

    admin_features = _load_admin_features()
    kabupaten_results = await _fetch_admin_forecasts(admin_features, forecast_days)

    # Enrich with terrain, BNPB, USGS, GloFAS data
    bnpb_lookup = _terrain_cache.get("_bnpb_lookup")
    usgs_lookup = _terrain_cache.get("_usgs_lookup")

    # Lazy-init terrain if not yet loaded
    if not _terrain_cache.get("srtm_loaded"):
        _compute_elevation_slope_lookup()
        generate_hillshade_image()
    if bnpb_lookup is None:
        bnpb_lookup = _load_bnpb_susceptibility()
        _terrain_cache["_bnpb_lookup"] = bnpb_lookup
    if usgs_lookup is None:
        usgs_lookup = _load_usgs_nowcast_raster()
        _terrain_cache["_usgs_lookup"] = usgs_lookup

    # Fetch BMKG alerts
    bmkg_alerts = await _fetch_bmkg_active_alerts()

    # Fetch GloFAS signal
    glofas_lookup = await _fetch_glofas_signal()

    # Enrich each kabupaten result
    for row in kabupaten_results:
        aid = row["admin_id"]

        # Terrain
        ti = _get_terrain_info(aid)
        row["elevation_m"] = ti["elevation_m"]
        row["slope_deg"] = ti["slope_deg"]
        row["slope_class"] = ti["slope_class"]

        # BNPB
        bn_info = bnpb_lookup.get(aid, {"score": 0.5, "class": "medium"}) if bnpb_lookup else {"score": 0.5, "class": "medium"}
        row["bnpb_susceptibility_class"] = bn_info["class"]
        row["bnpb_susceptibility_score"] = bn_info["score"]

        # Terrain weight
        tw_key = (ti["slope_class"], bn_info["class"])
        row["terrain_weight"] = TERRAIN_WEIGHT.get(tw_key, 1.0)

        # BMKG alert
        bmkg_info = bmkg_alerts.get(aid, {"alert_active": False, "boost": 1.0})
        row["bmkg_alert_active"] = bmkg_info["alert_active"]
        row["bmkg_alert_boost"] = bmkg_info["boost"]

        # USGS
        usgs_val = usgs_lookup.get(aid) if usgs_lookup else None
        row["usgs_nowcast_score"] = usgs_val

        # GloFAS
        glofas_val = glofas_lookup.get(aid) if glofas_lookup else None
        row["glofas_flood_prob"] = glofas_val

        # Recalculate risk with all new signals
        rl, rs, rd = _calculate_risk(
            soil_moisture=row["soil_moisture_max_3d"],
            rainfall_3day=row["rainfall_3day_mm"],
            rainfall_intensity=row["rainfall_intensity_max_mm_per_hour"],
            precipitation_probability=row.get("precipitation_probability_max"),
            soil_moisture_mid=row.get("soil_moisture_mid_max_3d", 0),
            soil_moisture_deep=row.get("soil_moisture_deep_max_3d", 0),
            cape=row.get("cape_max", 0),
            wind_gusts=row.get("wind_gusts_max_ms", 0),
            glofas_flood_prob=glofas_val,
            usgs_nowcast=usgs_val,
            bmkg_alert_boost=bmkg_info["boost"],
            terrain_multiplier=row["terrain_weight"],
            bnpb_susceptibility_score=bn_info["score"],
        )
        row["risk_level"] = rl
        row["risk_level_numeric"] = RISK_MODEL[rl]["level"]
        row["risk_score"] = round(rs, 2)
        row["final_risk_score"] = round(rs, 2)
        row["risk_drivers"] = rd[:8]
        row["recommendation"] = RISK_MODEL[rl]["recommendation"]

    results_by_admin = {row["admin_id"]: row for row in kabupaten_results}
    province_features = _build_province_features(admin_features, kabupaten_results, include_geometry)
    kabupaten_features = _build_kabupaten_features(admin_features, results_by_admin, include_geometry)
    layer_features = province_features if admin_level == "province" else kabupaten_features
    summary = _build_summary(kabupaten_results, province_features)
    fetched_at = datetime.now(timezone.utc).isoformat()

    response = {
        "status": "success",
        "source": {
            "provider": "Open-Meteo forecast API + BMKG + SRTM + BNPB + USGS",
            "model_note": "Forecast variables from Open-Meteo (NOAA/NCEP). Terrain from SRTM. Susceptibility from BNPB. Nowcast from USGS. Weather alerts from BMKG.",
            "api_url": OPEN_METEO_URL,
        },
        "risk_model": RISK_MODEL,
        "forecast_days": forecast_days,
        "admin_level": admin_level,
        "summary": summary,
        "hazard_layer": {
            "type": "FeatureCollection",
            "features": layer_features,
            "metadata": {
                "admin_source": "GADM 3.6 Indonesia level 2",
                "geometry_included": include_geometry,
                "generated_at": fetched_at,
            },
        },
        "province_layer": {
            "type": "FeatureCollection",
            "features": province_features,
        },
        "kabupaten_layer": {
            "type": "FeatureCollection",
            "features": kabupaten_features,
        },
        "markers": _build_markers(kabupaten_results),
        "fetched_at": fetched_at,
    }

    async with _lock:
        _hazard_cache[cache_key] = {"timestamp": now, "data": response}
    return response


# ========== ENDPOINTS ==========

@router.get("/early-warning/indonesia", summary="Area-based early warning layer for Indonesia")
async def get_early_warning_indonesia(
    forecast_days: int = Query(3, ge=1, le=7),
    admin_level: Literal["province", "kabupaten"] = Query("province"),
    include_geometry: bool = Query(True),
):
    """Return administrative hazard layers, markers, and dashboard summaries."""
    try:
        data = await _build_hazard_response(forecast_days, admin_level, include_geometry)
        return JSONResponse(content=data)
    except FileNotFoundError as exc:
        raise HTTPException(500, str(exc)) from exc
    except Exception as exc:
        logger.exception("[NOAA] Failed to build early warning layer")
        raise HTTPException(502, f"Gagal membangun layer peringatan dini: {exc}") from exc


@router.get("/noaa/hazard-layer", summary="NOAA-derived administrative hazard GeoJSON")
async def get_noaa_hazard_layer(
    forecast_days: int = Query(3, ge=1, le=7),
    admin_level: Literal["province", "kabupaten"] = Query("province"),
):
    data = await _build_hazard_response(forecast_days, admin_level, True)
    return JSONResponse(
        content={
            "type": "FeatureCollection",
            "features": data["hazard_layer"]["features"],
            "metadata": data["hazard_layer"]["metadata"],
            "summary": data["summary"],
            "risk_model": data["risk_model"],
            "source": data["source"],
        }
    )


@router.get("/noaa/soil-moisture", summary="Prakiraan kelembaban tanah per koordinat")
async def get_soil_moisture(
    latitude: float = Query(..., description="Lintang"),
    longitude: float = Query(..., description="Bujur"),
    forecast_days: int = Query(7, ge=1, le=16),
    threshold: Optional[float] = Query(None, ge=0.0, le=1.0),
):
    if not _is_valid_indonesia_coordinate(latitude, longitude):
        raise HTTPException(400, "Koordinat di luar wilayah Indonesia")

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "soil_moisture_0_to_7cm",
        "forecast_days": forecast_days,
        "timezone": FORECAST_TIMEZONE,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise HTTPException(502, f"Gagal mengambil data: {exc}") from exc

    hourly = data.get("hourly", {})
    hourly_result = [
        {"time": time, "soil_moisture_m3_per_m3": round(value, 4)}
        for time, value in zip(hourly.get("time", []), hourly.get("soil_moisture_0_to_7cm", []))
        if value is not None
    ]
    warning_details = [
        item for item in hourly_result if threshold is not None and item["soil_moisture_m3_per_m3"] >= threshold
    ]
    return JSONResponse(
        content={
            "latitude": latitude,
            "longitude": longitude,
            "forecast_days": forecast_days,
            "unit": "m3/m3",
            "threshold_m3_per_m3": threshold,
            "early_warning": bool(warning_details),
            "warning_details": warning_details if threshold is not None else None,
            "hourly": hourly_result,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    )


@router.get("/noaa/rainfall", summary="Prakiraan curah hujan per koordinat")
async def get_rainfall(
    latitude: float = Query(...),
    longitude: float = Query(...),
    forecast_days: int = Query(7, ge=1, le=16),
):
    if not _is_valid_indonesia_coordinate(latitude, longitude):
        raise HTTPException(400, "Koordinat di luar wilayah Indonesia")

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "rain,precipitation_probability",
        "forecast_days": forecast_days,
        "timezone": FORECAST_TIMEZONE,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise HTTPException(502, f"Gagal mengambil data: {exc}") from exc

    hourly = data.get("hourly", {})
    rain_3day = 0.0
    intensity_max = 0.0
    hourly_result = []
    for index, (time, rain) in enumerate(zip(hourly.get("time", []), hourly.get("rain", []))):
        if rain is None:
            continue
        value = float(rain)
        hourly_result.append({"time": time, "rain_mm": value})
        if index < 72:
            rain_3day += value
            intensity_max = max(intensity_max, value)

    return JSONResponse(
        content={
            "latitude": latitude,
            "longitude": longitude,
            "forecast_days": forecast_days,
            "unit": "mm",
            "rainfall_3day_mm": round(rain_3day, 1),
            "max_intensity_mm_per_hour": round(intensity_max, 1),
            "hourly": hourly_result,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    )


@router.get("/noaa/health", summary="Health check")
async def noaa_health():
    try:
        async with httpx.AsyncClient(timeout=5.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                OPEN_METEO_URL,
                params={
                    "latitude": -6.2,
                    "longitude": 106.8,
                    "hourly": "temperature_2m",
                    "forecast_days": 1,
                },
            )
            om_reachable = resp.status_code == 200
    except Exception:
        om_reachable = False

    # Check BMKG reachability
    bmkg_reachable = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(BMKG_RSS_URL, headers=BMKG_HEADERS)
            bmkg_reachable = r.status_code == 200
    except Exception:
        pass

    admin_count = len(_load_admin_features()) if _admin_cache["features"] else 0
    if not _terrain_cache.get("srtm_loaded"):
        _compute_elevation_slope_lookup()
        generate_hillshade_image()

    return {
        "status": "ok" if om_reachable else "degraded",
        "open_meteo_reachable": om_reachable,
        "bmkg_reachable": bmkg_reachable,
        "glofas_enabled": _GLOFAS_ENABLED,
        "glofas_note": "Disabled by default. Requires Copernicus CDS API key (cdsapi) to enable. See _fetch_glofas_signal() docs.",
        "usgs_nowcast_loaded": _terrain_cache.get("usgs_loaded", False),
        "srtm_loaded": _terrain_cache.get("srtm_loaded", False),
        "bnpb_susceptibility_loaded": _terrain_cache.get("bnpb_loaded", False),
        "terrain_hillshade_ready": _terrain_cache.get("hillshade_png") is not None,
        "admin_boundaries_loaded": admin_count if admin_count else len(_load_admin_features()),
    }


# ========== COMPOSITE RISK OVERLAY ==========

COMPOSITE_CACHE_MAX_AGE = timedelta(minutes=60)
# Per-key cache: key (from _build_cache_key) -> {"data": bytes, "bounds": str, "fetched_at": datetime}
_raster_cache: Dict[str, Dict[str, Any]] = {}
# Per-key cache: key -> {"data": dict, "fetched_at": datetime}
_metadata_cache: Dict[str, Dict[str, Any]] = {}
_lock_composite = asyncio.Lock()
# Set of cache keys currently being refreshed — prevents duplicate refreshes
_refreshing_keys: Set[str] = set()

def _build_cache_key(kabupaten_ids: Optional[str] = None) -> str:
    """Hourly cache key with optional kabupaten_id hash for per-filter caching."""
    hour_slot = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    suffix = ""
    if kabupaten_ids:
        suffix = ":" + hashlib.md5(kabupaten_ids.encode()).hexdigest()[:8]
    return f"terrain_composite:{hour_slot}{suffix}"

RISK_OVERLAY_COLORS = {
    "HIGH": (230, 40, 40, 80),
    "MEDIUM": (235, 195, 20, 60),
    "LOW": (0, 0, 0, 0),
}


def _rasterize_risk_layer(
    admin_features: List[Dict[str, Any]],
    results_by_admin: Dict[str, Dict[str, Any]],
    transform: Any,
    out_shape: Tuple[int, int],
) -> np.ndarray:
    """Rasterize risk polygons into RGBA overlay matching terrain grid."""
    overlay = np.zeros((out_shape[0], out_shape[1], 4), dtype=np.uint8)
    for risk_level in ("HIGH", "MEDIUM", "LOW"):
        color = RISK_OVERLAY_COLORS[risk_level]
        if color[3] == 0:
            continue
        polygons = []
        for admin in admin_features:
            row = results_by_admin.get(admin["id"])
            if row and row["risk_level"] == risk_level:
                geom = admin.get("geometry")
                if geom is not None:
                    polygons.append((mapping(geom), 1))
        if not polygons:
            continue
        try:
            mask = rasterize(
                polygons, out_shape=out_shape, transform=transform,
                fill=0, default_value=1, dtype=np.uint8, all_touched=True,
            )
            for c in range(3):
                overlay[:, :, c] = np.where(mask == 1, color[c], overlay[:, :, c])
            overlay[:, :, 3] = np.where(mask == 1, color[3], overlay[:, :, 3])
        except Exception as exc:
            logger.warning("[NOAA] Risk rasterize failed for %s: %s", risk_level, exc)
    return overlay


def _filter_admin_by_ids(kabupaten_ids_str: Optional[str]) -> Tuple[List[Dict[str, Any]], str]:
    """Filter admin features by comma-separated kabupaten_ids. Returns (filtered_af, hash_suffix)."""
    af = _load_admin_features()
    if not kabupaten_ids_str:
        return af, "all"

    ids = [x.strip() for x in kabupaten_ids_str.split(",") if x.strip()]
    if len(ids) > COMPOSITE_MAX_KABUPATEN:
        raise HTTPException(
            400,
            f"Maksimal {COMPOSITE_MAX_KABUPATEN} kabupaten, menerima {len(ids)}"
        )
    id_set = set(ids)
    filtered = [a for a in af if a["id"] in id_set]
    if not filtered:
        raise HTTPException(400, "Tidak ada kabupaten valid dari ID yang diberikan")
    hash_suffix = hashlib.md5(kabupaten_ids_str.encode()).hexdigest()[:8]
    logger.info("[NOAA] Filtered to %s/%s kabupaten (hash=%s)", len(filtered), len(ids), hash_suffix)
    return filtered, hash_suffix


async def generate_composite_raster_and_metadata(
    kabupaten_ids: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[dict], Optional[str]]:
    """Generate composite PNG + metadata dict + bounds string.
    Returns (png_bytes, metadata_dict, bounds_str).
    Bounds are computed from the actual admin features — never the full raster extent."""
    logger.info("[NOAA] Generating composite for kabupaten_ids=%s", kabupaten_ids)
    # Determine admin features and bounds FIRST (before loading DEM)
    admin_features = _load_admin_features()
    if kabupaten_ids:
        ids_set = set(x.strip() for x in kabupaten_ids.split(",") if x.strip())
        selected_admins = [a for a in admin_features if a["id"] in ids_set]
        if not selected_admins:
            raise ValueError("No valid kabupaten found for given IDs")
        # Log selected kabupaten names for debugging
        selected_names = [a["name"] for a in selected_admins]
        logger.info("[NOAA] Filter: %s kabupaten selected: %s", len(selected_admins), selected_names)
        # Compute union bounding box from selected admin geometries
        selected_geoms = [a["geometry"] for a in selected_admins if a["geometry"] is not None]
        if not selected_geoms:
            raise ValueError("Selected kabupaten have no valid geometry")
        union = unary_union(selected_geoms)
        minx, miny, maxx, maxy = union.bounds
        logger.info("[NOAA] Union bounds: minx=%s, miny=%s, maxx=%s, maxy=%s", minx, miny, maxx, maxy)
        bbox = [minx, miny, maxx, maxy]  # west, south, east, north
        af = selected_admins
        logger.info("[NOAA] Computed bbox (west,south,east,north): %s", bbox)
    else:
        af = admin_features
        # Fallback to Indonesia-wide bounds when no filter
        bbox = [95.0, -12.0, 142.0, 7.0]  # west, south, east, north
        logger.info("[NOAA] Using Indonesia-wide bounds (no kabupaten filter)")

    src = _load_srtm_raster()
    if src is None:
        logger.warning("[NOAA] SRTM not available, cannot generate composite")
        return None, None, None
    try:
        transform = src.transform
        MAX_SIZE = 2000

        # Compute raster window from bbox [west, north, east, south]
        window = src.window(*bbox)  # west, south, east, north internally
        w_raw = int(window.width)
        h_raw = int(window.height)

        if w_raw <= 0 or h_raw <= 0:
            raise ValueError(f"Invalid raster window: w={w_raw}, h={h_raw}")

        logger.info("[COMPOSITE] Window from bbox %s: %s x %s pixels", bbox, w_raw, h_raw)

        # Get precise geographic bounds of the raster window BEFORE closing src
        # window_bounds returns (left, bottom, right, top) in CRS coordinates
        window_geo = src.window_bounds(window)  # (left, bottom, right, top)

        # Read with optional downsampling — use window to crop, out_shape to downscale
        if w_raw <= MAX_SIZE and h_raw <= MAX_SIZE:
            elev_raw = src.read(1, window=window)
            h, w = h_raw, w_raw
        else:
            # Compute downsampled dimensions preserving aspect ratio
            if w_raw >= h_raw:
                new_w = MAX_SIZE
                new_h = max(1, int(h_raw * MAX_SIZE / w_raw))
            else:
                new_h = MAX_SIZE
                new_w = max(1, int(w_raw * MAX_SIZE / h_raw))
            elev_raw = src.read(1, window=window, out_shape=(new_h, new_w))
            h, w = new_h, new_w
            if w <= 0 or h <= 0:
                raise ValueError(f"Invalid downsampled size: w={w}, h={h}")
            logger.info("[COMPOSITE] Downsampled window: %s x %s, original window: %s x %s", w, h, w_raw, h_raw)

        # Build transform from georeferenced bounds — more robust than manual affine scaling
        left, bottom, right, top = window_geo
        win_transform = rasterio.transform.from_bounds(left, bottom, right, top, w, h)

        # Validate transform: convert four image corners to geo coords and compare with window_geo
        corners = [(0, 0), (w, 0), (0, h), (w, h)]
        geo_corners = [rasterio.transform.xy(win_transform, cy, cx, offset='ul') for cx, cy in corners]
        geo_left = min(gc[0] for gc in geo_corners)
        geo_right = max(gc[0] for gc in geo_corners)
        geo_bottom = min(gc[1] for gc in geo_corners)
        geo_top = max(gc[1] for gc in geo_corners)
        if (abs(geo_left - left) > 1e-6 or abs(geo_right - right) > 1e-6 or
            abs(geo_bottom - bottom) > 1e-6 or abs(geo_top - top) > 1e-6):
            logger.warning("[COMPOSITE] Transform validation mismatch: expected (%s,%s,%s,%s) got (%s,%s,%s,%s)",
                           left, bottom, right, top, geo_left, geo_bottom, geo_right, geo_top)
        else:
            logger.info("[COMPOSITE] Transform validated OK: bounds(%s,%s,%s,%s) -> image %sx%s",
                        left, bottom, right, top, w, h)

        logger.info("[COMPOSITE] Window detail: col_off=%s, row_off=%s, width=%s, height=%s",
                     int(window.col_off), int(window.row_off), w_raw, h_raw)

        # Capture nodata before closing src
        nodata_val = src.nodata
        src.close()

        # Combine NoData masking — single pass
        if nodata_val is not None:
            elev_raw = np.where(elev_raw == nodata_val, 0, elev_raw)
        elev_raw = np.where(elev_raw < -999, 0, elev_raw)
        elev = np.clip(elev_raw, 0, None)

        # Land mask
        try:
            lg = [a["geometry"] for a in af if a["geometry"] is not None]
            if lg:
                lu = unary_union(lg)
                land_mask = geometry_mask(
                    [lu] if lu.geom_type != "MultiPolygon" else list(lu.geoms),
                    transform=win_transform, invert=False, out_shape=(h, w),
                )
            else:
                land_mask = np.zeros((h, w), dtype=bool)
        except Exception:
            land_mask = (elev <= 0)

        # Terrain hillshade + color — use float32, fewer intermediates
        hs_f32 = _compute_hillshade(elev).astype(np.float32) / 255.0
        r, g, b = _elevation_to_color(elev)
        blend = hs_f32 * 0.7 + 0.3
        terrain = np.stack([
            (r.astype(np.float32) * blend).astype(np.uint8),
            (g.astype(np.float32) * blend).astype(np.uint8),
            (b.astype(np.float32) * blend).astype(np.uint8),
            np.where(land_mask, 255, 0).astype(np.uint8),
        ], axis=-1)

        # Verify alpha channel: kabupaten area should be 255 (opaque), outside should be 0 (transparent)
        inside_pixels = int(np.sum(land_mask))
        alpha_inside = int(np.sum(terrain[:, :, 3] > 0))
        logger.info("[COMPOSITE] Alpha channel: %s/%s pixels opaque (land_mask=True=%s) — expected all opaque inside kabupaten",
                    alpha_inside, h * w, inside_pixels)

        # Risk data — reuse existing pipeline
        results = await _fetch_admin_forecasts(af, 3)
        by_admin = {r["admin_id"]: r for r in results}
        bnpb_lk = _terrain_cache.get("_bnpb_lookup")
        usgs_lk = _terrain_cache.get("_usgs_lookup")
        if not _terrain_cache.get("srtm_loaded"):
            _compute_elevation_slope_lookup()
        if bnpb_lk is None:
            bnpb_lk = _load_bnpb_susceptibility()
            _terrain_cache["_bnpb_lookup"] = bnpb_lk
        if usgs_lk is None:
            usgs_lk = _load_usgs_nowcast_raster()
            _terrain_cache["_usgs_lookup"] = usgs_lk
        bmkg_a = await _fetch_bmkg_active_alerts()

        # Pre-extract terrain dict to avoid per-row lookup overhead
        elev_dict = _terrain_cache.get("elevation") or {}
        slope_dict = _terrain_cache.get("slope") or {}
        for row in results:
            aid = row["admin_id"]
            elev_m = elev_dict.get(aid, 0.0)
            si = slope_dict.get(aid, {"slope_deg": 0.0, "slope_class": "flat"})
            bi = bnpb_lk.get(aid, {"score": 0.5, "class": "medium"}) if bnpb_lk else {"score": 0.5, "class": "medium"}
            bm_i = bmkg_a.get(aid, {"alert_active": False, "boost": 1.0})
            uv = usgs_lk.get(aid) if usgs_lk else None
            tw = TERRAIN_WEIGHT.get((si["slope_class"], bi["class"]), 1.0)
            row["elevation_m"] = round(elev_m, 1)
            row["slope_deg"] = si["slope_deg"]
            row["slope_class"] = si["slope_class"]
            row["terrain_weight"] = tw
            row["bnpb_susceptibility_class"] = bi["class"]
            row["bnpb_susceptibility_score"] = bi["score"]
            row["bmkg_alert_active"] = bm_i["alert_active"]
            row["bmkg_alert_boost"] = bm_i["boost"]
            row["usgs_nowcast_score"] = uv
            rl, rs, _ = _calculate_risk(
                row["soil_moisture_max_3d"], row["rainfall_3day_mm"], row["rainfall_intensity_max_mm_per_hour"],
                row.get("precipitation_probability_max"), row.get("soil_moisture_mid_max_3d", 0),
                row.get("soil_moisture_deep_max_3d", 0), row.get("cape_max", 0), row.get("wind_gusts_max_ms", 0),
                usgs_nowcast=uv, bmkg_alert_boost=bm_i["boost"], terrain_multiplier=tw, bnpb_susceptibility_score=bi["score"],
            )
            row["risk_level"] = rl
            row["risk_level_numeric"] = RISK_MODEL[rl]["level"]
            row["risk_score"] = round(rs, 2)

        # Alpha-blend risk overlay onto terrain (in-place, no copy)
        risk_overlay = _rasterize_risk_layer(af, by_admin, win_transform, (h, w))
        ao = risk_overlay[:, :, 3] > 0
        r32 = risk_overlay[:, :, 3].astype(np.uint16)
        inv_alpha = 255 - r32
        for c in range(3):
            terrain[:, :, c] = np.where(
                ao,
                (terrain[:, :, c].astype(np.uint16) * inv_alpha + risk_overlay[:, :, c].astype(np.uint16) * r32) // 255,
                terrain[:, :, c],
            )

        img = Image.fromarray(terrain, "RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pfb = buf.getvalue()
        pfs = _build_province_features(af, results, False)
        summary = _build_summary(results, pfs)
        regions = [{"id": r["admin_id"], "name": r["admin_name"], "risk_level": r["risk_level"], "centroid": [r["lon"], r["lat"]]} for r in results]
        meta = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "regions": regions,
        }
        # Use precise window_geo bounds from raster window (not the approximate GADM bbox)
        # header needs "west,north,east,south" format for MapLibre
        west, south, east, north = window_geo
        bounds_str = f"{west},{north},{east},{south}"
        logger.info("[COMPOSITE] Bounds string for header: %s (from window_geo: %s,%s,%s,%s)",
                    bounds_str, west, south, east, north)

        # Validate output before returning
        if not pfb or not meta or not bounds_str:
            raise ValueError(
                f"Invalid composite output: png={len(pfb) if pfb else 0}bytes, meta_keys={list(meta.keys()) if meta else None}, bounds={bounds_str or 'empty'}"
            )
        logger.info("[NOAA] Composite generated: %s bytes, %s regions", len(pfb), len(regions))
        return pfb, meta, bounds_str
    except Exception as exc:
        logger.exception("[NOAA] ERROR generate_composite: %s", str(exc))
        if 'src' in dir() and src: src.close()
        return None, None, None


async def _background_refresh_composite(kabupaten_ids: Optional[str] = None) -> None:
    """Refreshes composite data in the background (stale-while-revalidate) per cache key."""
    cache_key = _build_cache_key(kabupaten_ids)
    logger.info("[NOAA] Background composite refresh started for key=%s", cache_key)
    try:
        png_bytes, metadata_dict, bounds_str = await generate_composite_raster_and_metadata(kabupaten_ids=kabupaten_ids)
        if png_bytes and metadata_dict:
            async with _lock_composite:
                _raster_cache[cache_key] = {"data": png_bytes, "bounds": bounds_str, "fetched_at": datetime.now(timezone.utc)}
                _metadata_cache[cache_key] = {"data": metadata_dict, "fetched_at": datetime.now(timezone.utc)}
            logger.info("[NOAA] Background composite refresh completed for key=%s", cache_key)
        else:
            logger.warning("[NOAA] Background composite refresh returned None for key=%s — keeping stale cache", cache_key)
    except Exception as exc:
        logger.exception("[NOAA] Background composite refresh failed for key=%s: %s", cache_key, str(exc))
    finally:
        async with _lock_composite:
            _refreshing_keys.discard(cache_key)


async def _ensure_risk_composite_cached(kabupaten_ids: Optional[str] = None) -> Tuple[Optional[bytes], Optional[dict], Optional[str]]:
    """Ensure composite and metadata are cached per key.
    - Cache empty → generate BLOCKING (must succeed or fail hard)
    - Cache expired → serve stale + trigger background refresh
    - Cache valid → return immediately
    """
    now = datetime.now(timezone.utc)
    cache_key = _build_cache_key(kabupaten_ids)

    async with _lock_composite:
        raster_entry = _raster_cache.get(cache_key)
        meta_entry = _metadata_cache.get(cache_key)
        raster_valid = raster_entry is not None and (now - raster_entry["fetched_at"]) < COMPOSITE_CACHE_MAX_AGE
        meta_valid = meta_entry is not None and (now - meta_entry["fetched_at"]) < COMPOSITE_CACHE_MAX_AGE

        # 1. BOTH valid — cache HIT
        if raster_valid and meta_valid:
            logger.info("[NOAA] Composite cache HIT for key=%s", cache_key)
            return raster_entry["data"], meta_entry["data"], raster_entry["bounds"]

        # 2. Cache empty on first request — BLOCKING generate
        if raster_entry is None:
            logger.info("[NOAA] Composite cache MISS for key=%s — generating blocking", cache_key)
            png_bytes, metadata_dict, bounds_str = await generate_composite_raster_and_metadata(kabupaten_ids=kabupaten_ids)
            if png_bytes and metadata_dict and bounds_str:
                _raster_cache[cache_key] = {"data": png_bytes, "bounds": bounds_str, "fetched_at": now}
                _metadata_cache[cache_key] = {"data": metadata_dict, "fetched_at": now}
                logger.info("[NOAA] Composite cache populated for key=%s (bounds=%s)", cache_key, bounds_str)
                return png_bytes, metadata_dict, bounds_str
            logger.error("[NOAA] Composite cache GENERATE FAILED for key=%s", cache_key)
            return None, None, None

    # 3. Cache expired but has stale data — serve stale + trigger background refresh
    if cache_key not in _refreshing_keys:
        async with _lock_composite:
            if cache_key not in _refreshing_keys:
                _refreshing_keys.add(cache_key)
                logger.info("[NOAA] Composite cache STALE for key=%s — serving stale + background refresh", cache_key)
                asyncio.create_task(_background_refresh_composite(kabupaten_ids))

    # Return stale data
    async with _lock_composite:
        raster_entry = _raster_cache.get(cache_key)
        meta_entry = _metadata_cache.get(cache_key)
        bounds_str = raster_entry["bounds"] if raster_entry else None
        if not bounds_str:
            bounds_str = "95.0,7.0,142.0,-12.0"
        logger.info("[NOAA] Composite serving stale data for key=%s (bounds=%s)", cache_key, bounds_str)
        return (raster_entry["data"] if raster_entry else None,
                meta_entry["data"] if meta_entry else None,
                bounds_str)


@router.get("/terrain/risk-composite", summary="Composite PNG: terrain + risk overlay")
async def get_risk_composite(
    kabupaten_ids: Optional[str] = Query(None, description="Comma-separated kabupaten IDs, max 5"),
):
    """Return composite PNG (terrain hillshade + elevation color + risk overlay).
    Header: X-Bounds only. Metadata moved to /terrain/risk-metadata."""
    png_bytes, _meta, bounds_str = await _ensure_risk_composite_cached(kabupaten_ids)
    if png_bytes is None:
        raise HTTPException(500, "Failed to generate risk composite")

    logger.info("[NOAA] GET risk-composite: kabupaten_ids=%s, X-Bounds=%s", kabupaten_ids, bounds_str)
    return StreamingResponse(
        io.BytesIO(png_bytes), media_type="image/png",
        headers={"X-Bounds": bounds_str},
    )


@router.get("/terrain/risk-metadata", summary="Metadata for risk composite overlay")
async def get_risk_metadata(
    kabupaten_ids: Optional[str] = Query(None, description="Comma-separated kabupaten IDs, max 5"),
):
    """Return metadata dict for the risk composite overlay.
    Auto-generates if cache is empty or stale."""
    _png_bytes, metadata_dict, _bounds_str = await _ensure_risk_composite_cached(kabupaten_ids)
    if metadata_dict is None:
        raise HTTPException(500, "Failed to generate risk metadata")
    return JSONResponse(content=metadata_dict)


# ========== TERRAIN ENDPOINTS ==========

@router.get("/terrain/hillshade", summary="Generate hillshade PNG from SRTM")
async def get_terrain_hillshade():
    """Return hillshade PNG overlay (pattern identical to /vs30/raster)."""
    if not os.path.exists(SRTM_PATH):
        raise HTTPException(404, "SRTM elevation data not available. File not found.")

    async with _lock_terrain:
        if _terrain_cache["hillshade_png"] is None:
            generate_hillshade_image()
        if _terrain_cache["hillshade_png"] is None:
            raise HTTPException(500, "Failed to generate hillshade image")

        bbox = _terrain_cache["hillshade_bbox"]
        bounds_str = ",".join(map(str, bbox))
        return StreamingResponse(
            io.BytesIO(_terrain_cache["hillshade_png"]),
            media_type="image/png",
            headers={"X-Bounds": bounds_str},
        )


@router.get("/terrain/bounds", summary="Get SRTM raster bounding box")
async def get_terrain_bounds():
    if not os.path.exists(SRTM_PATH):
        raise HTTPException(404, "SRTM elevation data not available")

    async with _lock_terrain:
        if _terrain_cache["hillshade_bbox"] is None:
            generate_hillshade_image()
        if _terrain_cache["hillshade_bbox"] is None:
            # Fallback: read bounds from raster directly
            try:
                with rasterio.open(SRTM_PATH) as src:
                    b = src.bounds
                    bbox = [b.left, b.top, b.right, b.bottom]
            except Exception as exc:
                raise HTTPException(500, f"Failed to read SRTM bounds: {exc}") from exc
        else:
            bbox = _terrain_cache["hillshade_bbox"]

    return JSONResponse(content={"bounds": bbox})


@router.get("/terrain/elevation", summary="Query elevation + slope for a coordinate")
async def query_terrain_elevation(
    lat: float = Query(..., description="Latitude", example=-6.2),
    lng: float = Query(..., description="Longitude", example=106.8),
):
    if not (-11.0 <= lat <= 6.0 and 95.0 <= lng <= 141.0):
        raise HTTPException(400, "Koordinat di luar wilayah Indonesia")

    if not os.path.exists(SRTM_PATH):
        return JSONResponse(content={
            "lat": lat, "lng": lng,
            "elevation_m": None,
            "slope_deg": None,
            "slope_class": None,
            "message": "SRTM data not available on server",
        })

    try:
        with rasterio.open(SRTM_PATH) as src:
            row, col = src.index(lng, lat)
            if not (0 <= row < src.height and 0 <= col < src.width):
                return JSONResponse(content={
                    "lat": lat, "lng": lng,
                    "elevation_m": None, "slope_deg": None,
                    "slope_class": None,
                    "message": "Point outside SRTM raster extent",
                })
            data = src.read(1, window=Window(col, row, 1, 1))
            elev = float(data[0, 0])
            nodata = src.nodata
            if (nodata is not None and elev == nodata) or elev <= -9999:
                return JSONResponse(content={
                    "lat": lat, "lng": lng,
                    "elevation_m": None, "slope_deg": None,
                    "slope_class": None,
                    "message": "No elevation data at this point (ocean or void)",
                })

            # Compute slope at this point from local 3x3 window
            # CRITICAL: convert gradient from degree-pixels to meters
            try:
                pixel_size_deg = src.transform.a
                window = Window(max(0, col - 1), max(0, row - 1), min(3, src.width - col), min(3, src.height - row))
                elev_block = src.read(1, window=window).astype(np.float64)
                if elev_block.size >= 4:
                    lat_rad = np.radians(lat)
                    cell_size_y = 110540.0 * pixel_size_deg
                    cell_size_x = 111320.0 * np.cos(lat_rad) * pixel_size_deg
                    dy, dx = np.gradient(elev_block)
                    dy_m = dy / cell_size_y
                    dx_m = dx / cell_size_x
                    slope_rad = np.arctan(np.sqrt(dx_m**2 + dy_m**2))
                    slope_deg = min(float(np.mean(slope_rad) * 180.0 / np.pi), 89.0)
                else:
                    slope_deg = 0.0
            except Exception:
                slope_deg = 0.0

            if slope_deg < 5:
                slope_class = "flat"
            elif slope_deg < 15:
                slope_class = "gentle"
            elif slope_deg < 30:
                slope_class = "moderate"
            else:
                slope_class = "steep"

            return JSONResponse(content={
                "lat": lat, "lng": lng,
                "elevation_m": round(elev, 1),
                "slope_deg": round(slope_deg, 1),
                "slope_class": slope_class,
                "source": "SRTM",
            })

    except Exception as exc:
        logger.exception("[NOAA] Terrain point query failed")
        raise HTTPException(500, f"Gagal membaca data elevasi: {exc}") from exc