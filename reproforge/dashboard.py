"""Loopback-only, read-only run and artifact-lineage dashboard."""

from __future__ import annotations

import html
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .spec import Plan


RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _records(root: Path) -> dict[str, dict]:
    result = {}
    directory = root / ".reproforge" / "runs"
    if not directory.is_dir():
        return result
    for path in directory.glob("*.json"):
        if not RUN_ID.fullmatch(path.stem) or path.is_symlink():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("run_id") == path.stem:
            result[path.stem] = record
    return result


def render(plan: Plan, selected: str | None = None, compare: str | None = None) -> str:
    records = _records(plan.root)
    if selected not in records:
        selected = next(iter(sorted(records, key=lambda key: str(records[key].get("created_at", "")), reverse=True)), None)
    current = records.get(selected, {})
    other = records.get(compare, {})
    items = []
    for run_id, record in sorted(records.items(), key=lambda pair: str(pair[1].get("created_at", "")), reverse=True):
        items.append(f'<li><a href="/?run={run_id}">{_esc(run_id[:12])}</a> '
                     f'<span class="badge">{_esc(record.get("status", "unknown"))}</span> '
                     f'{_esc(record.get("created_at", ""))}</li>')
    current_tasks = current.get("tasks") if isinstance(current.get("tasks"), list) else []
    other_tasks = other.get("tasks") if isinstance(other.get("tasks"), list) else []
    task_entries = {item["id"]: item for item in current_tasks
                    if isinstance(item, dict) and isinstance(item.get("id"), str)}
    other_entries = {item["id"]: item for item in other_tasks
                     if isinstance(item, dict) and isinstance(item.get("id"), str)}
    sections = []
    for task in plan.tasks:
        entry = task_entries.get(task.id, {})
        upstream = list(dict.fromkeys([*task.depends_on, *(item.task for item in task.inputs if item.task)]))
        ancestors = ", ".join(f'<a href="#task-{_esc(name)}">{_esc(name)}</a>' for name in upstream) or "none"
        outputs = entry.get("outputs", {}) if isinstance(entry.get("outputs"), dict) else {}
        artifacts = "".join(f'<li>{_esc(name)} <code>{_esc(ref.get("sha256", "?"))}</code></li>'
                            for name, ref in outputs.items() if isinstance(ref, dict)) or "<li>None recorded</li>"
        delta = ""
        if other:
            previous = other_entries.get(task.id, {})
            changed = previous.get("status") != entry.get("status") or previous.get("outputs") != outputs
            delta = f'<p class="badge">Compared with {_esc(compare[:12])}: {"changed" if changed else "unchanged"}</p>'
        sections.append(f'<section id="task-{_esc(task.id)}"><h3>{_esc(task.id)} '
                        f'<span class="badge">{_esc(entry.get("status", "not run"))}</span></h3>'
                        f'<p>Depends on: {ancestors}</p>{delta}<ul>{artifacts}</ul>'
                        f'<p>{_esc(entry.get("error", ""))}</p></section>')
    compare_options = "".join(f'<option value="{run_id}">{_esc(run_id[:12])}</option>'
                              for run_id in records if run_id != selected)
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>ReproForge runs</title><style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;"
            "padding:0 1rem;color:#172335;background:#f5f8fb}main{display:grid;grid-template-columns:18rem 1fr;gap:1.5rem}"
            "section,aside{background:white;padding:1rem 1.3rem;border-radius:.8rem;box-shadow:0 1px 4px #ccd}"
            "section{margin-bottom:1rem}code{font-size:.75rem;overflow-wrap:anywhere}.badge{background:#e5eef7;"
            "border-radius:.4rem;padding:.15rem .4rem}li{margin:.55rem 0}a{color:#1454a0}@media(max-width:700px)"
            "{main{display:block}aside{margin-bottom:1rem}}</style><h1>ReproForge</h1>"
            "<p>Read-only local run history and artifact lineage. Verification remains available in the CLI.</p>"
            f'<main><aside><h2>Runs</h2><ul>{"".join(items) or "<li>No runs yet</li>"}</ul></aside><article>'
            f'<h2>Run {_esc(selected or "none")}</h2><p>Status: {_esc(current.get("status", "not run"))}</p>'
            f'<form method="get"><input type="hidden" name="run" value="{_esc(selected or "")}">'
            f'<label>Compare with <select name="compare"><option value="">Select run</option>{compare_options}</select></label>'
            '<button type="submit">Compare</button></form>'
            f'{"".join(sections)}</article></main></html>')


def make_server(plan: Plan, port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path != "/":
                self.send_error(404)
                return
            query = parse_qs(url.query)
            chosen = query.get("run", [None])[0]
            compared = query.get("compare", [None])[0]
            if any(value is not None and not RUN_ID.fullmatch(value) for value in (chosen, compared)):
                self.send_error(400)
                return
            body = render(plan, chosen, compared).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.send_error(405)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
