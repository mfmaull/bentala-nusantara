# routers/populasi.py
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
import logging
import asyncio
import json
import os
from datetime import datetime, timezone

import rasterio
from rasterio.windows import Window
import numpy as np
from shapely.geometry import shape, Point
from shapely.ops import transform as shp_transform
from functools import partial
import pyproj

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------- PATH (struktur di Space: /app/assets/json/) ----------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "assets", "json")
GADM_PATH = os.path.join(ASSETS_DIR, "gadm36_IDN_2.json")
WORLDPOP_TIF_PATH = os.path.join(ASSETS_DIR, "idn_pop_2026_CN_1km_R2025A_UA_v1.tif")

# Cache global untuk GeoJSON hasil prekomputasi
_geojson_cache = {
    "data": None,
    "fetched_at": None,
    "ready": False,
    "error": None
}
_lock = asyncio.Lock()

def load_gadm():
    """Muat file GeoJSON GADM level 2 Indonesia"""
    if not os.path.exists(GADM_PATH):
        raise FileNotFoundError(f"GADM tidak ditemukan: {GADM_PATH}")
    with open(GADM_PATH, encoding="utf-8") as f:
        return json.load(f)

async def compute_density_geojson_background():
    """Prekomputasi kepadatan penduduk untuk semua kabupaten (dijalankan sekali saat startup)"""
    global _geojson_cache
    logger.info("⏳ Memulai prekomputasi kepadatan penduduk (background) ...")
    try:
        gadm_data = load_gadm()
        features = gadm_data["features"]
        
        with rasterio.open(WORLDPOP_TIF_PATH) as src:
            raster_crs = src.crs
            new_features = []
            total = len(features)
            
            for idx, feat in enumerate(features):
                geom = feat["geometry"]
                if geom["type"] not in ("Polygon", "MultiPolygon"):
                    continue
                
                shp = shape(geom)
                
                # Hitung luas area dalam km² menggunakan proyeksi UTM
                centroid = shp.centroid
                utm_zone = int((centroid.x + 180) // 6) + 1
                hemisphere = "south" if centroid.y < 0 else "north"
                utm_crs = pyproj.CRS(f"+proj=utm +zone={utm_zone} +{hemisphere} +ellps=WGS84")
                project_to_utm = partial(
                    pyproj.transform,
                    pyproj.Proj(raster_crs),
                    pyproj.Proj(utm_crs),
                    always_xy=True
                )
                shp_utm = shp_transform(project_to_utm, shp)
                luas_km2 = shp_utm.area / 1e6
                
                # Sampling grid 10x10 di dalam poligon
                minx, miny, maxx, maxy = shp.bounds
                cols, rows = 10, 10
                dx = (maxx - minx) / cols if cols > 0 else 0
                dy = (maxy - miny) / rows if rows > 0 else 0
                pop_vals = []
                
                for c in range(cols):
                    for r in range(rows):
                        x = minx + dx * (c + 0.5)
                        y = maxy - dy * (r + 0.5)   # karena raster sumbu Y ke bawah
                        pt = Point(x, y)
                        if not shp.contains(pt):
                            continue
                        try:
                            row_idx, col_idx = src.index(x, y)
                            if 0 <= row_idx < src.height and 0 <= col_idx < src.width:
                                data = src.read(1, window=Window(col_idx, row_idx, 1, 1))
                                val = data[0, 0]
                                if val > 0:
                                    pop_vals.append(val)
                        except Exception:
                            continue
                
                kepadatan = 0
                total_pop = 0
                if pop_vals:
                    kepadatan = int(round(np.mean(pop_vals)))
                    total_pop = int(kepadatan * luas_km2)
                
                new_props = {
                    "name": feat["properties"].get("NAME_2", ""),
                    "regency": feat["properties"].get("NAME_2", ""),
                    "province": feat["properties"].get("NAME_1", ""),
                    "kode": feat["properties"].get("GID_2", ""),
                    "kepadatan": kepadatan,
                    "luas_km2": round(luas_km2, 2),
                    "total_pop": total_pop,
                }
                new_features.append({
                    "type": "Feature",
                    "properties": new_props,
                    "geometry": geom,
                })
                
                if (idx + 1) % 50 == 0:
                    logger.info(f"  Progres prekomputasi: {idx+1}/{total}")
            
            geojson = {
                "type": "FeatureCollection",
                "features": new_features,
                "metadata": {
                    "source": "WorldPop 2026 & GADM",
                    "processed_at": datetime.now(timezone.utc).isoformat()
                }
            }
            
            async with _lock:
                _geojson_cache["data"] = geojson
                _geojson_cache["fetched_at"] = datetime.now(timezone.utc)
                _geojson_cache["ready"] = True
                _geojson_cache["error"] = None
            
            logger.info(f"✅ Prekomputasi selesai, {len(new_features)} kabupaten diproses.")
            
    except Exception as e:
        logger.exception("Gagal prekomputasi populasi")
        async with _lock:
            _geojson_cache["ready"] = False
            _geojson_cache["error"] = str(e)

@router.get("/populasi/geojson")
async def get_population_geojson():
    """Mengembalikan GeoJSON kepadatan penduduk (hasil prekomputasi)"""
    if not _geojson_cache["ready"]:
        if _geojson_cache["error"]:
            raise HTTPException(500, f"Prekomputasi gagal: {_geojson_cache['error']}")
        else:
            raise HTTPException(503, "Data populasi sedang dipersiapkan, silakan coba lagi dalam beberapa menit.")
    return JSONResponse(content=_geojson_cache["data"])

@router.get("/populasi/point")
async def get_population_point(lat: float, lon: float):
    """Query kepadatan penduduk pada titik koordinat tertentu (ringan, cepat)"""
    if not os.path.exists(WORLDPOP_TIF_PATH):
        raise HTTPException(404, "File raster populasi tidak ditemukan")
    try:
        with rasterio.open(WORLDPOP_TIF_PATH) as src:
            row, col = src.index(lon, lat)
            if 0 <= row < src.height and 0 <= col < src.width:
                nilai = src.read(1, window=Window(col, row, 1, 1))[0, 0]
                if nilai > 0:
                    return {"lat": lat, "lon": lon, "populasi": int(nilai)}
                else:
                    return {"lat": lat, "lon": lon, "populasi": 0, "message": "NoData (laut atau area tidak berpenduduk)"}
            else:
                raise HTTPException(400, "Koordinat di luar cakupan raster")
    except Exception as e:
        raise HTTPException(500, f"Gagal membaca raster: {str(e)}")