"""Владелец ленты: кто может уводить пуши и крутить общие настройки.

22.08 чужой /start переписал chat_id на себя, и владелец три дня не получал ничего —
пуши уходили другому человеку из allowlist. Тесты фиксируют разделение.
"""

import pytest

from homyak.adapters.outputs.tg_bot import is_owner, owner_id
from homyak.core.config import settings


def test_owner_is_the_first_id_of_the_allowlist_by_default():
    assert owner_id("", "37186533,80436293,8613286183") == 37186533


def test_explicit_owner_wins_over_the_allowlist_order():
    assert owner_id("80436293", "37186533,80436293") == 80436293


def test_owner_survives_spaces_and_junk():
    assert owner_id("", " 37186533 , 80436293 ") == 37186533
    assert owner_id("не-число", "37186533") == 37186533


def test_no_allowlist_means_no_owner():
    """Пустой allowlist = бот и так никого не пускает, отдельный гейт не нужен."""
    assert owner_id("", "") is None


@pytest.fixture
def owned(monkeypatch):
    monkeypatch.setattr(settings, "telegram_owner_id", "", raising=False)
    monkeypatch.setattr(settings, "telegram_allowed_ids", "37186533,8613286183", raising=False)


def test_only_the_owner_passes_the_gate(owned):
    assert is_owner(37186533) is True
    assert is_owner(8613286183) is False  # тот, кто увёл пуши 22.08
    assert is_owner(None) is False


def test_without_an_allowlist_everyone_passes(monkeypatch):
    """Fail-closed уже сделан allowlist'ом; при пустом списке бот не отвечает никому."""
    monkeypatch.setattr(settings, "telegram_owner_id", "", raising=False)
    monkeypatch.setattr(settings, "telegram_allowed_ids", "", raising=False)
    assert is_owner(12345) is True
