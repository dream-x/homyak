"""homyak-profile-set: применить профили вертикалей из config/profiles/{business,it,medical}.yaml."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml
from rich.console import Console

from homyak.core.verticals import VERTICALS
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

console = Console()


async def main_async(directory: str) -> None:
    repo = NewsRepo(SessionFactory)
    d = Path(directory)
    for vertical in VERTICALS:
        f = d / f"{vertical}.yaml"
        if not f.exists():
            console.print(f"[yellow]{f} нет — пропускаю[/yellow]")
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        description = (data.get("description") or "").strip()
        topics = data.get("topics") or []
        if not description:
            console.print(f"[yellow]{f} без description — пропускаю[/yellow]")
            continue
        version = await repo.set_profile(vertical, description, topics)
        console.print(f"[green]{vertical}[/green]: профиль v{version}, тем {len(topics)}")


def main() -> None:
    directory = sys.argv[1] if len(sys.argv) > 1 else "config/profiles"
    asyncio.run(main_async(directory))


if __name__ == "__main__":
    main()
