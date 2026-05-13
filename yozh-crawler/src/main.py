from __future__ import annotations

from .app import create_app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    from .settings import settings

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
