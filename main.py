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
    uvicorn.run(
        "app.server:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
        reload_dirs=["/app/app"],
        log_level="info",
    )
