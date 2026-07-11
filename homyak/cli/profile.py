"""homyak-profile-set: применить профиль интересов из YAML (config/profile.yaml по умолчанию)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml
from rich.console import Console

from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

console = Console()


async def main_async(path: str) -> None:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    description = (data.get("description") or "").strip()
    topics = data.get("topics") or []
    if not description:
        console.print("[red]profile.yaml без description[/red]")
        return
    repo = NewsRepo(SessionFactory)
    version = await repo.set_profile(description, topics)
    console.print(
        f"[green]Профиль сохранён[/green] (версия {version}), тем: {len(topics)}. "
        "Переоцени свежее: перезапусти processor или прогони reprocess."
    )


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "config/profile.yaml"
    asyncio.run(main_async(path))


if __name__ == "__main__":
    main()
