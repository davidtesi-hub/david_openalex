"""Phase 2: six open PDF answers from qualifying OpenAlex articles."""
import datetime as dt
import gc
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

import fitz
from huggingface_hub import hf_hub_download
from llama_cpp import Llama

from pdf_local_rag import QUESTIONS, build_chunks, retrieve
from pdf_weekday import DAY_GROUPS, group_for_timestamp, qwen_pdf_db

try:
    from scripts.library_storage import restore, journal_paths
except ModuleNotFoundError:
    from library_storage import restore, journal_paths

ROOT = Path(__file__).resolve().parents[1]
GROUP = os.environ.get("PDF_REVIEW_DAY_GROUP", "monday")
DB = qwen_pdf_db(ROOT, GROUP)
MODEL = "Qwen3-8B Q4_K_M + PDF RAG"
CONTEXT_TOKENS = 16384
OUTPUT_TOKENS = 3500
ANSWER_COLUMNS = ("id", "modelo", "version_preguntas", "intentos", "pdf_url", "pdf_version",
                  "pdf_source", "modalidad", *(f"answer{i}" for i in range(1, 7)),
                  "raw_response", "completed_at_utc")
ANSWER_SCHEMA = """CREATE TABLE {table} (
      id TEXT PRIMARY KEY, modelo TEXT NOT NULL,
      version_preguntas INTEGER NOT NULL CHECK(version_preguntas=1),
      intentos INTEGER NOT NULL DEFAULT 0 CHECK(intentos IN (-1,0,1,2,3)),
      pdf_url TEXT, pdf_version TEXT, pdf_source TEXT,
      modalidad TEXT CHECK(modalidad='rag'),
      answer1 TEXT, answer2 TEXT, answer3 TEXT,
      answer4 TEXT, answer5 TEXT, answer6 TEXT,
      raw_response TEXT, completed_at_utc TEXT)"""


def open_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='answers'").fetchone():
        db.execute(ANSWER_SCHEMA.format(table="answers"))
    names = [row[1] for row in db.execute("PRAGMA table_info(answers)")]
    if "intentos" not in names:
        db.execute("ALTER TABLE answers ADD COLUMN intentos INTEGER NOT NULL DEFAULT 0")
        names = [row[1] for row in db.execute("PRAGMA table_info(answers)")]
    schema = db.execute("SELECT sql FROM sqlite_master WHERE name='answers'").fetchone()[0]
    if names != list(ANSWER_COLUMNS) or "answer1 TEXT NOT NULL" in schema or "(-1,0,1,2,3)" not in schema:
        db.execute(ANSWER_SCHEMA.format(table="answers_reordered"))
        selected = ",".join("CASE WHEN answer1 IS NOT NULL THEN 0 ELSE intentos END"
                            if key == "intentos" else key for key in ANSWER_COLUMNS)
        db.execute(f"INSERT INTO answers_reordered ({','.join(ANSWER_COLUMNS)}) SELECT {selected} FROM answers")
        db.execute("DROP TABLE answers")
        db.execute("ALTER TABLE answers_reordered RENAME TO answers")
    db.commit()
    return db


def pdf_locations(work):
    """Return distinct open PDF candidates in preferred order."""
    locations = [work.get("primary_location") or {}, work.get("best_oa_location") or {}]
    locations.extend(work.get("locations") or [])
    found, seen = [], set()
    for item in locations:
        url = item.get("pdf_url") or ""
        if item.get("is_oa") and url.startswith("https://") and url not in seen:
            seen.add(url)
            source = item.get("source") or {}
            found.append((url, item.get("version") or "unknown",
                          source.get("display_name") or item.get("id") or "unknown"))
    return found


def candidates(db):
    done = {row[0] for row in db.execute("SELECT id FROM answers WHERE intentos IN (0,-1)")}
    for group in DAY_GROUPS:
        path = qwen_pdf_db(ROOT, group)
        if path == DB or not path.exists():
            continue
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as other:
            done.update(row[0] for row in other.execute("SELECT id FROM answers WHERE intentos IN (0,-1)"))
    qualified = set()
    for path in sorted((ROOT / "abstract_evaluation").glob("rubric_v3_arxiv_style_qwen3_8b*.sqlite")):
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
            for aid, score, q2, q3 in source.execute("SELECT id,puntuacion,Q2,Q3 FROM evaluations"):
                if score is not None and (score > 800 or (700 < score <= 800 and q2 == "D" and q3 in ("C", "D"))):
                    qualified.add(aid)
    found = {}
    for path in journal_paths(ROOT / "library"):
        path = restore(path)
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
            if not source.execute("SELECT 1 FROM sqlite_master WHERE name='papers'").fetchone():
                continue
            for aid, day, raw in source.execute("SELECT id,publication_date,raw_json FROM papers"):
                if aid in done or aid not in qualified or aid in found or not day:
                    continue
                if group_for_timestamp(day) != GROUP:
                    continue
                work = json.loads(raw)
                if work.get("is_retracted"):
                    continue
                locations = pdf_locations(work)
                if locations:
                    found[aid] = (day, path.stem, locations)
    return sorted(((day, aid, journal, locations)
                   for aid, (day, journal, locations) in found.items()), reverse=True)


def mark_attempt(db, aid):
    row = db.execute("SELECT intentos FROM answers WHERE id=?", (aid,)).fetchone()
    if row and row[0] in (0, -1):
        return None
    attempt = min(row[0] + 1, 3) if row else 1
    db.execute("""INSERT INTO answers(id,modelo,version_preguntas,intentos) VALUES(?,?,1,?)
                  ON CONFLICT(id) DO UPDATE SET intentos=excluded.intentos""", (aid, MODEL, attempt))
    db.commit()
    return attempt


def mark_failed(db, aid, attempt):
    if attempt == 3:
        db.execute("UPDATE answers SET intentos=-1 WHERE id=? AND intentos=3", (aid,))
        db.commit()


def first_readable_pdf(locations):
    errors = []
    for url, version, source in locations:
        try:
            return url, version, source, fetch_pdf(url)
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    raise ValueError("No readable open PDF; " + " | ".join(errors))


def fetch_pdf(url):
    req = urllib.request.Request(url, headers={"User-Agent": "david-openalex-pdf-review/1.0 (research; GitHub Actions)"})
    with urllib.request.urlopen(req, timeout=60) as response:
        if response.geturl().lower().startswith("http://"):
            raise ValueError("PDF redirected to insecure HTTP")
        chunks, size = [], 0
        while chunk := response.read(65536):
            size += len(chunk)
            if size > 40_000_000:
                raise ValueError("PDF exceeds 40 MB")
            chunks.append(chunk)
    data = b"".join(chunks)
    if not data.startswith(b"%PDF-"):
        raise ValueError("Downloaded content is not a PDF")
    return data


def supplied_text(data):
    with fitz.open(stream=data, filetype="pdf") as document:
        pages = [f"[PDF page {i+1}]\n{page.get_text(sort=True)}" for i, page in enumerate(document)]
    if len(pages) < 2 or sum(map(len, pages)) < 3000:
        raise ValueError("PDF text is missing or illegible")
    chunks = build_chunks(pages)
    selected = {}
    for query in QUESTIONS.values():
        for page, body in retrieve(chunks, query, count=2):
            selected[(page, body)] = True
    if not selected:
        raise ValueError("No relevant PDF passages retrieved")
    return "\n\n".join(f"[PDF page {page}]\n{body}" for page, body in selected)


def split_answers(raw):
    if "</think>" in raw:
        raw = raw.split("</think>", 1)[1]
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[1:-1]).strip()
    matches = list(re.finditer(r"(?m)^\s*([1-6])\s*[.):]\s*", raw))
    if len(matches) != 6 or [int(match.group(1)) for match in matches] != list(range(1, 7)):
        raise ValueError("Expected six numbered answers in order")
    answers = [raw[m.end():matches[i + 1].start() if i < 5 else len(raw)].strip()
               for i, m in enumerate(matches)]
    if any(not answer for answer in answers):
        raise ValueError("One or more answers are empty")
    return answers


def checkpoint(db):
    db.close()
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", DB.relative_to(ROOT).as_posix()], cwd=ROOT, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return open_db()
    subprocess.run(["git", "commit", "-m", f"Save OpenAlex PDF answers {GROUP}"], cwd=ROOT, check=True)
    for attempt in range(1, 9):
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT, check=True)
        if subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=ROOT).returncode == 0:
            return open_db()
        time.sleep(min(15, 3 * attempt))
    raise RuntimeError("Could not push PDF answers")


def main():
    deadline = int(os.environ.get("PDF_REVIEW_DEADLINE_EPOCH", str(int(time.time()) + 345 * 60)))
    db = open_db()
    papers = candidates(db)
    print(f"{MODEL} {GROUP}: {len(papers)} open PDFs pending", flush=True)
    if not papers:
        db.close()
        return
    model_path = hf_hub_download(repo_id="Qwen/Qwen3-8B-GGUF", filename="Qwen3-8B-Q4_K_M.gguf")
    model = Llama(model_path=model_path, n_ctx=CONTEXT_TOKENS,
                  n_threads=min(4, os.cpu_count() or 1), n_batch=256, verbose=False)
    instructions = (ROOT / "scripts" / "pdf_open_questions_rag.txt").read_text(encoding="utf-8-sig")
    for _, aid, journal, locations in papers:
        if time.time() >= deadline:
            break
        attempt = mark_attempt(db, aid)
        if attempt is None:
            continue
        started = time.monotonic()
        try:
            url, version, source, pdf = first_readable_pdf(locations)
            body = supplied_text(pdf)
            request = instructions + "\n\n<PAPER_TEXT>\n" + body + "\n</PAPER_TEXT>"
            token_count = len(model.tokenize(request.encode("utf-8")))
            if token_count + OUTPUT_TOKENS + 64 > CONTEXT_TOKENS:
                raise ValueError(f"PDF passages exceed model context budget: {token_count} input tokens")
            result = model.create_chat_completion(messages=[{"role": "user", "content": request}],
                                                  temperature=0.1, max_tokens=OUTPUT_TOKENS)
            raw = result["choices"][0]["message"]["content"] or ""
            answers = split_answers(raw)
            values = (aid, MODEL, 1, 0, url, version, source, "rag", *answers, raw,
                      dt.datetime.now(dt.timezone.utc).isoformat())
            columns = ",".join(ANSWER_COLUMNS)
            placeholders = ",".join("?" for _ in ANSWER_COLUMNS)
            updates = ",".join(f"{key}=excluded.{key}" for key in ANSWER_COLUMNS[3:])
            db.execute(f"INSERT INTO answers({columns}) VALUES({placeholders}) "
                       f"ON CONFLICT(id) DO UPDATE SET {updates} WHERE answers.intentos IN (1,2,3)", values)
            db.commit()
            print(f"{aid} {journal} attempt={attempt} duration_s={time.monotonic()-started:.1f} "
                  "score=None error=None status=valid", flush=True)
        except Exception as exc:
            mark_failed(db, aid, attempt)
            print(f"{aid} {journal} attempt={attempt} duration_s={time.monotonic()-started:.1f} "
                  f"score=None error={type(exc).__name__}: {exc} status=error", flush=True)
        finally:
            gc.collect()
        db = checkpoint(db)
    db.close()


if __name__ == "__main__":
    main()
