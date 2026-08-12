"""Прогон промпта ⭐-карточки на настоящих звёздах + аудит выдумок.

Промпт нельзя «проверить» юнит-тестом: тесты держат разбор и заземление, а качество выжимки
видно только на живых статьях. Скрипт берёт реально отмеченное ⭐, строит карточки и показывает
их рядом с исходником, а вторая (независимая) LLM-проверка ищет утверждения, которых в тексте
нет — то, что детерминированный `ungrounded` поймать не может (перевранная причинность,
приписанный автору вывод).

Судья живёт только здесь: в проде за каждую карточку платить вторым вызовом незачем — там
работает дешёвая детерминированная проверка.

    homyak-starcard-eval [N] [--judge] [--full]
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from sqlalchemy import text

from homyak.core.config import settings
from homyak.core.llm import OllamaLLM
from homyak.core.starcard import choose_mode, make_card
from homyak.storage.db import SessionFactory

console = Console()

_PICK = text(
    "select n.id, n.title, n.url, n.text, n.feed_name, length(coalesce(n.text,'')) len"
    " from feedback f join news_items n on n.id = f.news_item_id"
    " where f.signal = 'save' and n.vertical = 'it'"
    " order by f.created_at desc limit :n"
)

_JUDGE_SYSTEM = (
    "Ты придирчивый факт-чекер. На входе ИСХОДНЫЙ ТЕКСТ статьи и ПЕРЕСКАЗ на русском. "
    "Найди в пересказе утверждения, которых в исходном тексте НЕТ или которые ему противоречат "
    "(включая приписанные автору выводы и подменённые причины). "
    'Верни СТРОГО JSON: {"ok": true|false, "issues": ["...", "..."]}. '
    "issues — короткие формулировки проблем по-русски, пусто если всё подтверждается. "
    "Переформулировка своими словами и перевод — НЕ проблема. Сокращение — НЕ проблема."
)


async def _judge(llm: OllamaLLM, source: str, card_text: str) -> dict:
    try:
        return await llm.chat_json(
            _JUDGE_SYSTEM, f"ИСХОДНЫЙ ТЕКСТ:\n{source[:6000]}\n\nПЕРЕСКАЗ:\n{card_text}"
        )
    except Exception as e:
        return {"ok": None, "issues": [f"судья недоступен: {str(e)[:80]}"]}


async def main_async() -> None:
    args = sys.argv[1:]
    n = next((int(a) for a in args if a.isdigit()), 12)
    use_judge = "--judge" in args
    show_full = "--full" in args

    llm = OllamaLLM(model=settings.summary_model, fallback=settings.summary_fallback_model)
    async with SessionFactory() as s:
        rows = (await s.execute(_PICK, {"n": n})).all()

    console.print(f"[bold]⭐-карточки: {len(rows)} записей, модель {settings.summary_model}[/bold]\n")
    stats = {"full": 0, "brief": 0, "bare": 0}
    dropped_total = 0
    judged_bad = 0

    for r in rows:
        card = await make_card(r.title, r.text, llm)
        stats[card.mode] = stats.get(card.mode, 0) + 1
        dropped_total += len(card.dropped)

        console.print(f"[bold cyan]#{r.id}[/bold cyan] {r.title or '—'}")
        console.print(f"  [dim]{r.feed_name or '?'} · {r.len} симв. · режим {card.mode}"
                      f" (порог {choose_mode(r.text)})[/dim]")
        if card.line:
            console.print(f"  [green]▸[/green] {card.line}")
        for p in card.points:
            console.print(f"    • {p}")
        if not card.has_text:
            console.print("  [yellow]без пересказа — текста не хватило[/yellow]")
        if card.dropped:
            console.print(f"  [red]выброшено:[/red] {'; '.join(card.dropped)}")
        if show_full and r.text:
            console.print(f"  [dim]исходник: {r.text[:400].replace(chr(10), ' ')}…[/dim]")
        if use_judge and card.has_text:
            body = "\n".join(filter(None, [card.line, *card.points]))
            verdict = await _judge(llm, f"{r.title or ''}\n{r.text or ''}", body)
            if verdict.get("ok") is False:
                judged_bad += 1
                for issue in verdict.get("issues", [])[:4]:
                    console.print(f"  [red]судья:[/red] {issue}")
            elif verdict.get("ok") is True:
                console.print("  [dim]судья: подтверждено[/dim]")
        console.print()

    console.print(
        f"[bold]Итог:[/bold] full={stats['full']} brief={stats['brief']} bare={stats['bare']}"
        f" · выброшено фраз: {dropped_total}"
        + (f" · судья забраковал: {judged_bad}/{len(rows)}" if use_judge else "")
    )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
