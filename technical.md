# Technical Documentation — Bentala Nusantara

> Implementation-level documentation for Bentala Nusantara WebGIS.
> Versi 1.0 — Juni 2026

---

## 1. Project Overview

**Bentala Nusantara** adalah platform WebGIS yang mengintegrasikan data kebencanaan real-time (gempa bumi, cuaca ekstrem, longsor) dengan data geospasial statis (kepadatan penduduk, klasifikasi situs Vs30) untuk menyajikan informasi risiko bencana.

**Tumpukan Teknologi:**

| Layer | Teknologi | Deployment |
|-------|-----------|------------|
| Frontend | HTML, Tailwind CSS, MapLibre GL v5, Turf.js, Vanilla JS | Vercel |
| Backend | FastAPI (Python 3.11), Rasterio, Shapely, httpx | Hugging Face Spaces (Docker) |
| Storage | Google Sheets + Apps Script, Google Drive | Gratis (zero-cost) |
| Data Eksternal | BMKG, Open-Meteo (NOAA/NCEP), WorldPop, GADM | Live API |

---

## 2. Project Structure

```
/
├── index.html              # Landing page (~1457 baris)
├── map.html                # Main WebGIS app (~1714 baris, monolith)
├── early-warning.html      # Landslide risk dashboard (~1249 baris)
├── upload.html             # PDF/Excel upload page (~945 baris)
├── config.example.js       # API key template (gitignored)
├── tailwind.config.js      # Tailwind configuration
├── .vercelignore           # Vercel deploy exclusion rules
│
├── api/                    # Vercel serverless functions
│   ├── env.js              # Proxy MAP_SERVICE_KEY environment variable
│   ├── bmkg-cuaca.js       # Proxy BMKG weather data
│   └── bmkg-cuaca-detail.js# Proxy BMKG CAP XML detail
│
├── backend/
│   ├── main.py             # FastAPI entrypoint + CORS + lifespan
│   ├── Dockerfile          # Docker image (python:3.11-slim + GDAL)
│   ├── requirements.txt    # Python dependencies
│   └── routers/
│       ├── gempa.py        # BMKG earthquake (108 baris)
│       ├── cuaca.py        # BMKG weather alerts (179 baris)
│       ├── populasi.py     # WorldPop population density (175 baris)
│       ├── vs30.py         # Vs30 soil classification (191 baris)
│       └── noaa.py         # Open-Meteo hazard aggregation (675 baris)
│
└── assets/json/            # GIS data files (gitignored large files)
    ├── gadm36_IDN_2.json   # GADM admin boundaries level 2
    ├── idn_pop_2026_...tif # WorldPop 2026 raster
    └── vs30_wgs84.tif      # Vs30 COG raster
```

---

## 3. Backend Architecture (FastAPI)

### 3.1 main.py — Entrypoint

**File:** `backend/main.py`

```python
# Lifespan — menjalankan background task prekomputasi populasi saat startup
@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(populasi.compute_density_geojson_background())
    yield

app = FastAPI(title="Bentala Nusantara API", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])

# Semua router mount di prefix /api
app.include_router(gempa.router,    prefix="/api")
app.include_router(cuaca.router,    prefix="/api")
app.include_router(populasi.router, prefix="/api")
app.include_router(vs30.router,     prefix="/api")
app.include_router(noaa.router,     prefix="/api")

# Health check
@app.get("/") => {"status": "OK", "modules": ["gempa","cuaca","populasi","vs30","noaa"]}
```

**Detail:**
- CORS: Allow all origins (`allow_origins=["*"]`), hanya metode GET
- Port: 7860 (default Hugging Face Spaces) via Uvicorn
- Startup: Prekomputasi populasi di background (asyncio task)
- Semua endpoint diakses dengan prefix `/api`

### 3.2 Caching Strategy

Pola cache seragam di semua router — `asyncio.Lock()` + TTL dict.

```python
_cache = {"data": None, "fetched_at": None}
_lock = asyncio.Lock()

async def handler():
    async with _lock:
        if cache valid (< TTL):
            return cache
        data = await fetch_source()
        update cache
        return data
```

**TTL per Modul:**

| Modul | TTL | Penyimpanan |
|-------|-----|-------------|
| Gempa | 1 menit | JSON string in-memory |
| Cuaca | 15 menit | JSON dict in-memory |
| Populasi | Permanent (precomputed sekali saat startup) | GeoJSON dict in-memory |
| Vs30 raster | Permanent (generated once) | PNG bytes in-memory |
| NOAA Early Warning | 60 menit | JSON dict in-memory |

### 3.3 Error Handling

Semua router mengikuti pola seragam:
- Timeout sumber eksternal → `HTTPException(504)`
- HTTP error sumber eksternal → `HTTPException(502)`
- Internal error → `HTTPException(500)` + `logger.exception()`
- Koordinat di luar Indonesia → `HTTPException(400)`

---

## 4. API Specification

### 4.1 Gempa — `routers/gempa.py`

**Sumber:** BMKG — `https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json`

#### `GET /api/gempa`

| Item | Detail |
|------|--------|
| Method | GET |
| Parameter | Tidak ada |
| Response | Forward response dari BMKG (JSON) |
| Cache | 1 menit, berdasarkan `DateTime` dari BMKG |
| Headers BMKG | `User-Agent: Mozilla/5.0 (...) Chrome/91...` |
| Error handling | Timeout→504, HTML response→502, Parse error→502 |

Contoh response:
```json
{
  "Infogempa": {
    "gempa": {
      "Magnitude": "5.2",
      "Kedalaman": "10 km",
      "Wilayah": "Laut Jawa",
      "DateTime": "2026-06-08T10:30:00+07:00",
      "Coordinates": "-7.5,110.5",
      "Lintang": "-7.5 LS",
      "Bujur": "110.5 BT",
      "Potensi": "tidak berpotensi tsunami"
    }
  }
}
```

#### `GET /api/shakemap-image`

| Item | Detail |
|------|--------|
| Method | GET |
| Parameter | `file` (query, required) — nama file, contoh: `20260513133738.mmi.jpg` |
| Response | StreamingResponse image (jpeg/png/gif) |
| Cache | Tidak ada (forward langsung) |

---

### 4.2 Cuaca — `routers/cuaca.py`

**Sumber:** BMKG RSS — `https://www.bmkg.go.id/alerts/nowcast/id/rss.xml` + CAP XML

#### `GET /api/cuaca/alerts`

| Item | Detail |
|------|--------|
| Method | GET |
| Parameter | Tidak ada |
| Cache | 15 menit |
| Proses | Fetch RSS → parse XML dengan `xml.etree.ElementTree` → extract `item` elements (title, link, description, pubDate) |

Response:
```json
{
  "total": 3,
  "fetched_at": "2026-06-08T10:30:00+00:00",
  "alerts": [
    {
      "title": "Peringatan Dini Cuaca Jawa Barat",
      "link": "https://www.bmkg.go.id/cap/...",
      "description": "...",
      "pubDate": "Mon, 08 Jun 2026 10:00:00 GMT"
    }
  ]
}
```

#### `GET /api/cuaca/detail`

| Item | Detail |
|------|--------|
| Method | GET |
| Parameter | `url` (query, required) — URL CAP XML dari field `link` di `/alerts` |
| Validasi | URL harus mengandung `bmkg.go.id` |
| Cache | Tidak ada (URL unik per peringatan) |
| Proses | Fetch CAP XML → parse dengan `xml.etree.ElementTree` → extrak polygon (konversi `lat,lon` → `[lon, lat]`), severity, certainty, dll |

Response:
```json
{
  "headline": "Peringatan Dini Cuaca Jawa Barat",
  "event": "Hujan Lebat",
  "effective": "2026-06-08T10:00:00+07:00",
  "expires": "2026-06-08T13:00:00+07:00",
  "urgency": "Expected",
  "severity": "Moderate",
  "certainty": "Likely",
  "areaDesc": "Kab. Bogor, Kab. Sukabumi",
  "instruction": "Waspada potensi banjir...",
  "senderName": "BMKG",
  "polygons": [[[106.5, -6.5], [107.0, -6.5], ...]]
}
```

---

### 4.3 Populasi — `routers/populasi.py`

**Sumber:** WorldPop 2026 raster (1km resolution) + GADM 3.6 GeoJSON level 2

**Prekomputasi Startup:**
1. Load GADM GeoJSON (~500 kabupaten)
2. Untuk setiap kabupaten: hitung luas area dengan proyeksi UTM (`pyproj`)
3. Sampling 10×10 grid point di dalam polygon
4. Baca nilai populasi dari WorldPop raster per point (`rasterio.Window`)
5. Rata-rata nilai → kepadatan (jiwa/km²) × luas → total populasi
6. Simpan hasil sebagai GeoJSON di memory

#### `GET /api/populasi/geojson`

| Item | Detail |
|------|--------|
| Method | GET |
| Parameter | Tidak ada |
| Response | GeoJSON FeatureCollection |
| Status code | 503 jika prekomputasi belum selesai |
| Properties | `name`, `regency`, `province`, `kode` (GID_2), `kepadatan`, `luas_km2`, `total_pop` |

#### `GET /api/populasi/point`

| Item | Detail |
|------|--------|
| Method | GET |
| Parameter | `lat` (query), `lon` (query) |
| Response | `{"lat": -6.175, "lon": 106.827, "populasi": 15000}` |
| Metode | `rasterio.Window` single-pixel read saja |

---

### 4.4 Vs30 — `routers/vs30.py`

**Sumber:** Vs30 COG raster (Cloud-Optimized GeoTIFF) — SNI 1726:2019

**Klasifikasi Situs (Fixed Standard):**

| Kode | Nama | Rentang (m/s) | Warna RGBA |
|------|------|---------------|------------|
| SA | Batuan Keras | > 1500 | (69, 117, 180, 220) |
| SB | Batuan | 760 – 1500 | (145, 207, 96, 220) |
| SC | Tanah Keras | 360 – 760 | (254, 224, 139, 220) |
| SD | Tanah Kaku | 175 – 360 | (252, 141, 89, 220) |
| SE | Tanah Lunak | < 175 | (215, 48, 39, 220) |

#### `GET /api/vs30/raster`

| Item | Detail |
|------|--------|
| Method | GET |
| Parameter | Tidak ada |
| Response | StreamingResponse (image/png) + header `X-Bounds` |
| Proses | Baca raster → downsize max 2000px → warna per pixel → PNG RGBA |

#### `GET /api/vs30/bounds`

| Item | Detail |
|------|--------|
| Method | GET |
| Response | `{"bounds": [minLng, maxLat, maxLng, minLat]}` |

#### `GET /api/vs30`

| Item | Detail |
|------|--------|
| Method | GET |
| Parameter | `lng` (query), `lat` (query) |
| Validasi | lat: -11 s/d 6, lng: 95 s/d 141 |
| Response | `{"koordinat":{...}, "vs30": 450.25, "klasifikasi":{...}, "standar":"SNI 1726:2019"}` |

---

### 4.5 NOAA / Early Warning — `routers/noaa.py`

**Sumber:** Open-Meteo API — `https://api.open-meteo.com/v1/forecast`

#### `GET /api/early-warning/indonesia`

| Item | Detail |
|------|--------|
| Method | GET |
| Parameter | `forecast_days` (1-7, default=3), `admin_level` (province/kabupaten), `include_geometry` (bool) |
| Cache | 60 menit per kombinasi parameter |

**Proses internal:**
1. Load GADM boundaries (~500 kabupaten)
2. Batch request ke Open-Meteo (50 kabupaten per batch, max 4 concurrent)
3. Variabel: `soil_moisture_0_to_7cm`, `rain`, `precipitation_probability`
4. Hitung skor risiko berdasarkan threshold
5. Agregasi per provinsi/kabupaten
6. Return GeoJSON + summary + markers

**Risk thresholds:**
```
soil_moisture:  medium ≥ 0.35, high ≥ 0.42
rainfall_3day:  medium ≥ 75mm, high ≥ 150mm
rainfall_intensity: medium ≥ 10mm/h, high ≥ 20mm/h

Score: LOW (< 1.5), MEDIUM (1.5-3.5), HIGH (≥ 3.5)
```

#### `GET /api/noaa/hazard-layer`

Return hazard GeoJSON saja (tanpa summary/markers).

#### `GET /api/noaa/soil-moisture`

| Item | Detail |
|------|--------|
| Parameter | `latitude`, `longitude`, `forecast_days` (1-16), `threshold` (optional) |
| Response | Hourly soil moisture time series + early warning flag |

#### `GET /api/noaa/rainfall`

| Item | Detail |
|------|--------|
| Parameter | `latitude`, `longitude`, `forecast_days` (1-16) |
| Response | Hourly rainfall time series + 3-day accumulation + max intensity |

#### `GET /api/noaa/health`

| Item | Detail |
|------|--------|
| Response | Status Open-Meteo reachability + jumlah admin boundaries loaded |

---

## 5. Data Sources

| Sumber | Data | Format | Akses | Update |
|--------|------|--------|-------|--------|
| BMKG | Gempa terkini | JSON | HTTP GET | Real-time (cache 1m) |
| BMKG | Peringatan cuaca nowcast | RSS + CAP XML | HTTP GET | Real-time (cache 15m) |
| Open-Meteo | Prakiraan cuaca, soil moisture, rainfall | JSON | HTTP GET | Cache 60m |
| WorldPop | Kepadatan penduduk 2026 (1km) | Cloud-Optimized GeoTIFF | Local file | Precomputed startup |
| GADM 3.6 | Batas administrasi Indonesia level 2 | GeoJSON | Local file | Static |
| Vs30 | Klasifikasi situs tanah (SNI 1726:2019) | COG TIF | Local file | Generated once |

---

## 6. GIS Data Handling

### 6.1 GeoJSON (GADM)

- File: `gadm36_IDN_2.json` — 514 kabupaten/kota Indonesia
- Validasi geometry dengan `shapely.is_valid` sebelum diproses
- Proyeksi UTM untuk kalkulasi luas area (centroid-based zone detection)
- Digunakan di 3 router: populasi (choropleth), vs30 (boundary overlay), noaa (hazard aggregation)

### 6.2 Raster (WorldPop & Vs30)

**WorldPop:**
- Akses baca: `rasterio.Window(col, row, 1, 1)` — hanya baca 1 pixel per query
- Prekomputasi: sampling 10×10 grid (100 point per kabupaten)
- Path di backend: `assets/json/idn_pop_2026_CN_1km_R2025A_UA_v1.tif`

**Vs30:**
- Akses baca: `rasterio.Window` single-pixel read
- Image generation: downsample ke max 2000px, encode sebagai PNG RGBA
- Path di backend: `assets/json/vs30_wgs84.tif`

### 6.3 Layer di Frontend (MapLibre GL)

| Layer | Type | Source | Visual |
|-------|------|--------|--------|
| Population | `fill` (choropleth) | GeoJSON `/api/populasi/geojson` | 9-class sequential color (white→dark red) |
| Vs30 | `raster` (image) | PNG `/api/vs30/raster` + bounding box | 5-class site classification |
| Weather Alerts | `fill` (polygon) | CAP XML polygon → GeoJSON | Red transparent fill |
| Early Warning | `fill` (choropleth) | GeoJSON `/api/early-warning/indonesia` | Color by risk_level property |

---

## 7. Deployment

### 7.1 Backend — Hugging Face Spaces (Docker)

- **Platform:** Hugging Face Spaces, Docker runtime
- **Port:** 7860
- **Base image:** `python:3.11-slim` + `gdal-bin` + `libgdal-dev`
- **Startup:** `uvicorn main:app --host 0.0.0.0 --port 7860`
- **Cache:** In-memory (RAM Space)
- **Cold start:** Space bisa sleep setelah idle

### 7.2 Frontend — Vercel

- **Static deployment:** Semua file HTML/JS/CSS di-root
- **Serverless functions:** Folder `/api/*` → Vercel serverless functions
- **Build step:** Tidak ada (vanilla HTML/JS)
- **Environment variables:** MAP_SERVICE_KEY via Vercel dashboard
- **Ignored files:** `.vercelignore` (backend/, assets/json/*.tif, dll)

---

## 8. Reference to System Design

> Untuk arsitektur sistem, diagram alur data, dan penjelasan desain secara keseluruhan, lihat:
> **system-design.md**