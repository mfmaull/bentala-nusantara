# routers/cuaca.py
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
import httpx, logging, asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

router = APIRouter()
logger = logging.getLogger(__name__)

BMKG_RSS_URL = "https://www.bmkg.go.id/alerts/nowcast/id/rss.xml"
CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Referer": "https://www.bmkg.go.id/",
    "Accept": "application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
}

# Cache alerts RSS — 15 menit cukup, data cuaca tidak berubah per detik
_cache_alerts = {"data": None, "fetched_at": None}
_lock_alerts = asyncio.Lock()


def _parse_rss(xml_text: str) -> list:
    """Parse RSS XML BMKG → list of alert dict"""
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    alerts = []
    for item in channel.findall("item"):
        title = item.findtext("title", "")
        link  = item.findtext("link", "")
        desc  = item.findtext("description", "")
        pub   = item.findtext("pubDate", "")
        alerts.append({
            "title": title,
            "link": link,
            "description": desc,
            "pubDate": pub,
        })
    return alerts


def _parse_cap(xml_text: str) -> dict:
    """Parse CAP XML BMKG → dict dengan polygon, severity, dll"""
    ns = {"cap": CAP_NS}
    root = ET.fromstring(xml_text)

    def cap(tag):
        return root.find(f".//cap:{tag}", ns)

    def cap_text(tag, default=""):
        el = cap(tag)
        return el.text.strip() if el is not None and el.text else default

    # Ambil semua polygon dari semua <area>
    polygons = []
    for area in root.findall(f".//cap:area", ns):
        for poly in area.findall(f"cap:polygon", ns):
            if not poly.text:
                continue
            coords = []
            for pair in poly.text.strip().split():
                parts = pair.split(",")
                if len(parts) >= 2:
                    try:
                        lat, lon = float(parts[0]), float(parts[1])
                        coords.append([lon, lat])  # GeoJSON: [lng, lat]
                    except ValueError:
                        continue
            if len(coords) >= 3:
                polygons.append(coords)

    return {
        "headline":    cap_text("headline"),
        "event":       cap_text("event"),
        "effective":   cap_text("effective"),
        "expires":     cap_text("expires"),
        "urgency":     cap_text("urgency"),
        "severity":    cap_text("severity"),
        "certainty":   cap_text("certainty"),
        "areaDesc":    cap_text("areaDesc"),
        "instruction": cap_text("instruction"),
        "senderName":  cap_text("senderName"),
        "web":         cap_text("web"),
        "polygons":    polygons,  # list of GeoJSON coordinate arrays
    }


@router.get("/cuaca/alerts", summary="List peringatan cuaca aktif dari BMKG")
async def get_weather_alerts():
    """
    Mengambil feed RSS peringatan cuaca BMKG.
    Cache 15 menit — data cuaca tidak berubah per detik.
    Return: list of {title, link, description, pubDate}
    """
    async with _lock_alerts:
        now = datetime.now(timezone.utc)

        # Cek cache
        if _cache_alerts["data"] and _cache_alerts["fetched_at"]:
            age = now - _cache_alerts["fetched_at"]
            if age < timedelta(minutes=15):
                logger.info(f"[CUACA] Cache valid (umur: {int(age.total_seconds())}s)")
                return JSONResponse(content=_cache_alerts["data"])
            logger.info("[CUACA] Cache expired, fetch baru")

        # Fetch RSS dari BMKG
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(BMKG_RSS_URL, headers=HEADERS)
                res.raise_for_status()
                xml_text = res.text
        except httpx.TimeoutException:
            raise HTTPException(504, "Timeout ke server BMKG")
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"BMKG error: {e.response.status_code}")
        except Exception as e:
            raise HTTPException(500, f"Error: {str(e)}")

        # Parse XML → JSON
        try:
            alerts = _parse_rss(xml_text)
        except ET.ParseError as e:
            logger.error(f"[CUACA] Gagal parse XML: {e}")
            raise HTTPException(502, "Format RSS BMKG tidak valid")

        result = {
            "total": len(alerts),
            "fetched_at": now.isoformat(),
            "alerts": alerts,
        }

        _cache_alerts["data"] = result
        _cache_alerts["fetched_at"] = now
        logger.info(f"[CUACA] Cache diperbarui. Total: {len(alerts)} peringatan")

        return JSONResponse(content=result)


@router.get("/cuaca/detail", summary="Detail peringatan cuaca (CAP XML)")
async def get_weather_detail(url: str = Query(..., description="URL CAP XML dari field 'link' di /alerts")):
    """
    Fetch dan parse detail CAP XML untuk satu peringatan.
    Tidak di-cache karena URL-nya unik per peringatan.
    Return: {headline, event, polygons, severity, certainty, dll}
    """
    # Validasi URL — hanya izinkan domain BMKG
    if "bmkg.go.id" not in url:
        raise HTTPException(400, "URL harus berasal dari domain bmkg.go.id")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=HEADERS)
            res.raise_for_status()
            xml_text = res.text
    except httpx.TimeoutException:
        raise HTTPException(504, "Timeout ke server BMKG")
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"BMKG error: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(500, f"Error: {str(e)}")

    try:
        detail = _parse_cap(xml_text)
    except ET.ParseError as e:
        logger.error(f"[CUACA DETAIL] Gagal parse CAP XML: {e}")
        raise HTTPException(502, "Format CAP XML BMKG tidak valid")

    logger.info(f"[CUACA DETAIL] Berhasil parse: {detail.get('headline', '-')}")
    return JSONResponse(content=detail)