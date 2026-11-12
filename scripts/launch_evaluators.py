"""Dispatch idle evaluation workflows in this repository."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
WORKFLOWS = [
    "openalex-abstract-importance-parallel-qwen3_8b.yml",
    *(f"openalex-abstract-importance-parallel-qwen3_8b-{day}.yml" for day in
      ("tuesday", "wednesday", "thursday", "friday", "weekend")),
    *(f"openalex-pdf-review-qwen3-rag-8b-{day}.yml" for day in
      ("monday", "tuesday", "wednesday", "thursday", "friday", "weekend")),
    "openalex-phase3-qwen3-8b.yml",
    "openalex-phase4-selection.yml",
]


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response) if method == "GET" else None


def main():
    failed = []
    for workflow in WORKFLOWS:
        path = f"/actions/workflows/{urllib.parse.quote(workflow)}/runs"
        try:
            active = []
            for status in ("in_progress", "queued", "waiting", "requested", "pending"):
                result = api(f"{path}?status={status}&per_page=1")
                active.extend(result["workflow_runs"])
            if active:
                print(f"SKIP {workflow}: active run {active[0]['id']}", flush=True)
                continue
            api(f"/actions/workflows/{urllib.parse.quote(workflow)}/dispatches",
                method="POST", body={"ref": "main"})
            print(f"START {workflow}", flush=True)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"ERROR {workflow}: {exc}", flush=True)
            failed.append(workflow)
    if failed:
        raise SystemExit(f"Could not process: {', '.join(failed)}")


if __name__ == "__main__":
    main()
