# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import gempa
from routers import cuaca
from routers import populasi
from routers import vs30

import logging
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Bentala Nusantara API",
    description="Backend API untuk platform sadar bencana",
    version="1.0"
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

@app.get("/")
def health():
    return {
        "status": "OK",
        "service": "Bentala Nusantara API",
        "modules": ["gempa", "cuaca", "populasi", "vs30"],
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)