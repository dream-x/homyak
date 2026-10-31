"""homyak-radar — GitHub-радар руками.

  homyak-radar snapshot        снять stars/forks по репозиториям из ленты и ⭐ (как sweeper ночью)
  homyak-radar show [дней]     топ по звёздам/день за окно
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console

from homyak.core.radar import radar, snapshot

console = Console()


async def main_async(argv: list[str]) -> None:
    cmd = argv[0] if argv else "show"
    if cmd == "snapshot":
        res = await snapshot()
        console.print(
            f"репозиториев: {res['repos']} · снято: {res['saved']} · нет: {res['missing']}"
            + (f" · [red]остановлено: {res['stopped']}[/red]" if res["stopped"] else "")
        )
        return
    days = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 7
    rows = await radar(days, limit=25)
    if not rows:
        console.print("[dim]скорости пока нет: нужны два снимка в окне (homyak-radar snapshot)[/dim]")
        return
    for r in rows:
        star = "⭐ " if r["starred"] else "   "
        title = (r["item"] or {}).get("title") or ""
        console.print(
            f"{star}[bold]{r['repo']}[/bold]  +{r['gained']} за {r['span_days']}д "
            f"([green]{r['per_day']}/день[/green]) · {r['stars']}★  [dim]{title[:60]}[/dim]"
        )


def main() -> None:
    asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    main()
