# routers/populasi.py
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
import logging, asyncio, json, os
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

# --------------------------------- PATH (STRUKTUR PROYEK TERBARU) --------------------------------
# populasi.py ada di V1_2/backend/routers/  -> naik 3 level untuk sampai ke root V1_2/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASSETS_DIR = os.path.join(BASE_DIR, "assets", "json")

GADM_PATH = os.path.join(ASSETS_DIR, "gadm36_IDN_2.json")
WORLDPOP_TIF_PATH = os.path.join(ASSETS_DIR, "idn_pop_2026_CN_1km_R2025A_UA_v1.tif")   # sesuai screenshot

# Cache
_cache_geojson = {"data": None, "fetched_at": None}
_lock_pop = asyncio.Lock()


def load_gadm():
    if not os.path.exists(GADM_PATH):
        raise FileNotFoundError(f"GADM tidak ditemukan: {GADM_PATH}")
    with open(GADM_PATH, encoding="utf-8") as f:
        return json.load(f)


def compute_density_geojson():
    logger.info("⏳ Memulai perhitungan kepadatan penduduk...")
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

            # Hitung luas di proyeksi UTM
            centroid = shp.centroid
            utm_zone = int((centroid.x + 180) / 6) + 1
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

            # Sampling grid 10×10
            minx, miny, maxx, maxy = shp.bounds
            cols, rows = 10, 10
            dx = (maxx - minx) / cols
            dy = (maxy - miny) / rows

            pop_vals = []
            for c in range(cols):
                for r in range(rows):
                    x = minx + dx * (c + 0.5)
                    y = maxy - dy * (r + 0.5)   # orientasi raster
                    pt = Point(x, y)
                    if not shp.contains(pt):
                        continue
                    try:
                        row, col = src.index(x, y)
                        if 0 <= row < src.height and 0 <= col < src.width:
                            data = src.read(1, window=Window(col, row, 1, 1))
                            val = data[0, 0]
                            if val > 0:
                                pop_vals.append(val)
                    except Exception:
                        continue

            kepadatan = 0
            total_pop = 0
            if pop_vals:
                kepadatan = int(round(np.mean(pop_vals)))   # sesuai logika frontend
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
                logger.info(f"  Progres: {idx+1}/{total} kabupaten")

        geojson = {
            "type": "FeatureCollection",
            "features": new_features,
            "metadata": {
                "source": "WorldPop 2026 & GADM",
                "processed_at": datetime.now(timezone.utc).isoformat()
            }
        }
        logger.info(f"✅ Selesai, {len(new_features)} kabupaten diproses.")
        return geojson


@router.get("/populasi/geojson")
async def get_population_geojson():
    async with _lock_pop:
        if _cache_geojson["data"] is not None:
            logger.info("[POP] Mengembalikan dari cache")
            return JSONResponse(content=_cache_geojson["data"])

        try:
            geojson = compute_density_geojson()
        except Exception as e:
            logger.exception("Gagal memproses populasi")
            raise HTTPException(500, f"Gagal: {str(e)}")

        _cache_geojson["data"] = geojson
        _cache_geojson["fetched_at"] = datetime.now(timezone.utc)
        return JSONResponse(content=geojson)