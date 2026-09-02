# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import logging

from routers import gempa, cuaca, populasi, vs30, noaa

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: jalankan prekomputasi populasi di background
    logger.info("Starting background precomputation for population density...")
    asyncio.create_task(populasi.compute_density_geojson_background())
    # Init terrain module (SRTM, BNPB, USGS) — blocking but fast (raster metadata only)
    logger.info("Initializing terrain module (SRTM, BNPB, USGS)...")
    try:
        noaa._init_terrain_module()
        logger.info("Terrain module initialized successfully")
    except Exception as exc:
        logger.warning("Terrain module init failed (graceful fallback active): %s", exc)
    yield
    # Shutdown: cleanup jika diperlukan
    logger.info("Shutting down...")

app = FastAPI(
    title="Bentala Nusantara API",
    description="Backend API untuk platform sadar bencana",
    version="1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ganti dengan domain Vercel setelah deploy
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(gempa.router, prefix="/api")
app.include_router(cuaca.router, prefix="/api")
app.include_router(populasi.router, prefix="/api")
app.include_router(vs30.router, prefix="/api")
app.include_router(noaa.router, prefix="/api")

@app.get("/")
def health():
    return {
        "status": "OK",
        "service": "Bentala Nusantara API",
        "modules": ["gempa", "cuaca", "populasi", "vs30", "noaa"],
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:main", host="127.0.0.1", port=8000, reload=True)