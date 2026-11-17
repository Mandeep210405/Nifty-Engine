"""FastAPI entry point.  Run:  python -m app.main"""
from __future__ import annotations

import logging
import mimetypes
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import FRONTEND_DIR, ConfigError, Settings, load_settings, setup_logging
from app.engine import MarketEngine
from app.process_lock import ProcessLock
from app.storage.repository import Repository

# Windows registries sometimes map .js to text/plain, which browsers refuse as module/script.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

log = logging.getLogger("nifty")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock = None
        if not settings.is_mock:
            lock = ProcessLock(settings.data_dir / ".live_session.lock")
            lock.acquire()           # one broker session per machine/data dir
        if settings.is_mock and settings.db_path.exists():
            settings.db_path.unlink()          # mock data is ephemeral
        repo = Repository(settings.db_path)
        engine = MarketEngine(settings, repo)
        app.state.engine, app.state.settings = engine, settings
        await engine.start()
        log.info("Started in %s mode (%s)", settings.mode.upper(), settings.instrument.name)
        try:
            yield
        finally:
            await engine.stop()
            if lock:
                lock.release()

    app = FastAPI(title="NIFTY Live Engine", lifespan=lifespan)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
    return app


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings)
    import uvicorn
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
