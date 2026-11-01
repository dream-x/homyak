"""Стадии 2 и 4 вместе: голая запись без вертикали получает метку, теггер ставит it — метка
ограниченной темы снимается, остальные остаются. Битый файл интересов виден CLI как ошибка."""

from types import SimpleNamespace

import pytest

from homyak.adapters.analyzers import watch_verticals as wv
from homyak.adapters.analyzers import watchlist_matcher as wm
from homyak.adapters.analyzers.watch_verticals import WatchVerticalsAnalyzer
from homyak.adapters.analyzers.watchlist_matcher import WatchlistAnalyzer
from homyak.core.interfaces import AnalyzerContext
from homyak.core.watchlist import compile_watchlist

WL = compile_watchlist(
    [
        {"name": "Природные явления", "aliases": ["storm", "ураган"], "verticals": ["business", "medical"]},
        {"name": "Claude", "aliases": ["claude"]},
    ]
)


def _ctx(title, text="", vertical=None):
    item = SimpleNamespace(title=title, text=text, vertical=vertical, watch_topics=[])
    return AnalyzerContext(item_id=1, item=item, session=None)


async def test_label_survives_stage_2_without_a_vertical_and_is_restricted_after_the_tagger(monkeypatch):
    monkeypatch.setattr(wm, "compiled_watchlist", lambda: WL)
    monkeypatch.setattr(wv, "compiled_watchlist", lambda: WL)
    ctx = _ctx("storm-framework", "Storm ORM, works with Claude Code")  # vertical=None на стадии 2
    await WatchlistAnalyzer().analyze(ctx)
    assert ctx.item.watch_topics == ["Природные явления", "Claude"]

    ctx.item.vertical = "it"  # llm_tagger
    await WatchVerticalsAnalyzer().analyze(ctx)
    assert ctx.item.watch_topics == ["Claude"]

    other = _ctx("Ураган обрушился на побережье")
    await WatchlistAnalyzer().analyze(other)
    await WatchVerticalsAnalyzer().analyze(other)  # теггер отнёс к other → vertical None
    assert other.item.watch_topics == ["Природные явления"]


def test_restrictor_is_registered_right_after_the_tagger():
    from homyak.core.registry import build_analyzers
    from homyak.storage.qdrant import QdrantStore

    names = [a.name for a in build_analyzers(QdrantStore.__new__(QdrantStore), repo=None, with_llm=True)]
    assert names.index("watch_verticals") == names.index("llm_tagger") + 1
    assert names.index("watch_verticals") < names.index("llm_summarizer")
    assert "watch_verticals" not in [a.name for a in build_analyzers(QdrantStore.__new__(QdrantStore), with_llm=False)]


def test_cli_reports_a_broken_interests_file_instead_of_missing_verticals(tmp_path, monkeypatch, capsys):
    from homyak.cli import interests as cli
    from homyak.core import interests

    bad = tmp_path / "interests.yaml"
    bad.write_text("verticals: {}\nwatch:\n  - {name: x, aliases: [x], verticals: [buisness]}\n", encoding="utf-8")
    monkeypatch.setattr(interests.settings, "interests_path", str(bad))
    inter = interests.load_interests()
    assert inter.watch == [] and interests.load_error() and "buisness" in interests.load_error()
    with pytest.raises(SystemExit) as e:
        cli._check_verticals(inter)
    assert e.value.code == 1 and "не разобран" in capsys.readouterr().out

    bad.unlink()  # файл пропал: ошибка — «файла нет», а не прошлая опечатка
    interests.load_interests()
    assert interests.load_error() == "файла нет"
