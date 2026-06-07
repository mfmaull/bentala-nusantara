# AGENTS.md — Bentala Nusantara WebGIS

> AI agent configuration for Cline + DeepSeek-V4 (deepseek-v4-flash and deepseek-v4-pro model).
> Read this file FIRST before any task. Keep agent context minimal — load only what the task needs.

---

## Project Snapshot

**Mission:** Transform complex disaster data (earthquake, weather, seismic) into accessible public WebGIS.

**Stack:**
- Frontend: Vanilla HTML/JS, Tailwind CSS, MapLibre GL, Turf.js — deployed on **Vercel**
- Backend: FastAPI (Python) + Rasterio/GDAL — deployed on **Hugging Face Spaces (Docker)**
- Storage/DB: Google Sheets + Apps Script (comments, uploads), Google Drive (PDFs)
- Key data: BMKG (earthquake + weather CAP/RSS), WorldPop TIF, GADM GeoJSON, Vs30 COG raster (SNI 1726:2019)

**Repo:** `mfmaull/bentala-nusantara` | Branch: `main`
**Backend live:** `https://mfmaull-bentala-nusantara-api.hf.space/api`

**Active files:**
```
index.html         — landing page
map.html           — main WebGIS app (1500+ lines, monolith)
upload.html        — file upload (PDF + Excel via Apps Script)
config.js          — API keys (gitignored in production)
main.py            — FastAPI entrypoint
routers/
  gempa.py         — BMKG earthquake, 1-min cache
  cuaca.py         — BMKG weather RSS+CAP, 15-min cache
  populasi.py      — WorldPop raster + GADM GeoJSON, precomputed on startup
  vs30.py          — Vs30 COG raster point query
  noaa.py          — Open-Meteo integration, landslide risk index
```

---

## Agent Roster & Routing

Select ONE primary agent per task. If a task touches multiple domains, prefer the agent whose domain owns the output file.

### 🗺️ @gis-data-specialist
**Trigger keywords:** GeoJSON, raster, TIF, COG, Rasterio, Vs30, WorldPop, GADM, CRS, spatial query, bounding box, choropleth, pixel, coordinate transform

**Owns:** `routers/populasi.py`, `routers/vs30.py`, raster asset files

**Hard rules:**
- Never modify SNI 1726:2019 Vs30 thresholds (SA/SB/SC/SD/SE values are fixed standard)
- Never alter BMKG request headers — causes 403
- Always validate geometry with `shapely.is_valid` before spatial ops
- Always use WGS84 (EPSG:4326) for storage; convert to UTM only for area/distance calculations
- Use `rasterio.Window` for single-pixel reads (never read full raster for a point query)

---

### ⚡ @api-backend-architect
**Trigger keywords:** FastAPI, router, endpoint, async, cache, CORS, HTTP, httpx, Pydantic, lifespan, uvicorn

**Owns:** `main.py`, `routers/gempa.py`, `routers/cuaca.py`, `routers/noaa.py`

**Hard rules:**
- Cache pattern is `asyncio.Lock()` + TTL dict — do NOT introduce new cache libraries
- TTLs are fixed: earthquake=1min, weather=15min, raster data=permanent (precomputed once)
- Always include BMKG User-Agent and Referer headers (do NOT alter them)
- All routers use prefix `/api` — maintain this convention
- Return `JSONResponse` with explicit `content=` — do not rely on FastAPI auto-serialization for large payloads

---

### 🎨 @frontend-ux-engineer
**Trigger keywords:** HTML, CSS, Tailwind, JavaScript, UI, UX, MapLibre, map layer, sidebar, modal, responsive, dark mode, animation

**Owns:** `index.html`, `map.html`, `upload.html`

**Hard rules:**
- `map.html` is a monolith by design — do NOT split into separate JS files unless explicitly instructed
- Maintain existing CSS variable system (`--bg`, `--fg`, `--krem`, `--card`, etc.) — do not introduce new tokens without reason
- Map library: **MapLibre GL** (not Leaflet, not Mapbox) — API calls must use MapLibre syntax
- `setMode()` is the master mode switcher — all layer visibility changes must go through it
- Do not add new `map.on('load', ...)` calls — use the existing single load handler
- config.js must remain the single source for API keys

---

### 🌍 @disaster-data-integrator
**Trigger keywords:** BMKG, RSS, CAP XML, peringatan, earthquake, cuaca, weather alert, data source, feed, real-time

**Owns:** Data flow logic inside `cuaca.py`, `gempa.py`

**Hard rules:**
- Never use mock/dummy hazard data — always reference live BMKG or Open-Meteo endpoints
- CAP XML parsing uses Python `xml.etree.ElementTree` — do not switch to lxml unless explicitly approved
- Polygon coordinates from CAP are `lat,lon` pairs — convert to GeoJSON `[lon, lat]` format
- BMKG RSS URL: `https://www.bmkg.go.id/alerts/nowcast/id/rss.xml` — do not change
- BMKG earthquake URL: `https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json` — do not change

---

### 🏗️ @system-architecture-lead
**Trigger keywords:** refactor, architecture, performance, deployment, security, scaling, CI/CD, restructure, migrate

**Owns:** Cross-file decisions, `AGENTS.md`, `.clinerules`, deployment configs

**Hard rules:**
- Free-tier constraint is non-negotiable: Hugging Face Spaces (backend) + Vercel (frontend)
- No credit-card-required platforms — Heroku/Railway/Render are ruled out
- Google Sheets + Apps Script is the zero-cost DB layer — do not propose paid DB unless user asks
- Backend and frontend are separate git repos for independent deployment

---

## Overlap Resolution

When a task touches multiple agents, apply this priority order:

```
1. Which file is being modified? → owner agent takes lead
2. Tie: backend change → @api-backend-architect leads
3. Tie: frontend change → @frontend-ux-engineer leads
4. Cross-cutting (e.g. new feature end-to-end) → @system-architecture-lead leads, delegates subtasks
```

---

## Code Conventions

### Python
- PEP 8, max line length 100
- Type hints on all function signatures
- `async def` for all I/O; `asyncio.Lock()` for shared state
- `logging` module only — never `print()`
- `httpx.AsyncClient` for outbound HTTP

### JavaScript
- ES6+: `const`/`let`, arrow functions, destructuring, template literals
- Vanilla JS only — no jQuery, no React
- `fetch()` for HTTP; async/await preferred
- Event delegation for dynamic DOM
- Comments in English for complex logic

### Naming
- Python: `snake_case` functions/vars, `PascalCase` classes
- JS: `camelCase` functions/vars
- HTML IDs: `kebab-case`

---

## Hard Prohibitions (Never Do)

- Never modify Vs30 SNI 1726:2019 classification thresholds
- Never alter BMKG request headers
- Never add `map.on('load', ...)` as a second handler in `map.html`
- Never introduce paid/credit-card-required infrastructure
- Never split `map.html` into modules without explicit user approval
- Never use `print()` in Python backend code
- Never hardcode API keys — always use `config.js` or environment variables
- Never rewrite entire files for small changes

---

**Last updated:** 2026-06-07
**Model target:** DeepSeek-V4 (deepseek-v4-flash and deepseek-v4-pro) via Cline
