from __future__ import annotations

from gpucall.app import create_app

app = create_app()

__all__ = ["app", "create_app"]
