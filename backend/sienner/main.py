from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sienner import __version__
from sienner.api.routes import router
from sienner.config import get_settings
from sienner.deps import init_storage


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_storage()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Sienner",
        version=__version__,
        description="Portfolio evaluation tool - Stage 1a walking skeleton",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
