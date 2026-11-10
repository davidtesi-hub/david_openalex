"""Select phase-3 OpenAlex works scoring at least 9.00 for phase 4."""
import datetime as dt
import json
import sqlite3
from pathlib import Path

from library_storage import journal_paths, restore

ROOT = Path(__file__).resolve().parents[1]
SCORES = ROOT / "abstract_evaluation/phase3/pdf_score_qwen3_8b.sqlite"
OUTPUT = ROOT / "abstract_evaluation/phase4/selected_papers.sqlite"


def sunday(value):
    day = dt.date.fromisoformat(value[:10])
    return (day + dt.timedelta(days=6 - day.weekday())).isoformat()


def locations(raw):
    seen = set()
    for loc in [raw.get("primary_location"), raw.get("best_oa_location"), *(raw.get("locations") or [])]:
        if not isinstance(loc, dict):
            continue
        key = (loc.get("landing_page_url"), loc.get("pdf_url"))
        if key not in seen:
            seen.add(key)
            yield loc


def is_arxiv(loc):
    source = loc.get("source") or {}
    return ("arxiv" in str(source.get("display_name") or "").lower()
            or "arxiv.org" in str(loc.get("landing_page_url") or "").lower()
            or "arxiv.org" in str(loc.get("pdf_url") or "").lower())


def urls(raw):
    locs = list(locations(raw))
    main = next((x for x in locs if x.get("landing_page_url")), None)
    preprint = next((x for x in locs if is_arxiv(x)), None)
    return (main.get("landing_page_url") if main else None,
            main.get("pdf_url") if main else None,
            preprint.get("landing_page_url") if preprint else None,
            preprint.get("pdf_url") if preprint else None)


def main():
    with sqlite3.connect(SCORES) as scores:
        wanted = {row[0] for row in scores.execute("SELECT id FROM evaluations WHERE puntuacion >= 900")}
    found = {}
    for path in journal_paths(ROOT / "library"):
        restored = restore(path)
        with sqlite3.connect(restored) as source:
            for work_id, date, raw_json in source.execute("SELECT id, publication_date, raw_json FROM papers"):
                if work_id not in wanted or not date:
                    continue
                main, pdf, preprint, preprint_pdf = urls(json.loads(raw_json))
                row = (work_id, date[:10], sunday(date), main, pdf, preprint, preprint_pdf)
                if work_id not in found:
                    found[work_id] = row
                else:
                    previous = found[work_id]
                    found[work_id] = tuple(a or b for a, b in zip(previous, row))
    missing = wanted - found.keys()
    if missing:
        raise RuntimeError(f"Selected OpenAlex IDs absent from collectors: {len(missing)}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".sqlite.tmp")
    temp.unlink(missing_ok=True)
    target = sqlite3.connect(temp)
    try:
        target.execute("""CREATE TABLE papers (
            id TEXT PRIMARY KEY, effective_date TEXT NOT NULL, week_sunday TEXT NOT NULL,
            landing_page TEXT, pdf_url TEXT, preprint_landing_page TEXT,
            preprint_pdf_url TEXT)""")
        target.executemany("INSERT INTO papers VALUES(?,?,?,?,?,?,?)", sorted(found.values()))
        target.execute("CREATE INDEX papers_week_sunday ON papers(week_sunday)")
        target.commit()
    finally:
        target.close()
    temp.replace(OUTPUT)
    print(f"Phase 4 OpenAlex: {len(found)} papers with score >=9.00")


if __name__ == "__main__":
    main()
