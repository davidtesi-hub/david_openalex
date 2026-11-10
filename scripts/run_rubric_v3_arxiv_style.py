#!/usr/bin/env python3
"""Evaluate the version 3 rubric from abstracts with literal evidence."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from collections import defaultdict, deque
from datetime import date
from itertools import chain
from pathlib import Path

from huggingface_hub import hf_hub_download
from llama_cpp import Llama, LlamaGrammar

from rubric_v3_arxiv_style_storage import open_db, save_response

try:
    from scripts.library_storage import restore, journal_paths
except ModuleNotFoundError:
    from library_storage import restore, journal_paths

ROOT = Path(__file__).resolve().parents[1]
MODEL_KEY = os.environ.get("RUBRIC_MODEL", "qwen3_8b")
MODELS = {
    "qwen3_8b": ("Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", "Qwen3-8B Q4_K_M"),
    "qwen3_14b": ("Qwen/Qwen3-14B-GGUF", "Qwen3-14B-Q4_K_M.gguf", "Qwen3-14B Q4_K_M"),
    "openscholar_8b": ("QuantFactory/Llama-3.1_OpenScholar-8B-GGUF", "Llama-3.1_OpenScholar-8B.Q4_K_M.gguf", "OpenScholar-8B Q4_K_M"),
}
if MODEL_KEY not in MODELS:
    raise ValueError("Unknown model")
MODEL_REPO, MODEL_FILE, MODEL_LABEL = MODELS[MODEL_KEY]
SHARD = os.environ.get("RUBRIC_WEEKDAY", "")
WEEKDAYS = {
    "monday": {0}, "tuesday": {1}, "wednesday": {2},
    "thursday": {3}, "friday": {4}, "weekend": {5, 6},
}
if SHARD and (SHARD not in WEEKDAYS or MODEL_KEY != "qwen3_8b"):
    raise ValueError("Invalid rubric weekday shard")
suffix = f"_{SHARD}" if SHARD and SHARD != "monday" else ""
DB_PATH = ROOT / "abstract_evaluation" / f"rubric_v3_arxiv_style_{MODEL_KEY}{suffix}.sqlite"


def publication_weekday(value):
    try:
        return date.fromisoformat((value or "")[:10]).weekday()
    except ValueError:
        return None

# The grammar enforces seven ordered choices. The parser independently verifies it.
GRAMMAR = 'root ::= "T=" type "\\nQ1=" choice "\\nE1=\\\"" txt "\\\"\\nJ1=\\\"" txt "\\\"\\nQ2=" choice "\\nE2=\\\"" txt "\\\"\\nJ2=\\\"" txt "\\\"\\nQ3=" choice "\\nE3=\\\"" txt "\\\"\\nJ3=\\\"" txt "\\\"\\nQ4=" choice "\\nE4=\\\"" txt "\\\"\\nJ4=\\\"" txt "\\\""\nchoice ::= [AaBbCcDdUu]\ntype ::= [TtEeMmSsPpUu]\ntxt ::= [^"\\n]*\n'

def prompt(abstract: str) -> str:
    return (ROOT / "scripts" / "rubric_v3_arxiv_style_prompt.txt").read_text(encoding="utf-8") + "\n<abstract>\n" + abstract.strip()[:10000] + "\n</abstract>"


def collect_candidates(db: sqlite3.Connection):
    journals = list(json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["journals"])
    done = {row[0] for row in db.execute("SELECT id FROM evaluations")}
    pending = {}
    seen = set(done)
    for journal in journals:
        path = restore(ROOT / "library" / f"{journal}.sqlite")
        if not path.exists():
            continue
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for work_id, abstract, day in source.execute("""
                SELECT id,abstract,publication_date FROM papers
                WHERE type='article' AND TRIM(COALESCE(abstract,''))<>''
                AND COALESCE(json_extract(raw_json,'$.is_retracted'),0)=0
                ORDER BY publication_date DESC,id DESC
            """):
                if work_id in seen:
                    continue
                seen.add(work_id)
                if SHARD and publication_weekday(day) not in WEEKDAYS[SHARD]:
                    continue
                pending[work_id] = (work_id, abstract, day or "", journal)
        finally:
            source.close()
    return list(pending.values()), journals


def ordered_candidates(papers, categories):
    priority_name = {"qwen3_8b": "priority_arxiv_style_mod0.json", "openscholar_8b": "priority_arxiv_style_mod1.json", "qwen3_14b": "priority_arxiv_style_mod2.json"}[MODEL_KEY]
    path = ROOT / "abstract_evaluation" / priority_name
    requests = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(requests, list):
        raise ValueError("Priority file must be a JSON array")
    by_id = {paper[0]: paper for paper in papers}
    seen = set()
    priorities = []
    for request in sorted((r for r in requests if isinstance(r, dict)), key=lambda r: r.get("requested_at_utc", "")):
        arxiv_id = request.get("work_id") or request.get("arxiv_id")
        if isinstance(arxiv_id, str):
            arxiv_id = arxiv_id.removeprefix("https://openalex.org/")
        if arxiv_id in by_id and arxiv_id not in seen:
            priorities.append(by_id[arxiv_id])
            seen.add(arxiv_id)
    days = defaultdict(lambda: defaultdict(deque))
    for paper in papers:
        if paper[0] not in seen:
            days[paper[2][:10]][paper[3]].append(paper)
    def regular():
        for day in sorted(days, reverse=True):
            queues = days[day]
            for category in categories:
                queues[category] = deque(sorted(queues[category], key=lambda p: (p[2],p[0]), reverse=True))
            while any(queues.values()):
                for category in categories:
                    if queues[category]:
                        yield queues[category].popleft()
    return chain(priorities, regular())


def infer(model: Llama, grammar: LlamaGrammar, abstract: str, seed: int):
    message = prompt(abstract)
    if MODEL_KEY in ("qwen3_8b", "qwen3_14b"):
        message = "/no_think\n" + message
    started = time.monotonic()
    output = model.create_chat_completion(
        messages=[{"role": "user", "content": message}],
        grammar=grammar, temperature=0.2, top_p=0.8, seed=seed,
        max_tokens=650,
    )
    raw = output["choices"][0]["message"]["content"] or ""
    usage = output.get("usage") or {}
    return raw, int((time.monotonic() - started)*1000), usage.get("prompt_tokens"), usage.get("completion_tokens")


def checkpoint(db: sqlite3.Connection):
    db.close()
    relative = DB_PATH.relative_to(ROOT).as_posix()
    subprocess.run(["git", "add", "--", relative], cwd=ROOT, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return open_db(DB_PATH)
    subprocess.run(["git", "-c", "user.name=github-actions[bot]", "-c",
                    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                    "commit", "-m", f"Guardar rúbrica v3 {MODEL_KEY}"], cwd=ROOT, check=True)
    for attempt in range(3):
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT, check=True)
        if subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=ROOT).returncode == 0:
            return open_db(DB_PATH)
        time.sleep(3*(attempt+1))
    raise RuntimeError("Could not publish SQLite checkpoint")


def main():
    deadline = int(os.environ.get("IMPORTANCE_DEADLINE_EPOCH", str(int(time.time())+345*60)))
    max_papers = int(os.environ.get("RUBRIC_MAX_PAPERS", "0") or "0")
    if max_papers < 0:
        raise ValueError("RUBRIC_MAX_PAPERS cannot be negative")
    db = open_db(DB_PATH)
    try:
        papers, categories = collect_candidates(db)
        print(f"{MODEL_LABEL}: {len(papers)} papers pending", flush=True)
        if not papers:
            return
        model_file = hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE)
        model = Llama(model_path=model_file, n_ctx=8192, n_threads=min(4,os.cpu_count() or 1),
                      n_batch=256, verbose=False)
        grammar = LlamaGrammar.from_string(GRAMMAR, verbose=False)
        next_checkpoint = time.monotonic()+30*60
        valid = invalid = processed = consecutive_failures = 0
        for arxiv_id, abstract, timestamp, category in ordered_candidates(papers,categories):
            if time.time() >= deadline or (max_papers and processed >= max_papers):
                break
            processed += 1
            paper_valid = False
            for attempt in (1,2):
                if time.time() >= deadline:
                    break
                attempt_started = time.monotonic()
                try:
                    raw,latency,input_tokens,output_tokens = infer(model,grammar,abstract,17+attempt-1)
                except Exception as error:
                    raw,latency,input_tokens,output_tokens = f"INFERENCE_ERROR: {type(error).__name__}: {error}",None,None,None
                score,error = save_response(db,arxiv_id=arxiv_id,model=MODEL_LABEL,abstract=abstract,
                    raw=raw,latency_ms=latency,input_tokens=input_tokens,output_tokens=output_tokens)
                elapsed_seconds = time.monotonic() - attempt_started
                print(f"{arxiv_id} {category} attempt={attempt} duration_s={elapsed_seconds:.1f} score={score} error={error}",flush=True)
                if error is None:
                    valid += 1
                    paper_valid = True
                    break
                invalid += 1
            consecutive_failures = 0 if paper_valid else consecutive_failures + 1
            if time.monotonic() >= next_checkpoint:
                db=checkpoint(db)
                next_checkpoint=time.monotonic()+30*60
            if consecutive_failures >= 3:
                raise RuntimeError("Three papers produced no valid rubric choices; stopping early")
        print(f"Completed: {valid} valid papers, {invalid} invalid attempts",flush=True)
        if processed and valid == 0:
            raise RuntimeError("No valid rubric choices saved")
    finally:
        db.close()


if __name__ == "__main__":
    main()
