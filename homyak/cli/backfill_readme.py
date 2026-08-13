"""Backfill: дотянуть README у GitHub-записей, где в базе осел один блёрб.

До появления прямого запроса к API текст репозитория извлекался из HTML-страницы общим
экстрактором, и у 30-45% записей оседало меньше 400 символов — «описанием» служила одна
английская строка из RSSHub. Прогон переписывает text; `search_tsv` пересчитается сам.

    homyak-backfill-readme [--limit N] [--days N] [--summarize] [--dry]

`--summarize` заодно перегенерирует саммари «по-нашему» (дорого: вызов LLM на запись,
на загруженном боксе — минуты). Без него правится только текст, а саммари останутся
прежними до следующего прогона `homyak-resummarize`.
Эмбеддинги под новый текст — отдельно, `homyak-reembed`.
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from sqlalchemy import text as sql
from sqlalchemy import update

from homyak.adapters.analyzers.llm_summarizer import LlmSummarizerAnalyzer
from homyak.core.github import fetch_readme
from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import NewsItem
from homyak.storage.db import SessionFactory

console = Console()

_CANDIDATES = sql(
    "select id, url, length(coalesce(text,'')) len from news_items"
    " where feed_name like 'gh_%' and url like '%github.com/%'"
    "   and length(coalesce(text,'')) < 400"
    "   and fetched_at > now() - make_interval(days => :days)"
    " order by fetched_at desc limit :lim"
)


def _arg(name: str, default: int) -> int:
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return int(a.split("=", 1)[1])
    return default


async def main_async() -> None:
    limit = _arg("limit", 500)
    days = _arg("days", 60)
    do_summary = "--summarize" in sys.argv
    dry = "--dry" in sys.argv

    async with SessionFactory() as s:
        rows = (await s.execute(_CANDIDATES, {"days": days, "lim": limit})).all()
    console.print(
        f"[bold]Коротких GitHub-записей за {days} дн.: {len(rows)}[/bold]"
        + (" · саммари перегенерируем" if do_summary else "")
        + (" · сухой прогон" if dry else "")
    )

    summarizer = LlmSummarizerAnalyzer() if do_summary else None
    fixed = missed = 0
    for r in rows:
        readme = await fetch_readme(r.url)
        if not readme or len(readme) <= r.len:
            missed += 1
            continue
        fixed += 1
        console.print(f"  {r.url.split('github.com/')[-1][:44]:46} {r.len:>5} → {len(readme):>6}")
        if dry:
            continue
        async with SessionFactory() as s:
            await s.execute(update(NewsItem).where(NewsItem.id == r.id).values(text=readme))
            await s.commit()
            if summarizer is not None:
                item = await s.get(NewsItem, r.id)
                ctx = AnalyzerContext(item_id=r.id, item=item, session=s)
                await summarizer.analyze(ctx)
                if ctx.summary:
                    await s.execute(
                        update(NewsItem).where(NewsItem.id == r.id).values(summary=ctx.summary)
                    )
                    await s.commit()

    console.print(f"[bold]Готово:[/bold] дотянуто {fixed}, без README {missed}")
    if fixed and not do_summary and not dry:
        console.print("[dim]Саммари остались прежними — перегенерировать: --summarize[/dim]")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
