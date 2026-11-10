#!/usr/bin/env python3
"""Run version 3 against journal articles with abstracts."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from huggingface_hub import hf_hub_download
from llama_cpp import Llama

from rubric_v3 import open_db, prompt, save_response

try:
    from scripts.library_storage import restore, journal_paths
except ModuleNotFoundError:
    from library_storage import restore, journal_paths

ROOT = Path(__file__).resolve().parents[1]
MODEL_KEY = os.environ.get('RUBRIC_MODEL', 'qwen3_8b')
MODELS = {
    'qwen3_8b': ('Qwen/Qwen3-8B-GGUF', 'Qwen3-8B-Q4_K_M.gguf', 'Qwen3-8B Q4_K_M'),
    'openscholar_8b': ('QuantFactory/Llama-3.1_OpenScholar-8B-GGUF',
                       'Llama-3.1_OpenScholar-8B.Q4_K_M.gguf', 'OpenScholar-8B Q4_K_M'),
}
MODEL_REPO, MODEL_FILE, MODEL_LABEL = MODELS[MODEL_KEY]
DB_PATH = ROOT / 'abstract_evaluation' / f'rubric_v3_{MODEL_KEY}.sqlite'


def candidates(db):
    completed = {row[0] for row in db.execute(
        'SELECT work_id FROM evaluations WHERE validation_error IS NULL AND version_rubrica=3')}
    pending = {}
    journals = list(json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))['journals'])
    for journal in journals:
        path = restore(ROOT / 'library' / f'{journal}.sqlite')
        if not path.exists():
            continue
        source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            for work_id, abstract, day in source.execute('''SELECT id,abstract,publication_date FROM papers
                    WHERE type='article' AND TRIM(COALESCE(abstract,''))<>''
                    AND COALESCE(json_extract(raw_json,'$.is_retracted'),0)=0
                    ORDER BY publication_date DESC,id DESC'''):
                if work_id not in completed and work_id not in pending:
                    pending[work_id] = (work_id, abstract, day or '', journal)
        finally:
            source.close()
    priority = ROOT / 'abstract_evaluation' / ('priority_mod0.json' if MODEL_KEY == 'qwen3_8b' else 'priority_mod1.json')
    requests = json.loads(priority.read_text(encoding='utf-8')) if priority.exists() else []
    priority_ids = list(dict.fromkeys(item['work_id'].removeprefix('https://openalex.org/')
                                  for item in requests if isinstance(item, dict) and isinstance(item.get('work_id'), str)))
    seen = set()
    for work_id in priority_ids:
        if work_id in pending:
            seen.add(work_id)
            yield pending[work_id]
    by_day = {}
    for paper in pending.values():
        if paper[0] not in seen:
            by_day.setdefault(paper[2][:10], {}).setdefault(paper[3], []).append(paper)
    for day in sorted(by_day, reverse=True):
        queues = by_day[day]
        while any(queues.values()):
            for journal in journals:
                if queues.get(journal):
                    yield queues[journal].pop(0)


def checkpoint(db):
    db.close()
    relative = DB_PATH.relative_to(ROOT).as_posix()
    subprocess.run(['git', 'add', '--', relative], cwd=ROOT, check=True)
    if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=ROOT).returncode == 0:
        return open_db(DB_PATH)
    subprocess.run(['git', '-c', 'user.name=github-actions[bot]', '-c',
                    'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                    'commit', '-m', f'Guardar evaluaciones de rúbrica v3 {MODEL_KEY}'], cwd=ROOT, check=True)
    for attempt in range(3):
        pull = subprocess.run(['git', 'pull', '--rebase', 'origin', 'main'], cwd=ROOT)
        if pull.returncode:
            subprocess.run(['git', 'rebase', '--abort'], cwd=ROOT)
            raise RuntimeError('Checkpoint rebase failed; preserve artifact')
        if subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=ROOT).returncode == 0:
            return open_db(DB_PATH)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError('Could not publish SQLite checkpoint')


def main():
    deadline = int(os.environ.get('IMPORTANCE_DEADLINE_EPOCH', str(int(time.time()) + 345 * 60)))
    db = open_db(DB_PATH)
    try:
        papers = list(candidates(db))
        print(f'{MODEL_LABEL}: {len(papers)} pending for rubric v3', flush=True)
        model_file = hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE)
        model = Llama(model_path=model_file, n_ctx=8192, n_threads=min(4, os.cpu_count() or 1),
                      n_batch=256, verbose=False)
        next_checkpoint = time.monotonic() + 30 * 60
        valid = 0
        for work_id, abstract, day, journal in papers:
            if time.time() >= deadline:
                break
            if len(model.tokenize(abstract.encode())) > 5700:
                print(f'Skip overlong abstract: {work_id}', flush=True)
                continue
            started = time.monotonic()
            try:
                output = model.create_chat_completion(
                    messages=[{'role': 'user', 'content': prompt(abstract)}],
                    temperature=0.2, top_p=0.8, seed=17, max_tokens=1500)
                raw = output['choices'][0]['message']['content'] or ''
                usage = output.get('usage') or {}
            except Exception as exc:
                raw, usage = f'INFERENCE_ERROR: {exc}', {}
            row_id = save_response(db, work_id=work_id, model=MODEL_LABEL, abstract=abstract,
                                   raw=raw, latency_ms=int((time.monotonic() - started) * 1000),
                                   input_tokens=usage.get('prompt_tokens'),
                                   output_tokens=usage.get('completion_tokens'))
            score, error = db.execute('SELECT puntuacion,validation_error FROM evaluations WHERE id=?',
                                      (row_id,)).fetchone()
            print(f'{work_id} {journal}: {score or "unscored"} {error or ""}', flush=True)
            valid += error is None
            if valid == 1 or time.monotonic() >= next_checkpoint:
                db = checkpoint(db)
                next_checkpoint = time.monotonic() + 30 * 60
        print(f'Valid rubric v3 responses: {valid}', flush=True)
    finally:
        db.close()


if __name__ == '__main__':
    main()
