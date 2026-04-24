"""Entry point for the TradeAutonom GRVT trade execution service."""

import logging
import uvicorn
from dotenv import load_dotenv

from app.config import Settings

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    settings = Settings()
    # Auto-reload is enabled on V1 (shared-mount hot-reload from
    # /opt/tradeautonom-v3/app) but not on V2 Cloudflare Containers where
    # code is baked into the image. Set APP_RELOAD=0 in the container env
    # to disable. Default True preserves V1 behaviour.
    run_kwargs: dict = {
        "host": settings.app_host,
        "port": settings.app_port,
        "log_level": "info",
    }
    if settings.app_reload:
        run_kwargs["reload"] = True
        run_kwargs["reload_dirs"] = ["/app/app"]
    uvicorn.run("app.server:app", **run_kwargs)
