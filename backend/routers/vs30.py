# routers/vs30.py
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from rasterio.windows import Window
import rasterio, logging, os

router = APIRouter()
logger = logging.getLogger(__name__)

# ── Path (ikuti pola populasi.py — naik 3 level ke root V1_2/) ──────────────
BASE_DIR   = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASSETS_DIR = os.path.join(BASE_DIR, "assets", "json")
VS30_PATH  = os.path.join(ASSETS_DIR, "vs30_indonesia_cog.tif")

# ── Klasifikasi Situs SNI 1726:2019 ─────────────────────────────────────────
# Urutan: dari nilai tertinggi ke terendah
KLASIFIKASI = [
    {"kode": "SA", "nama": "Batuan Keras", "min": 1500, "max": None,  "warna": "#4575b4"},
    {"kode": "SB", "nama": "Batuan",       "min": 760,  "max": 1500,  "warna": "#91cf60"},
    {"kode": "SC", "nama": "Tanah Keras",  "min": 360,  "max": 760,   "warna": "#fee08b"},
    {"kode": "SD", "nama": "Tanah Kaku",   "min": 175,  "max": 360,   "warna": "#fc8d59"},
    {"kode": "SE", "nama": "Tanah Lunak",  "min": None, "max": 175,   "warna": "#d73027"},
]

def _get_klasifikasi(vs30: float) -> dict:
    """Klasifikasi situs berdasarkan SNI 1726:2019"""
    if vs30 > 1500: return KLASIFIKASI[0]
    if vs30 > 760:  return KLASIFIKASI[1]
    if vs30 > 360:  return KLASIFIKASI[2]
    if vs30 >= 175: return KLASIFIKASI[3]
    return KLASIFIKASI[4]


@router.get("/vs30", summary="Query nilai Vs30 per koordinat")
async def query_vs30(
    lng: float = Query(..., description="Longitude", example=121.79),
    lat: float = Query(..., description="Latitude (negatif = LS)", example=-4.08),
):
    """
    Membaca nilai Vs30 dari raster COG pada titik koordinat yang diberikan.
    Mengembalikan nilai Vs30 (m/s) dan klasifikasi situs SNI 1726:2019.
    Tidak di-cache — baca 1 pixel dari COG sangat cepat (~milidetik).
    """
    # ── Validasi file ────────────────────────────────────────────────────────
    if not os.path.exists(VS30_PATH):
        logger.error(f"[VS30] File tidak ditemukan: {VS30_PATH}")
        raise HTTPException(500, "File Vs30 tidak ditemukan di server")

    # ── Validasi koordinat (bounding box Indonesia) ──────────────────────────
    if not (-11.0 <= lat <= 6.0 and 95.0 <= lng <= 141.0):
        raise HTTPException(400, "Koordinat di luar wilayah Indonesia (lat: -11 s/d 6, lng: 95 s/d 141)")

    # ── Baca raster ──────────────────────────────────────────────────────────
    try:
        with rasterio.open(VS30_PATH) as src:
            # Konversi koordinat geografis → indeks pixel
            row, col = src.index(lng, lat)

            # Cek apakah pixel masih di dalam cakupan raster
            if not (0 <= row < src.height and 0 <= col < src.width):
                raise HTTPException(404, "Koordinat di luar cakupan raster Vs30")

            # Baca 1 pixel saja (Window efisien untuk COG)
            data  = src.read(1, window=Window(col, row, 1, 1))
            vs30_val = float(data[0, 0])

            # Cek nodata — dari metadata raster atau nilai 0
            nodata = src.nodata
            is_nodata = (nodata is not None and vs30_val == nodata) or vs30_val <= 0

            if is_nodata:
                logger.info(f"[VS30] Nodata pada ({lng}, {lat})")
                return JSONResponse(content={
                    "koordinat": {"lng": lng, "lat": lat},
                    "vs30": None,
                    "satuan": "m/s",
                    "klasifikasi": None,
                    "pesan": "Tidak ada data Vs30 pada titik ini (laut atau area kosong)",
                    "standar": "SNI 1726:2019",
                })

    except HTTPException:
        raise  # teruskan HTTP error yang sudah kita buat
    except Exception as e:
        logger.exception(f"[VS30] Error membaca raster pada ({lng}, {lat})")
        raise HTTPException(500, f"Gagal membaca data Vs30: {str(e)}")

    # ── Klasifikasi & response ───────────────────────────────────────────────
    klas = _get_klasifikasi(vs30_val)
    logger.info(f"[VS30] ({lng}, {lat}) → {vs30_val:.1f} m/s → {klas['kode']}")

    return JSONResponse(content={
        "koordinat": {"lng": lng, "lat": lat},
        "vs30": round(vs30_val, 2),
        "satuan": "m/s",
        "klasifikasi": {
            "kode":  klas["kode"],
            "nama":  klas["nama"],
            "warna": klas["warna"],
        },
        "standar": "SNI 1726:2019",
    })