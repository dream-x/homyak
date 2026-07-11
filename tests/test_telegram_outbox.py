import asyncio
import json

from homyak.adapters.sources.telegram_relay import TelegramRelaySource


class FakeRepo:
    """Мини-repo только с курсором (byte offset), без БД."""

    def __init__(self):
        self.cursors: dict[str, str | None] = {}

    async def get_cursor(self, name):
        return self.cursors.get(name)

    async def save_cursor(self, name, cursor, error=None):
        self.cursors[name] = cursor


async def _run_briefly(src, sink, seconds=0.15):
    task = asyncio.create_task(src.subscribe(sink))
    await asyncio.sleep(seconds)
    src.stop()
    await task


async def test_relay_tails_parses_and_advances_offset(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text(
        json.dumps({"source_id": "1:1", "text": "hello"}) + "\n"
        + json.dumps({"source_id": "1:2", "text": "world", "url": "https://t.me/x/2"}) + "\n",
        encoding="utf-8",
    )
    repo = FakeRepo()
    src = TelegramRelaySource(str(outbox), repo, poll_interval=0.01)
    got = []

    async def sink(dto):
        got.append(dto)

    await _run_briefly(src, sink)

    assert [d.source_id for d in got] == ["1:1", "1:2"]
    assert all(d.source_type == "telegram" for d in got)
    assert int(repo.cursors["telegram-relay"]) == outbox.stat().st_size


async def test_relay_ignores_incomplete_trailing_line(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    # вторая строка без \n — неполная, не должна обрабатываться
    with outbox.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"source_id": "1:1", "text": "done"}) + "\n")
        f.write(json.dumps({"source_id": "1:2", "text": "partial"}))
    repo = FakeRepo()
    src = TelegramRelaySource(str(outbox), repo, poll_interval=0.01)
    got = []

    async def sink(dto):
        got.append(dto)

    await _run_briefly(src, sink)

    assert [d.source_id for d in got] == ["1:1"]  # неполная строка отложена
