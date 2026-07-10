"""uvicorn entrypoint для FastAPI-приложения."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "homyak.adapters.outputs.api:app",
        host=os.getenv("HOMYAK_HOST", "0.0.0.0"),
        port=int(os.getenv("HOMYAK_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
