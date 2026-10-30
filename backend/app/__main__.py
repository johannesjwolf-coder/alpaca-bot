"""Start: ``uv run python -m app`` (im Ordner backend).

Startet genau einen Backend-Prozess, standardmässig nur auf 127.0.0.1.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .config import load_settings
from .instance_lock import InstanceLock, InstanceLockError
from .main import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if not settings.binds_loopback and not settings.allow_nonlocal_bind:
        print(
            f"Abbruch: APP_HOST={settings.host} ist keine localhost-Adresse. Die Anwendung hat keine "
            "Benutzeranmeldung und darf nicht öffentlich erreichbar sein. Nur für Container mit "
            "Port-Bindung an 127.0.0.1: APP_ALLOW_NONLOCAL_BIND=1 setzen.",
            file=sys.stderr,
        )
        sys.exit(2)
    lock = InstanceLock(settings.db_path.with_suffix(".lock"))
    try:
        lock.acquire()
    except InstanceLockError as exc:
        print(f"Abbruch: {exc}", file=sys.stderr)
        sys.exit(3)
    try:
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            workers=1,  # ausdrücklich ein Prozess (Bot-Instanz, Order-Sperren)
            proxy_headers=False,
            server_header=False,
            log_level="info",
        )
    finally:
        lock.release()


if __name__ == "__main__":
    main()
