"""Arranque del dashboard: ``python -m p2p_arb_bot.web``.

Lee host/puerto del entorno y levanta uvicorn sobre ``app:app``.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    uvicorn.run("p2p_arb_bot.web.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
