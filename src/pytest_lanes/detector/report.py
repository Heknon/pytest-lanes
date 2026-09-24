"""The detector's output: a terminal section and an optional JSON file."""
from __future__ import annotations

import json

_LABEL = {"unsafe": "UNSAFE", "check": "CHECK", "ok": "OK"}


def write_terminal(tr, report: dict) -> None:
    tr.write_sep("=", "pytest-lanes shared-state report")
    findings = report["findings"]
    unsafe = [f for f in findings if f["severity"] == "unsafe"]
    if not unsafe:
        tr.write_line(f"{report['tests']} tests, {report['paths']} paths watched: nothing unsafe found.")
    for f in findings:
        example = f["examples"][0] if f["examples"] else ""
        more = f" (+{f['tests'] - 1} more)" if f["tests"] > 1 else ""
        tr.write_line(f"{_LABEL[f['severity']]:<7}{f['kind']:<10}{f['path']}   {example}{more}",
                      red=f["severity"] == "unsafe", yellow=f["severity"] == "check")
    if report["truncated"]:
        tr.write_line("Snapshots were truncated at lanes_detect_max_nodes; some state was not watched.",
                      yellow=True)
    shown = {f["kind"] for f in findings}
    for kind, note in report["notes"].items():
        if kind in shown:
            tr.write_line(f"{kind}: {note}")


def write_json(path: str, report: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
