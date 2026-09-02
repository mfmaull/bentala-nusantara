# routers/vs30.py
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from rasterio.windows import Window
import rasterio
import logging
import io
import numpy as np
from PIL import Image
from pathlib import Path

router = APIRouter()
logger = logging.getLogger(__name__)

# ========== KONFIGURASI PATH ==========
CURRENT_DIR = Path(__file__).parent.absolute()
PROJECT_ROOT = CURRENT_DIR.parent
ASSETS_DIR = PROJECT_ROOT / "assets" / "json"
VS30_PATH = ASSETS_DIR / "vs30_wgs84.tif"

logger.info(f"[VS30] Mencari file di: {VS30_PATH}")
logger.info(f"[VS30] File exists = {VS30_PATH.exists()}")

# ========== KLASIFIKASI SNI 1726:2019 ==========
KLASIFIKASI = [
    {"kode": "SA", "nama": "Batuan Keras", "min": 1500, "max": None,  "warna": (69, 117, 180, 220)},
    {"kode": "SB", "nama": "Batuan",       "min": 760,  "max": 1500,  "warna": (145, 207, 96, 220)},
    {"kode": "SC", "nama": "Tanah Keras",  "min": 360,  "max": 760,   "warna": (254, 224, 139, 220)},
    {"kode": "SD", "nama": "Tanah Kaku",   "min": 175,  "max": 360,   "warna": (252, 141, 89, 220)},
    {"kode": "SE", "nama": "Tanah Lunak",  "min": None, "max": 175,   "warna": (215, 48, 39, 220)},
]

def _get_class_color(vs30: float) -> tuple:
    if vs30 > 1500:
        return KLASIFIKASI[0]["warna"]
    if vs30 > 760:
        return KLASIFIKASI[1]["warna"]
    if vs30 > 360:
        return KLASIFIKASI[2]["warna"]
    if vs30 >= 175:
        return KLASIFIKASI[3]["warna"]
    return KLASIFIKASI[4]["warna"]

def _get_klasifikasi(vs30: float) -> dict:
    if vs30 > 1500:
        return KLASIFIKASI[0]
    if vs30 > 760:
        return KLASIFIKASI[1]
    if vs30 > 360:
        return KLASIFIKASI[2]
    if vs30 >= 175:
        return KLASIFIKASI[3]
    return KLASIFIKASI[4]

# ========== CACHE ==========
_raster_image_cache: bytes | None = None
_raster_bbox: list[float] | None = None

def generate_raster_image() -> None:
    global _raster_image_cache, _raster_bbox
    try:
        logger.info("[VS30] Memulai generate image raster...")
        with rasterio.open(VS30_PATH) as src:
            data = src.read(1)
            height, width = data.shape
            bounds = src.bounds
            _raster_bbox = [bounds[0], bounds[3], bounds[2], bounds[1]]
            logger.info(f"Bounding box asli: {_raster_bbox}")

            # Jika file sudah cukup kecil (<= 2000 piksel), gunakan langsung
            MAX_SIZE = 2000
            if width <= MAX_SIZE and height <= MAX_SIZE:
                downsampled = data
                h, w = height, width
                logger.info(f"Ukuran asli sudah kecil, tidak perlu downsampling: {w} x {h}")
            else:
                # Hitung target ukuran
                if width > height:
                    new_width = MAX_SIZE
                    new_height = int(height * MAX_SIZE / width)
                else:
                    new_height = MAX_SIZE
                    new_width = int(width * MAX_SIZE / height)
                
                # Pastikan stride tidak nol
                stride_y = max(1, height // new_height)
                stride_x = max(1, width // new_width)
                downsampled = data[::stride_y, ::stride_x][:new_height, :new_width]
                h, w = downsampled.shape
                logger.info(f"Ukuran setelah downsampling: {w} x {h} (stride: {stride_x}, {stride_y})")

            # Buat array RGBA
            img_array = np.zeros((h, w, 4), dtype=np.uint8)
            for i in range(h):
                for j in range(w):
                    val = downsampled[i, j]
                    if val <= 0:
                        img_array[i, j] = (0, 0, 0, 0)
                    else:
                        img_array[i, j] = _get_class_color(val)

            img = Image.fromarray(img_array, 'RGBA')
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            buffer.seek(0)
            _raster_image_cache = buffer.getvalue()
            logger.info("[VS30] Image raster siap")

    except Exception as e:
        logger.exception("Gagal generate raster Vs30")
        raise HTTPException(500, f"Gagal membuat peta Vs30: {str(e)}")

# ========== ENDPOINTS ==========
@router.get("/vs30/raster", summary="Hasilkan peta Vs30 seluruh Indonesia")
async def get_vs30_raster():
    if not VS30_PATH.exists():
        raise HTTPException(500, "File Vs30 tidak ditemukan di server")

    global _raster_image_cache, _raster_bbox
    if _raster_image_cache is None:
        generate_raster_image()

    bounds_str = ",".join(map(str, _raster_bbox))
    return StreamingResponse(
        io.BytesIO(_raster_image_cache),
        media_type="image/png",
        headers={"X-Bounds": bounds_str}
    )

@router.get("/vs30/bounds", summary="Dapatkan bounding box raster Vs30")
async def get_vs30_bounds():
    if not VS30_PATH.exists():
        raise HTTPException(500, "File Vs30 tidak ditemukan di server")

    global _raster_bbox
    if _raster_bbox is None:
        generate_raster_image()

    return JSONResponse(content={"bounds": _raster_bbox})

@router.get("/vs30", summary="Query nilai Vs30 per koordinat")
async def query_vs30(
    lng: float = Query(..., description="Longitude", example=121.79),
    lat: float = Query(..., description="Latitude (negatif = LS)", example=-4.08),
):
    if not VS30_PATH.exists():
        logger.error(f"File tidak ditemukan: {VS30_PATH}")
        raise HTTPException(500, "File Vs30 tidak ditemukan di server")

    if not (-11.0 <= lat <= 6.0 and 95.0 <= lng <= 141.0):
        raise HTTPException(400, "Koordinat di luar wilayah Indonesia (lat: -11 s/d 6, lng: 95 s/d 141)")

    try:
        with rasterio.open(VS30_PATH) as src:
            row, col = src.index(lng, lat)
            if not (0 <= row < src.height and 0 <= col < src.width):
                raise HTTPException(404, "Koordinat di luar cakupan raster Vs30")

            data = src.read(1, window=Window(col, row, 1, 1))
            vs30_val = float(data[0, 0])
            nodata = src.nodata
            is_nodata = (nodata is not None and vs30_val == nodata) or vs30_val <= 0

            if is_nodata:
                return JSONResponse(content={
                    "koordinat": {"lng": lng, "lat": lat},
                    "vs30": None,
                    "satuan": "m/s",
                    "klasifikasi": None,
                    "pesan": "Tidak ada data Vs30 pada titik ini (laut atau area kosong)",
                    "standar": "SNI 1726:2019",
                })

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error membaca raster pada ({lng}, {lat})")
        raise HTTPException(500, f"Gagal membaca data Vs30: {str(e)}")

    klas = _get_klasifikasi(vs30_val)
    return JSONResponse(content={
        "koordinat": {"lng": lng, "lat": lat},
        "vs30": round(vs30_val, 2),
        "satuan": "m/s",
        "klasifikasi": {
            "kode": klas["kode"],
            "nama": klas["nama"],
            "warna": klas["warna"],
        },
        "standar": "SNI 1726:2019",
    })