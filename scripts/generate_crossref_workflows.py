#!/usr/bin/env python3
"""Regenerate the forty journal workflows from config.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"
WORKFLOWS = ROOT / ".github" / "workflows"


def shifted_cron(value: str, minutes: int):
    minute, rest = value.split(" ", 1)
    shifted = int(minute) + minutes
    if shifted >= 60:
        raise ValueError("Shift crosses an hour boundary")
    return str(shifted) + " " + rest


def workflow(code: str, journal: dict, mode: str):
    path = f"library/{mode}/{code}.sqlite"
    cron = shifted_cron(journal["cron_utc"], 3 if mode == "crossref" else 7)
    secret = "CROSSREF_MAILTO" if mode == "crossref" else "CROSSMARK_MAILTO"
    return f"""name: {mode.capitalize()} {journal['name']} collector

on:
  schedule:
    - cron: '{cron}'
  workflow_dispatch:
  repository_dispatch:
    types: [run-{mode}-{code}-collector]

permissions:
  contents: write

concurrency:
  group: {mode}-{code}-collector
  cancel-in-progress: false

jobs:
  collect:
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    steps:
      - name: Checkout main
        uses: actions/checkout@v5
        with:
          ref: main
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: '3.12'

      - name: Record starting revision and deadline
        run: |
          echo "START_REV=$(git rev-parse HEAD)" >> "$GITHUB_ENV"
          echo "CROSSREF_DEADLINE_EPOCH=$(date -u -d '+24 minutes' +%s)" >> "$GITHUB_ENV"

      - name: Check collector invariants without API requests
        run: python -m unittest discover -s tests -p 'test_crossref_collectors.py' -v

      - name: Capture 370 publication days through yesterday UTC
        env:
          {secret}: ${{{{ secrets.{secret} }}}}
          CROSSREF_DEADLINE_EPOCH: ${{{{ env.CROSSREF_DEADLINE_EPOCH }}}}
        run: python -u scripts/collect_crossref_journal.py --journal {code} --mode {mode} --max-requests 100

      - name: Preserve capture as artifact
        if: always()
        uses: actions/upload-artifact@v6
        with:
          name: {mode}-{code}-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}
          path: {path}
          if-no-files-found: ignore

      - name: Publish saved data
        if: always()
        run: |
          if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then git rebase --abort; fi
          if [ ! -f {path} ]; then exit 0; fi
          git config user.name 'github-actions[bot]'
          git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
          git add -- {path}
          if git diff --cached --quiet; then exit 0; fi
          git commit -m 'Update {mode} {code} capture'
          for attempt in 1 2 3; do
            git fetch origin main
            if ! git diff --quiet "$START_REV" origin/main -- {path}; then
              echo 'Remote journal data changed; preserve artifact instead of overwriting it.' >&2
              exit 1
            fi
            git rebase origin/main || {{ git rebase --abort; exit 1; }}
            if git push origin HEAD:main; then exit 0; fi
            sleep "$attempt"
          done
          exit 1
"""


def main():
    journals = json.loads(CONFIG.read_text(encoding="utf-8"))["journals"]
    for code, journal in journals.items():
        if "crossref_issn" not in journal:
            raise ValueError(f"Missing Crossref ISSN for {code}")
        for mode in ("crossref", "crossmark"):
            (WORKFLOWS / f"{mode}-{code}-collector.yml").write_text(
                workflow(code, journal, mode), encoding="utf-8")


if __name__ == "__main__":
    main()
