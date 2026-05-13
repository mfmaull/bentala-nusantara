# routers/gempa.py
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import httpx, logging, json, asyncio
from datetime import datetime, timezone, timedelta

router = APIRouter()
logger = logging.getLogger(__name__)

BMKG_URL = "https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Referer": "https://www.bmkg.go.id/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
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
                logger.info("[GEMPA] Cache valid, return cache")
                return Response(
                    content=_cache["data"],
                    media_type="application/json"
                )
            logger.info("[GEMPA] Cache expired, fetch baru")

        # Fetch dari BMKG
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(BMKG_URL, headers=HEADERS)
                res.raise_for_status()
                data = res.json()
        except httpx.TimeoutException:
            raise HTTPException(504, "Timeout ke server BMKG")
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"BMKG error: {e.response.status_code}")
        except Exception as e:
            raise HTTPException(500, f"Error: {str(e)}")

        # Ambil waktu BMKG untuk cache expiry
        dt_str = data.get("Infogempa", {}).get("gempa", {}).get("DateTime", "")
        bmkg_dt = (
            datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt_str else now
        )

        # Simpan ke cache
        _cache["data"] = json.dumps(data, ensure_ascii=False)
        _cache["bmkg_datetime"] = bmkg_dt
        logger.info(f"[GEMPA] Cache diperbarui. Waktu BMKG: {bmkg_dt}")

        return Response(
            content=_cache["data"],
            media_type="application/json"
        )