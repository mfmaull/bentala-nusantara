# routers/gempa.py
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
import httpx
import logging
import json
import asyncio
import io
from datetime import datetime, timezone, timedelta

router = APIRouter()
logger = logging.getLogger(__name__)

BMKG_BASE_URL = "https://data.bmkg.go.id/DataMKG/TEWS"
BMKG_GEMPA_URL = f"{BMKG_BASE_URL}/autogempa.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

_cache = {
    "data": None,
    "bmkg_datetime": None,
}
_lock = asyncio.Lock()

@router.get("/gempa")
async def get_gempa():
    async with _lock:
        now = datetime.now(timezone.utc)

        # Cek cache
        if _cache["data"] and _cache["bmkg_datetime"]:
            if now - _cache["bmkg_datetime"] < timedelta(minutes=1):
                logger.info("[GEMPA] Mengembalikan dari cache")
                return Response(content=_cache["data"], media_type="application/json")

        logger.info("[GEMPA] Mengambil data baru dari BMKG")
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(BMKG_GEMPA_URL, headers=HEADERS)
                response.raise_for_status()

                # Coba parsing JSON secara manual dengan deteksi encoding
                try:
                    data = response.json()
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    raw_text = response.content.decode('latin-1')
                    logger.warning(f"Gagal parse JSON, mencoba manual. Error: {e}")
                    if raw_text.strip().startswith('<'):
                        snippet = raw_text[:200]
                        raise HTTPException(502, f"BMKG mengembalikan HTML (mungkin error). Cuplikan: {snippet}")
                    data = json.loads(raw_text)
        except httpx.TimeoutException:
            raise HTTPException(504, "Timeout saat menghubungi BMKG")
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"BMKG mengembalikan status error: {e.response.status_code}")
        except Exception as e:
            logger.exception("Error tidak terduga")
            raise HTTPException(500, f"Error internal: {str(e)}")

        # Ambil waktu gempa dari data
        dt_str = data.get("Infogempa", {}).get("gempa", {}).get("DateTime", "")
        if dt_str:
            try:
                bmkg_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            except ValueError:
                bmkg_dt = now
        else:
            bmkg_dt = now

        # Simpan ke cache
        _cache["data"] = json.dumps(data, ensure_ascii=False)
        _cache["bmkg_datetime"] = bmkg_dt
        logger.info(f"[GEMPA] Cache diperbarui. Waktu BMKG: {bmkg_dt}")

        return Response(content=_cache["data"], media_type="application/json")


@router.get("/shakemap-image")
async def get_shakemap_image(file: str = Query(..., description="Nama file shakemap, misal: 20260513133738.mmi.jpg")):
    """
    Proxy untuk mengambil gambar shakemap dari BMKG.
    Contoh: /shakemap-image?file=20260513133738.mmi.jpg
    """
    image_url = f"{BMKG_BASE_URL}/{file}"
    logger.info(f"[SHAKEMAP] Mengambil gambar dari BMKG: {image_url}")

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(image_url, headers=HEADERS)
            resp.raise_for_status()
            content = await resp.aread()
    except httpx.TimeoutException:
        raise HTTPException(504, "Timeout saat mengambil shakemap dari BMKG")
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"BMKG mengembalikan status error: {e.response.status_code}")
    except Exception as e:
        logger.exception("Error tidak terduga saat fetch shakemap")
        raise HTTPException(500, f"Error internal: {str(e)}")

    # Tentukan media type (default jpeg)
    media_type = "image/jpeg"
    if file.lower().endswith(".png"):
        media_type = "image/png"
    elif file.lower().endswith(".gif"):
        media_type = "image/gif"

    return StreamingResponse(io.BytesIO(content), media_type=media_type)