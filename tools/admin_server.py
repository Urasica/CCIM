"""Local admin UI for CCIM v2.

Run from the v2 directory:
    uv run python tools/admin_server.py
"""

from __future__ import annotations

from admin_ui.app import app, main

__all__ = ["app", "main"]


if __name__ == "__main__":
    main()
