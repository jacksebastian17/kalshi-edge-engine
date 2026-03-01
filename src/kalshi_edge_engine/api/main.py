"""FastAPI application for Kalshi Edge Engine."""

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(
    title="Kalshi Edge Engine",
    description="Real-time signal generation and dashboard backend",
    version="0.1.0",
)


class HealthResponse(BaseModel):
    """Health check response."""

    status: str


class VersionResponse(BaseModel):
    """Version information response."""

    name: str
    version: str


@app.get("/health", response_model=HealthResponse)
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/version", response_model=VersionResponse)
async def version() -> dict[str, Any]:
    """Version information endpoint."""
    return {
        "name": "kalshi-edge-engine",
        "version": "0.1.0",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
