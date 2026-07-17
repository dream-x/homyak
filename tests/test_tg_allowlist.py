"""Приватность бота: гейт пускает только разрешённых. Без него любой по @username читает
твои ленты, портит обучение и /start'ом угоняет пуши. Регресс — чтобы гейт не сняли молча."""

from types import SimpleNamespace

from homyak.adapters.outputs.tg_bot import AllowlistMiddleware, _parse_allowed


def test_parse_allowed_ids():
    assert _parse_allowed("37186533") == frozenset({37186533})
    assert _parse_allowed(" 1, 2 ,3 ") == frozenset({1, 2, 3})
    assert _parse_allowed("1,x,2") == frozenset({1, 2})  # мусор молча пропускаем
    assert _parse_allowed("") == frozenset()  # пусто = никого, а не «все»


async def _run(mw, data):
    calls = []

    async def handler(event, d):
        calls.append(1)
        return "OK"

    result = await mw(handler, object(), data)
    return result, len(calls)


async def test_owner_passes():
    mw = AllowlistMiddleware(frozenset({37186533}))
    result, n = await _run(mw, {"event_from_user": SimpleNamespace(id=37186533, username="m")})
    assert result == "OK" and n == 1


async def test_stranger_dropped():
    mw = AllowlistMiddleware(frozenset({37186533}))
    result, n = await _run(mw, {"event_from_user": SimpleNamespace(id=999, username="evil")})
    assert result is None and n == 0  # хендлер НЕ вызван


async def test_no_user_dropped():
    mw = AllowlistMiddleware(frozenset({37186533}))
    result, n = await _run(mw, {})
    assert result is None and n == 0


async def test_empty_allowlist_is_fail_closed():
    """Пустой allowlist роняет ВСЕХ, включая владельца — а не открывает бот всем."""
    mw = AllowlistMiddleware(frozenset())
    result, n = await _run(mw, {"event_from_user": SimpleNamespace(id=37186533, username="m")})
    assert result is None and n == 0
