"""The detector's output: a terminal section and an optional JSON file."""
from __future__ import annotations

import json

_LABEL = {"unsafe": "UNSAFE", "check": "CHECK", "ok": "OK"}


def write_terminal(tr, report: dict, json_path=None) -> None:
    tr.write_sep("=", "pytest-lanes shared-state report")
    findings = report["findings"]
    unsafe = [f for f in findings if f["severity"] == "unsafe"]
    if not unsafe:
        tr.write_line(f"{report['tests']} tests, {report['paths']} paths watched: nothing unsafe found.")
    listed = [f for f in findings if f["severity"] != "ok"]
    if unsafe:
        tr.write_line(f"{len(unsafe)} unsafe finding(s) in {len(report['unsafe_tests'])} of "
                      f"{report['tests']} tests (all listed as unsafe_tests in the JSON report):",
                      red=True)
    for f in listed:
        example = f["examples"][0] if f["examples"] else ""
        more = f" (+{f['tests'] - 1} more)" if f["tests"] > 1 else ""
        tr.write_line(f"{_LABEL[f['severity']]:<7}{f['kind']:<10}{f['path']}   {example}{more}",
                      red=f["severity"] == "unsafe", yellow=f["severity"] == "check")
    if report["truncated"]:
        tr.write_line("Snapshots were truncated at lanes_detect_max_nodes; some state was not watched.",
                      yellow=True)
    ok = len(findings) - len(listed)
    if ok:
        where = "listed in the JSON report" if json_path else "add --lanes-detect-report=PATH to list them as JSON"
        tr.write_line(f"{ok} set-once (caches set once, then stable): {where}")
    shown = {f["kind"] for f in listed}
    for kind, note in report["notes"].items():
        if kind in shown:
            tr.write_line(f"{kind}: {note}")


def write_json(path: str, report: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
