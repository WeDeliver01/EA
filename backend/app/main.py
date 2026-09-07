"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.errors import install_error_handlers
from app.api.v1.system import router as system_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import make_redis_client

logger = get_logger(service="api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        echo=settings.database_echo,
    )
    app.state.db_engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.redis = make_redis_client(settings.redis_url)

    logger.info("api_startup", git_sha=settings.git_sha, environment=settings.environment)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        logger.info("api_shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        log_level=settings.log_level, log_format=settings.log_format, git_sha=settings.git_sha
    )

    app = FastAPI(title="DelicateTrader API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.correlation_id = request.headers.get("X-Correlation-Id", str(uuid.uuid4()))
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = request.state.correlation_id
        return response

    install_error_handlers(app)
    app.include_router(system_router, prefix="/api/v1")

    return app


app = create_app()
