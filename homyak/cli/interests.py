"""homyak-interests: показать / сравнить / применить декларацию интересов.

    show      три слоя разом: что объявил · что дообучилось · какие веса
    diff      чем config/interests.yaml отличается от применённого профиля в БД
    apply     применить (новая версия профиля в БД; вертикали без изменений не трогаем)
    unmute    снять мьют кнопки 🔇: `unmute <вертикаль> <тег>` | `unmute all` | без аргументов — список
    backfill  пересчитать personal_score у айтемов, убитых снятым мьютом

Зачем diff отдельной командой: раньше декларация лежала в config/profiles/*.yaml, который читал
только CLI, а живой профиль — в БД, и его переписывала кнопка 🔇. Расхождение между ними никак не
показывалось: `medical: mute` прожил в базе трое суток и выкосил 21% вертикали. Теперь дрейф виден
одной командой.
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.table import Table
from sqlalchemy import or_, select

from homyak.adapters.analyzers.personalizer import PersonalizerAnalyzer
from homyak.core.config import settings
from homyak.core.interests import (
    MUTE_SKIP_PREFIX,
    VerticalInterest,
    diff_declaration,
    load_interests,
)
from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import NewsItem
from homyak.core.verticals import VERTICALS
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo
from homyak.storage.qdrant import QdrantStore

console = Console()

_POL_LABEL = {
    "love": "❤️  обожаю",
    "like": "👍 нравится",
    "meh": "😐 нейтрально",
    "dislike": "👎 не люблю",
    "mute": "🔇 мьют",
}


def _norm(s: str | None) -> str:
    return " ".join((s or "").split())


def _check_verticals(inter) -> None:
    """Опечатка в ключе вертикали иначе прошла бы молча.

    `buisness:` создал бы активный профиль для вертикали, которой не бывает, а настоящая
    тихо осталась бы на прежнем профиле. Пропавший ключ не менее коварен: судья продолжит
    читать старый профиль из БД, а в файле его уже нет — тот самый невидимый дрейф.
    """
    keys = set(inter.verticals)
    known = set(VERTICALS)
    for unknown in sorted(keys - known):
        console.print(f"[red]✗ '{unknown}' — не вертикаль[/red] (есть: {', '.join(sorted(known))})")
    for missing in sorted(known - keys):
        console.print(f"[yellow]⚠ {missing} нет в файле[/yellow] — в БД останется старый профиль")


async def _diff_one(repo: NewsRepo, vertical: str, decl: VerticalInterest) -> list[str]:
    """Строки расхождений файла с применённым профилем. Пусто = синхронизировано."""
    prof = await repo.get_active_profile(vertical)
    if prof is None:
        return ["профиля в БД нет — apply создаст первую версию"]
    _ver, desc, topics = prof
    return diff_declaration(decl, desc, topics)


async def cmd_diff(repo: NewsRepo) -> bool:
    inter = load_interests()
    _check_verticals(inter)
    dirty = False
    for vertical, decl in inter.verticals.items():
        lines = await _diff_one(repo, vertical, decl)
        if not lines:
            console.print(f"[green]✓ {vertical}[/green] — совпадает с БД")
            continue
        dirty = True
        console.print(f"[yellow]≠ {vertical}[/yellow]")
        for line in lines:
            console.print(f"    {line}")
    if not dirty:
        console.print("\n[dim]Декларация и БД синхронизированы.[/dim]")
    else:
        console.print("\n[dim]Применить: uv run homyak-interests apply[/dim]")
    return dirty


async def cmd_apply(repo: NewsRepo) -> None:
    inter = load_interests()
    _check_verticals(inter)
    for vertical, decl in inter.verticals.items():
        if vertical not in VERTICALS:
            console.print(f"[red]{vertical}: не вертикаль — не применяю[/red]")
            continue
        if not decl.description.strip():
            console.print(f"[yellow]{vertical}: пустое description — пропускаю[/yellow]")
            continue
        lines = await _diff_one(repo, vertical, decl)
        if not lines:
            console.print(f"[dim]{vertical}: без изменений[/dim]")
            continue  # версии профиля — история решений, не спамим её пустыми применениями
        version = await repo.set_profile(
            vertical, _norm(decl.description), [t.model_dump() for t in decl.topics]
        )
        console.print(f"[green]{vertical}[/green]: применено → v{version}, тем {len(decl.topics)}")
        for line in lines:
            console.print(f"    {line}")
    console.print(
        "\n[dim]watch и weights подхватываются на лету (кэш по mtime) — рестарт не нужен.[/dim]"
    )


async def cmd_show(repo: NewsRepo) -> None:
    inter = load_interests()

    console.print("\n[bold]1. ДЕКЛАРАЦИЯ[/bold] [dim]— твои слова, config/interests.yaml[/dim]")
    for vertical, decl in inter.verticals.items():
        prof = await repo.get_active_profile(vertical)
        ver = f"v{prof[0]}" if prof else "нет в БД"
        sync = "" if not await _diff_one(repo, vertical, decl) else "  [yellow]≠ БД[/yellow]"
        console.print(f"\n  [bold cyan]{vertical}[/bold cyan] [dim]({ver})[/dim]{sync}")
        console.print(f"    [dim]{_norm(decl.description)[:150]}…[/dim]")
        by_pol: dict[str, list[str]] = {}
        for t in decl.topics:
            by_pol.setdefault(t.polarity, []).append(t.name)
        for pol, label in _POL_LABEL.items():
            if by_pol.get(pol):
                console.print(f"    {label}: {', '.join(by_pol[pol])}")

    console.print("\n[bold]2. ВЫУЧЕННОЕ[/bold] [dim]— система сама, из 👍/👎. В файл не пишет[/dim]")
    muted = await repo.list_muted_tags()
    if muted:
        for v, tag, at in muted:
            console.print(f"  🔇 {v}/{tag} [dim]({at:%Y-%m-%d})[/dim]")
    else:
        console.print("  [dim]мьютов от кнопки 🔇 нет[/dim]")

    console.print("\n[bold]3. ВЕСА[/bold] [dim]— сколько что значит[/dim]")
    t = Table(show_header=False, box=None, padding=(0, 2))
    for k, v in inter.weights.model_dump().items():
        t.add_row(f"  {k}", str(v))
    console.print(t)

    console.print(f"\n[dim]Под наблюдением (watch): {len(inter.watch)} тем[/dim]")


async def cmd_backfill(repo: NewsRepo) -> None:
    """Оживить айтемы, у которых personal_score=NULL из-за мьюта, которого больше нет.

    Переопубликовать их в NATS нельзя: processor на processed_at != NULL делает идемпотентный
    ack и молча выходит (processor.py:32). Поэтому зовём тот же PersonalizerAnalyzer напрямую.

    Фильтры выстраданы на живых данных:

    * skip_reason: NULL (жертвы мьюта до того, как мы стали писать причину) ИЛИ наш собственный
      префикс. Отсеянных гейтом («низкая близость…») брать нельзя — у них тоже personal_score
      NULL, и бэкфилл воскресил бы отсеянный мусор. А вот СВОИХ брать обязательно: без этого
      команда не видела бы ровно тех, ради кого написана, и каждая новая жертва мьюта оставалась
      бы мёртвой навсегда (переобработки нет — processor.py:32 на processed_at != NULL молча
      ack'ает).
    * llm_relevance IS NOT NULL — жертва мьюта ВСЕГДА прошла судью (он stage 7, мьют stage 8).
      Без этого условия первый прогон поднял 1198 айтемов от 10.07 — эпоха до LLM-стадий,
      ни тегов, ни саммари: они лежали без score совсем по другой причине.
    """
    pers = PersonalizerAnalyzer(repo, QdrantStore(settings.qdrant_url))
    revived = still_muted = 0
    async with SessionFactory() as s:
        rows = (
            await s.execute(
                select(NewsItem).where(
                    NewsItem.processed_at.isnot(None),
                    NewsItem.personal_score.is_(None),
                    NewsItem.vertical.isnot(None),
                    NewsItem.llm_relevance.isnot(None),
                    or_(
                        NewsItem.skip_reason.is_(None),
                        NewsItem.skip_reason.startswith(MUTE_SKIP_PREFIX),
                    ),
                )
            )
        ).scalars()
        for item in rows:
            ctx = AnalyzerContext(item_id=item.id, item=item, session=s)
            await pers.analyze(ctx)
            if item.personal_score is None:
                still_muted += 1
            else:
                revived += 1
        await s.commit()
    console.print(f"[green]оживлено: {revived}[/green], осталось замьючено: {still_muted}")


async def cmd_unmute(repo: NewsRepo) -> None:
    """Снять мьют кнопки: `homyak-interests unmute <вертикаль> <тег>` (или `all`).

    Существует потому, что снять мьют было больше нечем: кнопка 🔇 после нажатия схлопывается,
    а замьюченный тег в пуш уже не прилетит — значит и второй раз её не нажать. Оставался
    только DELETE руками по таблице.
    """
    args = sys.argv[2:]
    muted = await repo.list_muted_tags()
    if not args:
        if not muted:
            console.print("[dim]мьютов от кнопки 🔇 нет[/dim]")
            return
        for v, tag, at in muted:
            console.print(f"  🔇 {v}/{tag} [dim]({at:%Y-%m-%d %H:%M})[/dim]")
        console.print("\n[dim]снять: homyak-interests unmute <вертикаль> <тег> | unmute all[/dim]")
        return

    if args[0] == "all":
        for v, tag, _at in muted:
            await repo.unmute_topic(v, tag)
        console.print(f"[green]снято мьютов: {len(muted)}[/green]")
    elif len(args) >= 2:
        vertical, tag = args[0], args[1]
        await repo.unmute_topic(vertical, tag)
        console.print(f"[green]🔇 снят: {vertical}/{tag}[/green]")
    else:
        console.print("[red]нужно: unmute <вертикаль> <тег> | unmute all[/red]")
        raise SystemExit(2)
    console.print("[dim]оживить айтемы: homyak-interests backfill[/dim]")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    repo = NewsRepo(SessionFactory)
    table = {
        "show": cmd_show,
        "diff": cmd_diff,
        "apply": cmd_apply,
        "backfill": cmd_backfill,
        "unmute": cmd_unmute,
    }
    fn = table.get(cmd)
    if fn is None:
        console.print(f"[red]неизвестная команда: {cmd}[/red]")
        console.print(f"доступно: {', '.join(table)}")
        raise SystemExit(2)
    asyncio.run(fn(repo))


if __name__ == "__main__":
    main()
