"""Entry point for the TradeAutonom Account Dashboard service."""

import logging
import os
import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", "8003"))
    uvicorn.run(
        "dashboard.server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
