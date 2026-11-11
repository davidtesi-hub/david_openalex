#!/usr/bin/env python3
"""Build static OpenAlex weekday totals and evaluation status."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from effective_publication_date import crossref_online_dates, effective_date

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


def read_journal(slug: str, online_dates: dict[str, str]) -> dict[str, dt.date]:
    path = restore(LIBRARY / f"{slug}.sqlite")
    if not path.exists():
        raise FileNotFoundError(f"Falta la base del journal {path}")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        records = {}
        for work_id, doi, publication_date in db.execute("SELECT id, doi, publication_date FROM papers"):
            try:
                day = effective_date(doi, publication_date, online_dates)
                records[work_id] = dt.date.fromisoformat(day)
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


def phase_ids() -> tuple[set[str], set[str], set[str]]:
    phase1, phase2, phase3 = evaluated_ids(), set(), set()
    for path in sorted((EVALUATIONS / "pdf_answers_v1").glob("pdf_answers_qwen3_rag_8b*.sqlite")):
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            phase2.update(row[0] for row in db.execute("SELECT id FROM answers WHERE intentos=0"))
        finally:
            db.close()
    path = EVALUATIONS / "phase3" / "pdf_score_qwen3_8b.sqlite"
    if path.exists():
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            phase3.update(row[0] for row in db.execute(
                "SELECT id FROM evaluations WHERE puntuacion IS NOT NULL"
            ))
        finally:
            db.close()
    return phase1, phase2, phase3


def weekday_counts(records, phase1, phase2, phase3, start=None, end=None):
    result = [{"phase3": 0, "phase2": 0, "phase1": 0, "collected": 0} for _ in range(7)]
    for work_id, published in records.items():
        if start is not None and not start <= published <= end:
            continue
        stage = ("phase3" if work_id in phase3 else "phase2" if work_id in phase2
                 else "phase1" if work_id in phase1 else "collected")
        result[published.weekday()][stage] += 1
    return result


def add_period(records, stage_ids, today, week_start, previous_start, previous_end):
    quarter_start = today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1)
    half_start = today.replace(month=1 if today.month <= 6 else 7, day=1)
    phase1, phase2, phase3 = stage_ids
    counts = lambda start=None, end=None: weekday_counts(
        records, phase1, phase2, phase3, start, end
    )
    return {
        "total": len(records),
        "current_week": counts(week_start, today),
        "previous_week": counts(previous_start, previous_end),
        "current_month": counts(today.replace(day=1), today),
        "current_quarter": counts(quarter_start, today),
        "current_half_year": counts(half_start, today),
        "current_year": counts(today.replace(month=1, day=1), today),
        "all_time": counts(),
    }


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    week_start = today - dt.timedelta(days=today.weekday())
    previous_start = week_start - dt.timedelta(days=7)
    previous_end = week_start - dt.timedelta(days=1)
    stage_ids = phase_ids()
    online_dates = crossref_online_dates(LIBRARY)
    journals = {}
    all_records = {}
    for slug in JOURNALS:
        records = read_journal(slug, online_dates)
        journals[slug] = add_period(records, stage_ids, today, week_start, previous_start, previous_end)
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
        "counting_rule": "Crossref published-online by DOI, otherwise OpenAlex publication_date; combined total deduplicated by OpenAlex work ID",
        "evaluation_databases": EVALUATION_DATABASES,
        "journals": journals,
        "Total": add_period(all_records, stage_ids, today, week_start, previous_start, previous_end),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Escrito {OUTPUT.relative_to(ROOT)}. Total único: {len(all_records):,}")


if __name__ == "__main__":
    main()
