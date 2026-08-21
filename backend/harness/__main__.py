"""Local development entry point."""

from __future__ import annotations

from pathlib import Path

import uvicorn

from .api.app import create_app
from .core.config import load_settings
from .runtime import validate_runtime_platform


def main() -> None:
    validate_runtime_platform()
    project_root = Path(__file__).resolve().parents[2]
    settings = load_settings(project_root)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
