"""Publication date used after collection: Crossref online date, then OpenAlex."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
try:
    from scripts.library_storage import restore
except ModuleNotFoundError:
    from library_storage import restore


def _day(value: str | None) -> str | None:
    if not value or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _doi(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value or None


def crossref_online_dates(library: Path) -> dict[str, str]:
    """Read collected Crossref metadata without changing either collector."""
    result = {}
    for archive in sorted((library / "crossref").glob("*.db")):
        path = restore(archive.with_suffix(".sqlite"))
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for doi, published_online in db.execute(
                "SELECT doi,published_online FROM papers WHERE published_online IS NOT NULL"
            ):
                key, day = _doi(doi), _day(published_online)
                if key and day:
                    result[key] = day
    return result


def effective_date(doi: str | None, openalex_date: str | None,
                   online_dates: dict[str, str]) -> str | None:
    return online_dates.get(_doi(doi)) or _day(openalex_date)
