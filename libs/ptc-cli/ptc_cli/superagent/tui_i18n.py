"""Locale resolution and string lookup for the TUI.

Every screen keeps its copy next to the renderer that prints it, as a
``STRINGS = {"en": {...}, "zh-CN": {...}}`` table, and reads it through :func:`t`.
The locale comes from ``CONDUCTOR_LOCALE`` first, then ``LC_ALL``, ``LC_MESSAGES``
and ``LANG``: anything that starts with ``zh`` is zh-CN and everything else is
English, because a terminal that cannot say which language it speaks still has to
render.

A table that is missing a zh-CN key is a bug in the renderer, not something an
operator should see half-translated, so under pytest — or with ``PTC_I18N_PARITY``
set — :func:`t` raises instead of quietly answering in English. At runtime the same
call falls back, because a missing translation must not take the screen down.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

EN = "en"
ZH_CN = "zh-CN"
LOCALES = (EN, ZH_CN)
DEFAULT_LOCALE = EN
# Read in this order; the first one that says something wins.
LOCALE_ENV_KEYS = ("CONDUCTOR_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")
_PARITY_OFF = frozenset({"0", "false", "no", "off"})


class MissingTranslationError(KeyError):
    """A table does not carry a key, or does not carry it in every locale."""


def normalize_locale(raw: object) -> str:
    """``zh-CN`` for anything that starts with ``zh``, ``en`` for everything else."""
    text = str(raw or "").strip().lower().replace("-", "_")
    return ZH_CN if text.startswith("zh") else EN


def resolve_locale(environ: Mapping[str, str] | None = None) -> str:
    """The locale of this process: ``CONDUCTOR_LOCALE``, else the ``LC_*``/``LANG``."""
    environment = os.environ if environ is None else environ
    raw = ""
    for name in LOCALE_ENV_KEYS:
        raw = environment.get(name, "").strip()
        if raw:
            break
    return normalize_locale(raw)


def parity_enforced(environ: Mapping[str, str] | None = None) -> bool:
    """True when a missing zh-CN key must raise instead of falling back to English.

    Pytest sets ``PYTEST_CURRENT_TEST`` for the duration of a test, which is what
    makes the renderers' own tables hold their parity without a fixture. An
    operator can turn it off with ``PTC_I18N_PARITY=0`` and on with any other
    non-empty value.
    """
    environment = os.environ if environ is None else environ
    override = environment.get("PTC_I18N_PARITY", "").strip().lower()
    if override in _PARITY_OFF:
        return False
    if override:
        return True
    return bool(environment.get("PYTEST_CURRENT_TEST", "").strip())


def _section(table: object, locale: str) -> Mapping[str, str] | None:
    """The ``{key: string}`` mapping of one locale, or None when there is none.

    A table is either keyed by locale or a flat ``{key: string}`` mapping; a flat
    table is its own English section and needs no locale key.
    """
    if not isinstance(table, Mapping):
        return None
    values = list(table.values())
    if values and all(isinstance(value, Mapping) for value in values):
        section = table.get(locale)
        return section if isinstance(section, Mapping) else None
    return table


def strings(table: Mapping[str, Mapping[str, str]], locale: str | None = None) -> dict[str, str]:
    """The table's strings for a locale, in English when the table has no such locale."""
    resolved = locale or resolve_locale()
    section = _section(table, resolved)
    if section is None:
        section = _section(table, DEFAULT_LOCALE) or {}
    return dict(section)


def t(table: Mapping[str, Mapping[str, str]], key: str, locale: str | None = None) -> str:
    """The string for ``key`` in the resolved locale.

    A key the table does not carry at all is a bug in the caller and always
    raises. A key the table carries in English but not in the resolved locale
    raises while parity is enforced and falls back to English otherwise.
    """
    resolved = locale or resolve_locale()
    section = _section(table, resolved)
    if section is not None and key in section:
        return section[key]
    english = _section(table, DEFAULT_LOCALE)
    if english is not None and key in english:
        if resolved != DEFAULT_LOCALE and parity_enforced():
            message = f"{resolved} has no {key!r}; en has it"
            raise MissingTranslationError(message)
        return english[key]
    message = f"no {key!r} in this table"
    raise MissingTranslationError(message)


def parity_problems(table: Mapping[str, Mapping[str, str]]) -> list[str]:
    """Every key one locale has and another does not, and every missing section."""
    english = _section(table, DEFAULT_LOCALE)
    if english is None:
        return [f"the table has no {DEFAULT_LOCALE!r} section"]
    problems: list[str] = []
    for locale in LOCALES:
        section = _section(table, locale)
        if section is None:
            problems.append(f"{locale}: the table has no {locale!r} section")
            continue
        problems.extend(f"{locale}: missing {key!r}" for key in sorted(set(english) - set(section)))
        problems.extend(f"{locale}: unknown {key!r}" for key in sorted(set(section) - set(english)))
    return problems


def check_parity(table: Mapping[str, Mapping[str, str]]) -> None:
    """Raise :class:`MissingTranslationError` when a table is not bilingual."""
    problems = parity_problems(table)
    if problems:
        raise MissingTranslationError("; ".join(problems))
