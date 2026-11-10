#!/usr/bin/env python3
"""Build static OpenAlex weekday totals and evaluation status."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

try:
    from scripts.library_storage import restore, journal_paths
except ModuleNotFoundError:
    from library_storage import restore, journal_paths

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "library"
EVALUATIONS = ROOT / "abstract_evaluation"
OUTPUT = ROOT / "dashboard" / "data" / "weekday_totals.json"
JOURNALS = list(json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["journals"])
EVALUATION_DATABASES = [
    "rubric_v3_arxiv_style_qwen3_8b.sqlite",
    "rubric_v3_arxiv_style_qwen3_8b_tuesday.sqlite",
    "rubric_v3_arxiv_style_qwen3_8b_wednesday.sqlite",
    "rubric_v3_arxiv_style_qwen3_8b_thursday.sqlite",
    "rubric_v3_arxiv_style_qwen3_8b_friday.sqlite",
    "rubric_v3_arxiv_style_qwen3_8b_weekend.sqlite",
]
WEEKDAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def read_journal(slug: str) -> dict[str, dt.date]:
    path = restore(LIBRARY / f"{slug}.sqlite")
    if not path.exists():
        raise FileNotFoundError(f"Falta la base del journal {path}")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        records = {}
        for work_id, publication_date in db.execute("SELECT id, publication_date FROM papers"):
            try:
                records[work_id] = dt.date.fromisoformat(publication_date[:10])
            except (TypeError, ValueError):
                continue
        return records
    finally:
        db.close()


def evaluated_ids() -> set[str]:
    result = set()
    for name in EVALUATION_DATABASES:
        path = EVALUATIONS / name
        if not path.exists():
            continue
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            result.update(row[0] for row in db.execute(
                "SELECT id FROM evaluations WHERE puntuacion IS NOT NULL"
            ))
        finally:
            db.close()
    return result


def weekday_counts(records, evaluated, start=None, end=None):
    result = [{"evaluated": 0, "pending": 0} for _ in range(7)]
    for work_id, published in records.items():
        if start is not None and not start <= published <= end:
            continue
        result[published.weekday()]["evaluated" if work_id in evaluated else "pending"] += 1
    return result


def add_period(records, evaluated, today, week_start, previous_start, previous_end):
    quarter_start = today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1)
    half_start = today.replace(month=1 if today.month <= 6 else 7, day=1)
    return {
        "total": len(records),
        "current_week": weekday_counts(records, evaluated, week_start, today),
        "previous_week": weekday_counts(records, evaluated, previous_start, previous_end),
        "current_month": weekday_counts(records, evaluated, today.replace(day=1), today),
        "current_quarter": weekday_counts(records, evaluated, quarter_start, today),
        "current_half_year": weekday_counts(records, evaluated, half_start, today),
        "current_year": weekday_counts(records, evaluated, today.replace(month=1, day=1), today),
        "all_time": weekday_counts(records, evaluated),
    }


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    week_start = today - dt.timedelta(days=today.weekday())
    previous_start = week_start - dt.timedelta(days=7)
    previous_end = week_start - dt.timedelta(days=1)
    evaluated = evaluated_ids()
    journals = {}
    all_records = {}
    for slug in JOURNALS:
        records = read_journal(slug)
        journals[slug] = add_period(records, evaluated, today, week_start, previous_start, previous_end)
        all_records.update(records)
    payload = {
        "generated_at_utc": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "today_utc": today.isoformat(),
        "week_start_utc": week_start.isoformat(),
        "previous_week_start_utc": previous_start.isoformat(),
        "previous_week_end_utc": previous_end.isoformat(),
        "month_start_utc": today.replace(day=1).isoformat(),
        "quarter_start_utc": today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1).isoformat(),
        "half_year_start_utc": today.replace(month=1 if today.month <= 6 else 7, day=1).isoformat(),
        "year_start_utc": today.replace(month=1, day=1).isoformat(),
        "weekdays": WEEKDAYS,
        "counting_rule": "publication_date; combined total deduplicated by OpenAlex work ID",
        "evaluation_databases": EVALUATION_DATABASES,
        "journals": journals,
        "Total": add_period(all_records, evaluated, today, week_start, previous_start, previous_end),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Escrito {OUTPUT.relative_to(ROOT)}. Total único: {len(all_records):,}")


if __name__ == "__main__":
    main()
