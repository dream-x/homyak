"""Карточка ⭐-канала: русская выжимка статьи с проверкой на выдумки.

Отличие от `llm_summarizer` (тот держит язык оригинала): здесь ВСЕГДА русский — канал русский,
а перемешанные языки в ленте читаются рвано.

Главное требование — «чёткая, не выдуманная линия статьи». Промптом это не гарантируется, поэтому
защита трёхслойная:

1. Нет текста — нет пересказа. Если статьи не досталось, карточка выходит голой (заголовок +
   ссылка): треть звёзд приходит с lobsters/hn/github, где текста в базе 0-130 символов, и
   пересказывать там нечего. Скупая карточка честнее выдуманной.
2. Три режима по объёму исходника: full (линия + тезисы), brief (одна строка из короткого
   описания — по сути перевод) и bare (без пересказа вовсе).
3. Проверка заземления (`ungrounded`) — детерминированная, без второго LLM-вызова: каждое
   «проверяемое» слово выжимки (число или латинское имя собственное) обязано встречаться в
   исходнике. Что не подтвердилось — выбрасывается вместе со своей фразой, а не переписывается.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

# Границы режимов. MIN_FULL — по данным: у настоящих статей текст 800+, у github-карточек 80-130.
MIN_FULL = 600
MIN_BRIEF = 60

_SYSTEM_FULL = (
    "Ты пересказываешь техническую статью для канала русскоязычного инженера. "
    "Верни СТРОГО JSON: {\"line\": \"...\", \"points\": [\"...\", \"...\"]}\n\n"
    "line — ОДНА фраза (до 200 символов): о чём статья и что именно в ней утверждается. "
    "Это линия статьи, а не тема: не «про базы данных», а что конкретно автор показал, "
    "заявил или измерил.\n"
    "points — 2-3 тезиса, каждый до 150 символов: конкретика из текста. Без воды и без "
    "повторов line.\n\n"
    "ЯЗЫК: всё по-русски, ни одного английского слова вне имён собственных. Названия "
    "продуктов, компаний, языков, команд и переменных оставляй как в оригинале "
    "(Rust, Kubernetes, OpenAI, AGENTS.md).\n\n"
    "ТОЧНОСТЬ — важнее полноты. Правила, каждое из которых обязательно:\n"
    "1. Каждая фраза должна опираться на конкретное место в тексте. Не можешь показать "
    "это место — не пиши фразу.\n"
    "2. Даты, версии, числа и статусы бери дословно. Не помнишь точно — не упоминай вовсе.\n"
    "3. Термины переводи точно, а не по созвучию (credentials — учётные данные, а не "
    "сертификаты). Нет точного русского — оставь английский.\n"
    "4. Сохраняй оговорки и ограничения. Если в тексте сказано, что что-то работает "
    "ТОЛЬКО так или НЕ работает как-то, либо передай это целиком, либо не бери этот факт.\n"
    "5. Не связывай факты причинами и последовательностями, которых в тексте нет, и не "
    "делай выводов за автора.\n"
    "6. Ничего не добавляй из своих знаний о предмете.\n\n"
    "Лучше два коротких точных тезиса, чем три с натяжкой."
)

_SYSTEM_BRIEF = (
    "Ты оформляешь короткую карточку для русскоязычного технического канала. "
    "На входе — краткое описание проекта или анонса (часто английское). "
    "Верни СТРОГО JSON: {\"line\": \"...\"}\n\n"
    "line — одна фраза по-русски (до 200 символов): что это такое и для чего. "
    "По сути перевод и уплотнение описания. Названия оставляй как в оригинале.\n"
    "Ничего не добавляй от себя: ни возможностей, ни цифр, ни оценок, которых нет во входе."
)

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON = re.compile(r"\{.*\}", re.DOTALL)

# Что считаем проверяемым фактом: латинское слово (имя продукта/компании) и число.
# Хвостовую пунктуацию срезаем отдельно: «AI-агент» давал токен «ai-», которого нет ни в
# одном исходнике, и проверка убивала совершенно нормальную фразу.
_LATIN = re.compile(r"[a-z][a-z0-9+#._-]*")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_NORM = re.compile(r"[^a-z0-9а-яё.,+#_-]+")
_EDGE = "._-+#,"
_DASHES = re.compile(r"[‐-―−­]")  # неразрывный дефис, тире, мягкий перенос
_SPACES = re.compile(r"[    \s]+")  # NBSP и узкие пробелы разрядки
_THOUSANDS = re.compile(r"(\d) (\d{3})(?!\d)")
_SEPS = re.compile(r"[ ._-]+")

# Латиница, которая не является утверждением: техножаргон, живущий в русской речи сам по себе.
# Без этого списка «open source» или «AI-агент» в выжимке ловились бы как выдумка.
_GENERIC = frozenset(
    {
        "open", "source", "opensource", "web", "api", "cloud", "self", "hosted", "selfhosted",
        "llm", "llms", "gpu", "cpu", "ram", "sdk", "cli", "ide", "http", "https", "url", "json",
        "yaml", "sql", "git", "pdf", "css", "html", "rest", "grpc", "docker", "linux", "ios",
        "and", "the", "for", "with", "from", "your", "you", "not", "все", "это",
    }
)


# Числительные словами → цифра. Английские статьи пишут «four remained», а пересказ — «4»,
# и без этой таблицы совершенно верный факт считался бы выдуманным (поймано на живых ⭐).
_WORD_NUMS: dict[str, str] = {}
for _digit, _forms in {
    "1": "one один одна одно одного одной одним одну",
    "2": "two два две двух двум двумя",
    "3": "three три трех трёх трем трём тремя",
    "4": "four четыре четырех четырёх четырем четырём четырьмя",
    "5": "five пять пяти пятью",
    "6": "six шесть шести шестью",
    "7": "seven семь семи семью",
    "8": "eight восемь восьми восьмью",
    "9": "nine девять девяти девятью",
    "10": "ten десять десяти десятью",
    "11": "eleven одиннадцать одиннадцати",
    "12": "twelve двенадцать двенадцати",
}.items():
    for _form in _forms.split():
        _WORD_NUMS[_form] = _digit

_WORD = re.compile(r"[a-zа-яё]+")

# Qwen изредка роняет иероглиф прямо в русскую фразу («常驻 (резидентный) процесс» — поймано
# на живой ⭐). В публичный канал такое отдавать нельзя, а «почистить» нечем: выкидываем фразу.
_FOREIGN = re.compile(r"[぀-ヿ㐀-鿿가-힯֐-׿؀-ۿ]")


@dataclass
class Card:
    """Готовая выжимка. mode: full | brief | bare (без пересказа)."""

    line: str | None = None
    points: list[str] = field(default_factory=list)
    mode: str = "bare"
    dropped: list[str] = field(default_factory=list)  # что выкинула проверка заземления

    @property
    def has_text(self) -> bool:
        return bool(self.line or self.points)


def _norm(text: str) -> str:
    """Приведение к сравнимому виду. Юникод-типографика тут не косметика, а корректность:
    исходники приходят с неразрывным дефисом в «GPT‑5.5» и тонким пробелом в «1 290», и
    без выравнивания оба факта считались бы выдуманными (проверено на живых звёздах)."""
    s = (text or "").lower()
    s = _DASHES.sub("-", s)
    s = _SPACES.sub(" ", s)
    s = _THOUSANDS.sub(r"\1\2", s)  # «1 290» → «1290», иначе число не совпадёт с пересказом
    return " " + _NORM.sub(" ", s) + " "


def _squash(text: str) -> str:
    """Без разделителей — для имён: «gpt-5.5» должно находиться в «gpt 5.5» и наоборот."""
    return _SEPS.sub("", text)


def facts(text: str) -> set[str]:
    """«Проверяемые» единицы фразы: латинские слова и числа.

    Кириллицу не проверяем сознательно: пересказ и обязан переформулировать русскую речь
    своими словами. Ловим ровно то, что модели свойственно выдумывать — имена и цифры.
    """
    low = _norm(text)
    out = set()
    for raw in _LATIN.findall(low):
        w = raw.strip(_EDGE)
        if len(w) >= 3 and w not in _GENERIC:
            out.add(w)
    out |= {n.rstrip(_EDGE).replace(",", ".") for n in _NUM.findall(low)}
    return out


def ungrounded(fragment: str, source: str) -> set[str]:
    """Факты фрагмента, которых нет в исходнике. Пусто = фрагменту можно верить."""
    src = _norm(source)
    src_squashed = _squash(src)
    src_nums = {n.rstrip(_EDGE).replace(",", ".") for n in _NUM.findall(src)}
    src_nums |= {_WORD_NUMS[w] for w in _WORD.findall(src) if w in _WORD_NUMS}
    bad = set()
    for f in facts(fragment):
        if f[0].isdigit():
            # Число: только точное совпадение — подстрокой «5» нашлась бы внутри «2025».
            if f not in src_nums:
                bad.add(f)
        # Имя: подстрокой (github ≈ github.com, Qwen ≈ qwen3), плюс вариант без разделителей.
        elif f not in src and _squash(f) not in src_squashed:
            bad.add(f)
    return bad


def _reject(fragment: str, source: str) -> str | None:
    """Причина, по которой фразу нельзя публиковать (None = можно)."""
    if _FOREIGN.search(fragment):
        return "чужое письмо"
    bad = ungrounded(fragment, source)
    return ", ".join(sorted(bad)) if bad else None


def filter_grounded(card: Card, source: str) -> Card:
    """Выбрасывает фразы с неподтверждёнными фактами — по одной, а не всю карточку.

    Перегенерацию не делаем: та же модель на том же тексте выдумает то же самое, а лишний
    вызов удваивает задержку. Отброшенное пишем в `dropped` — это сигнал качества промпта.
    """
    dropped: list[str] = []
    line = card.line
    if line:
        why = _reject(line, source)
        if why:
            dropped.append(f"line: {why}")
            line = None
    points = []
    for p in card.points:
        why = _reject(p, source)
        if why:
            dropped.append(f"point: {why}")
        else:
            points.append(p)
    # Линия — стержень карточки; без неё тезисы висят в воздухе, режим падает до bare.
    mode = card.mode if line else "bare"
    return Card(
        line=line,
        points=points if line else [],
        mode=mode,
        dropped=dropped,
    )


def from_data(data: dict) -> Card:
    """dict модели → Card. Терпит кривые формы: points строкой, мусор вместо списка."""
    line = str(data.get("line") or "").strip().strip('"') or None
    raw_points = data.get("points") or []
    if isinstance(raw_points, str):  # модель иногда склеивает тезисы в одну строку
        raw_points = [p for p in raw_points.split("\n") if p.strip()]
    if not isinstance(raw_points, list):
        raw_points = []
    points = [str(p).strip().lstrip("•-—*").strip() for p in raw_points]
    return Card(line=line, points=[p for p in points if p][:3])


def _parse(raw: str) -> Card:
    """Текстовый ответ → Card. Запасной путь: основной — `chat_json` (format=json у Ollama)."""
    text = _THINK.sub("", raw or "").strip()
    m = _JSON.search(text)
    if not m:
        return Card()
    try:
        return from_data(json.loads(m.group(0)))
    except json.JSONDecodeError:
        return Card()


def choose_mode(text: str | None) -> str:
    n = len((text or "").strip())
    if n >= MIN_FULL:
        return "full"
    return "brief" if n >= MIN_BRIEF else "bare"


async def make_card(title: str | None, text: str | None, llm) -> Card:
    """Заголовок+текст → проверенная выжимка. Ошибка LLM = голая карточка, не исключение."""
    source = (text or "").strip()
    mode = choose_mode(source)
    if mode == "bare":
        return Card(mode="bare")
    system = _SYSTEM_FULL if mode == "full" else _SYSTEM_BRIEF
    user = f"Заголовок: {title or '—'}\n\nТекст:\n{source[:6000]}"
    # chat_json (format=json), а не chat_text: на просьбе «верни СТРОГО JSON» текстом модель
    # половину ответов оборачивала в прозу и карточки молча выходили голыми.
    try:
        card = from_data(await llm.chat_json(system, user))
    except Exception as e:
        log.warning("starcard_llm_failed", error=str(e)[:150])
        return Card(mode="bare")
    card.mode = mode
    if not card.has_text:
        return Card(mode="bare")
    # Заголовок — часть исходника: модель законно опирается на него, а в text его может не быть.
    return filter_grounded(card, f"{title or ''}\n{source}")
