"""Источник «репозитории по теме GitHub» (PollSource): читает github.com/topics/<тема>.

Зачем свой парсер, а не RSSHub. 14.08 GitHub закрыл search API для анонимных запросов, и
фиды `topic:ai-agents` / `topic:llm` умерли разом — RSSHub падает на `undefined.map`, получив
ошибку вместо результатов. Заменой стало зеркало трендов, но оно ищет ПО ЯЗЫКАМ: нишевый
проект на 200 звёзд в общеязыковой тренд не попадает никогда, и тема просела втрое
(73 записи за неделю против 28). Страница темы при этом отдаётся всем и без авторизации.

Берём ДВА списка на тему:
  • по умолчанию — заметные проекты; отдаётся один раз и дальше меняется медленно;
  • ?s=updated — недавно активные, это и есть поток новых имён.
Наборы почти не пересекаются (проверено), а дубли всё равно снимет upsert по URL.

Описание со страницы не тянем сознательно: у карточек оно лежит в глубоко вложенной разметке,
которая переживает рестайлинг хуже всего, а текст репозитория всё равно приедет из README —
`article_fetch` запрашивает его напрямую через API. Парсер держится за одну пару, которая
рестайлинги пережила: ссылку `/owner/repo` рядом с data-view-component.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import structlog

from homyak.core.interfaces import NewsItemDTO

log = structlog.get_logger(__name__)

_URL = "https://github.com/topics/{topic}"
_UPDATED = "https://github.com/topics/{topic}?o=desc&s=updated"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_REPO = re.compile(r'href="/([\w.-]+/[\w.-]+)"[^>]*data-view-component')

# Служебные пути, тоже подходящие под шаблон /a/b, но репозиториями не являющиеся.
_NOT_REPO = ("topics/", "collections/", "sponsors/", "orgs/", "users/", "features/", "apps/")


def parse_topic_page(html: str) -> list[str]:
    """HTML страницы темы → ['owner/repo', ...] без повторов, в порядке страницы."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _REPO.finditer(html or ""):
        slug = m.group(1)
        if slug in seen or slug.lower().startswith(_NOT_REPO):
            continue
        seen.add(slug)
        out.append(slug)
    return out


class GithubTopicSource:
    """Один инстанс на тему. Имя попадает в feed_name как gh_topic_<тема>."""

    def __init__(self, topic: str, interval_seconds: int = 21600) -> None:
        self._topic = topic
        self.name = f"gh_topic_{topic.replace('-', '_')}"
        self.interval_seconds = interval_seconds

    async def _page(self, client: httpx.AsyncClient, url: str) -> list[str]:
        resp = await client.get(url.format(topic=self._topic))
        resp.raise_for_status()
        return parse_topic_page(resp.text)

    async def poll(self, cursor: str | None) -> AsyncIterator[tuple[NewsItemDTO, str]]:
        async with httpx.AsyncClient(
            timeout=40, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            slugs = await self._page(client, _URL)
            for slug in await self._page(client, _UPDATED):
                if slug not in slugs:
                    slugs.append(slug)

        if not slugs:
            # Разметка уехала — молчать нельзя: иначе источник годами отдаёт ноль и выглядит
            # как «на GitHub по теме ничего нет». Ошибка осядет в ingest_state.last_error.
            raise RuntimeError(f"github/topics/{self._topic}: не разобрал ни одного репозитория")

        log.info("github_topic_parsed", topic=self._topic, repos=len(slugs))
        now = datetime.now(timezone.utc)
        for slug in slugs:
            owner, name = slug.split("/", 1)
            url = f"https://github.com/{slug}"
            yield (
                NewsItemDTO(
                    source_type="rss",  # тот же тракт, что у прочих фидов
                    source_id=url,
                    url=url,
                    title=name,
                    author=owner,
                    feed_name=self.name,
                    published_at=now,
                    category="tech",
                ),
                now.isoformat(),
            )
