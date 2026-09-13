from __future__ import annotations

import os

import uvicorn

from app.config import Settings
from app.logging_config import configure_logging


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    uvicorn.run(
        "app.server:app",
        host=settings.mcp_bind_host,
        port=settings.mcp_port,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        server_header=False,
    )


if __name__ == "__main__":
    main()
