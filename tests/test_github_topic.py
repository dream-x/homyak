"""Парсер страницы темы GitHub. Разметку они меняют — тест фиксирует, за что держимся."""

from homyak.adapters.sources.github_topic import GithubTopicSource, parse_topic_page

# Фрагмент реальной страницы github.com/topics/ai-agents (август 2026), ужатый до сути:
# ссылка на репозиторий с data-view-component, картинка-превью на тот же репозиторий,
# служебные ссылки на темы и спонсоров.
PAGE = """
<article class="border rounded color-shadow-small">
  <a href="/NousResearch/hermes-agent" data-view-component="true" class="Link">hermes-agent</a>
  <a href="/NousResearch/hermes-agent" data-view-component="true" class="image">
    <img src="https://repository-images.githubusercontent.com/1.png" alt="hermes">
  </a>
  <p class="f5 color-fg-muted">An agent harness for local models</p>
  <a href="/topics/ai-agents" data-view-component="true">ai-agents</a>
  <a href="/sponsors/NousResearch" data-view-component="true">Sponsor</a>
</article>
<article>
  <a href="/deepseek-ai/deepseek-harness" data-view-component="true">deepseek-harness</a>
  <a href="/orgs/deepseek-ai/people" data-view-component="true">people</a>
</article>
"""


def test_parses_repos_and_drops_service_links():
    assert parse_topic_page(PAGE) == ["NousResearch/hermes-agent", "deepseek-ai/deepseek-harness"]


def test_repo_is_not_duplicated_by_its_preview_image_link():
    """Карточка ссылается на репозиторий дважды — заголовком и картинкой."""
    assert parse_topic_page(PAGE).count("NousResearch/hermes-agent") == 1


def test_empty_or_broken_markup_yields_nothing():
    assert parse_topic_page("") == []
    assert parse_topic_page("<html><body>ничего похожего</body></html>") == []


def test_feed_name_is_stable_and_sql_safe():
    """feed_name уезжает в БД и в фильтры лент — дефис в теме превращаем в подчёркивание."""
    assert GithubTopicSource("ai-agents").name == "gh_topic_ai_agents"
    assert GithubTopicSource("llm").name == "gh_topic_llm"
