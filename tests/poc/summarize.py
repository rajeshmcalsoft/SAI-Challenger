#!/usr/bin/env python3
"""
summarize.py - turn pytest JUnit XML reports into a pass/fail/xfail summary.

INPUT:
    Any number of JUnit XML files written by run_poc.sh (named
    <target>-<stamp>.xml, for example saivpp-1016-1130.xml), or folders
    holding them. Several runs of the same target are fine.

    With no argument it reads *.xml from poc/reports and
    poc_shubham/reports (whichever exist), relative to the folder you
    run it from.

TARGET PROFILES:
    xfail reasons come from the target profile, targets/<target>.json
    (looked up in --profiles DIR, then next to this script, then in the
    current folder). If a profile has no entry for an xfail test, the
    reason written in the XML is used and the test is flagged as
    "not in profile".

    Profile format read by this script (extra keys are ignored):

        {
          "xfail": { "<test name>": "<reason>", ... },
          "scope_gaps": [ "<item not in scope>", ... ]
        }

    "xfail" may also be a list of {"test": "...", "reason": "..."}.
    A test name may be the bare name or a full pytest node id.

OUTPUT:
    One table with a column per target (latest run of each target), so
    saivs and saivpp appear side by side, with time per test. Below it:
    totals, gaps with their xfail reasons, profile checks (new gaps,
    stale entries), run-to-run consistency, and failure details.

    --all-runs     one column per run instead of the latest per target
    --runs N       use only the newest N runs of each target
    --md FILE      Markdown summary
    --json FILE    full data, including untruncated errors
    --html FILE    compact HTML summary: filter buttons per target, counts
                   per test file, report names, and a last section listing
                   every failure, xfail and gap

Examples:
    python3 summarize.py poc_shubham/reports/saivs-1016-0930.xml \\
                         poc_shubham/reports/saivpp-1016-1130.xml
    python3 summarize.py poc_shubham/reports/ --runs 2 --all-runs
    python3 summarize.py reports/*.xml --profiles targets --md summary.md
    python3 summarize.py --html summary.html --md summary.md --json summary.json

Exit codes:
    0 = no failures anywhere
    1 = at least one failed test (or strict XPASS) in any report
    2 = no input found
"""

import argparse
import glob
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

DEFAULT_DIRS = [
    os.path.join("poc", "reports"),
    os.path.join("poc_shubham", "reports"),
]
# Longest name first: "saivs-thrift-..." must not be read as plain "saivs".
KNOWN_TARGETS = ("saivs-thrift", "saivpp", "saivs")
TARGET_ORDER = {"saivs": 0, "saivs-thrift": 1, "saivpp": 2}
MAX_ERR = 170

# Worst outcome wins when pytest writes one test twice (a test plus a
# setup/teardown error, for example).
RANK = {"passed": 0, "xfail": 1, "skipped": 2, "xpass": 3, "failed": 4}
LABEL = {
    "passed": "PASS",
    "failed": "FAIL",
    "xfail": "XFAIL",
    "xpass": "XPASS",
    "skipped": "SKIP",
}
COUNT_KEYS = ("passed", "failed", "xfail", "xpass", "skipped")
PROFILE_XFAIL_KEYS = ("xfail", "xfails", "expected_gaps", "gaps")
PROFILE_SCOPE_KEYS = ("scope_gaps", "not_in_scope", "out_of_scope")


# --------------------------------------------------------------------------
# Reading reports
# --------------------------------------------------------------------------

def split_classname(classname):
    """Return (module file, class path) from a pytest classname."""
    parts = classname.split(".") if classname else []

    for i, part in enumerate(parts):
        if part.startswith("test_"):
            return part + ".py", ".".join(parts[i + 1:])

    return (parts[-1] + ".py" if parts else "unknown"), ""


def short_error(message):
    """Return one readable line from a long pytest failure message."""
    first = (message or "").strip().splitlines()[0] if message else ""

    fields = re.findall(
        r"SAI_ACL_ENTRY_ATTR_FIELD_(\w+?)(?=['\",\s])",
        message or "",
    )
    fields = list(dict.fromkeys(fields))

    if "Failed to create ACL entry" in first and fields:
        first = (
            "Failed to create ACL entry "
            f"(fields: {', '.join(fields)}) -> status FAILURE"
        )

    # SAI status codes are useful when diagnosing remove() failures.
    match = re.search(
        r"remove\((\S+?)\)\s*-->.*?(SAI_STATUS_\w+)",
        message or "",
    )
    if match:
        first = f"remove({match.group(1)}) --> {match.group(2)}"

    first = " ".join(first.split())

    if len(first) > MAX_ERR:
        first = first[: MAX_ERR - 3] + "..."

    return first


def classify(testcase):
    """Return (outcome, message) for one <testcase> element."""
    bad = testcase.find("failure")
    if bad is None:
        bad = testcase.find("error")

    if bad is not None:
        message = bad.get("message", "") or (bad.text or "")
        if "XPASS(strict)" in message:
            return "xpass", message
        return "failed", message

    skipped = testcase.find("skipped")
    if skipped is not None:
        message = skipped.get("message", "") or (skipped.text or "")
        if "xfail" in skipped.get("type", "").lower():
            return "xfail", message
        return "skipped", message

    return "passed", ""


def detect_target(path):
    """Find the target name in a report file name (saivs, saivpp, ...)."""
    base = os.path.basename(path).lower().replace("_", "-")

    for name in KNOWN_TARGETS:
        if name in base:
            return name

    match = re.match(r"([a-z0-9]+(?:-[a-z]+)?)", base)
    return match.group(1) if match else "?"


def parse_report(path):
    """Parse one JUnit XML file into a run record with one entry per test."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = {}
    timestamp = ""

    for suite in suites:
        timestamp = timestamp or suite.get("timestamp", "")

        for testcase in suite.iter("testcase"):
            module, cls = split_classname(testcase.get("classname", ""))
            name = testcase.get("name", "?")
            shown = f"{cls}::{name}" if cls else name
            key = f"{module}::{shown}"
            outcome, message = classify(testcase)

            try:
                seconds = float(testcase.get("time") or 0)
            except ValueError:
                seconds = 0.0

            entry = tests.get(key)
            if entry is None:
                tests[key] = {
                    "key": key,
                    "file": module,
                    "name": shown,
                    "base": name.split("[")[0],
                    "outcome": outcome,
                    "time": seconds,
                    "message": message,
                }
                continue

            # Same test written twice: keep the worst outcome, add the time.
            entry["time"] += seconds
            if RANK[outcome] > RANK[entry["outcome"]]:
                entry["outcome"] = outcome
                entry["message"] = message

    return {
        "report": os.path.basename(path),
        "path": path,
        "target": detect_target(path),
        "timestamp": timestamp,
        "tests": tests,
    }


def count(run):
    """Count outcomes in one run."""
    result = dict.fromkeys(COUNT_KEYS, 0)

    for test in run["tests"].values():
        result[test["outcome"]] += 1

    return result


# --------------------------------------------------------------------------
# Target profiles
# --------------------------------------------------------------------------

def _reason_text(value):
    if isinstance(value, dict):
        value = value.get("reason", "")
    return str(value or "").strip()


def _bare_name(name):
    """tests/poc/test_x.py::test_y[param] -> test_y[param]

    The parameter stays: test_y[0] and test_y[4095] are different gaps. A
    profile entry without a parameter still matches every parameter."""
    return str(name).split("::")[-1].strip()


def parse_profile(data):
    """Pull xfail reasons and scope gaps out of a profile dictionary."""
    xfail = {}
    scope = []

    for key in PROFILE_XFAIL_KEYS:
        value = data.get(key)
        if isinstance(value, dict):
            for name, reason in value.items():
                xfail[_bare_name(name)] = _reason_text(reason)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    name = item.get("test") or item.get("name") or item.get("id")
                    if name:
                        xfail[_bare_name(name)] = _reason_text(item)
                elif isinstance(item, str):
                    xfail[_bare_name(item)] = ""

    for key in PROFILE_SCOPE_KEYS:
        for item in data.get(key) or []:
            if isinstance(item, dict):
                label = item.get("item") or item.get("name") or item.get("test") or "?"
                reason = _reason_text(item)
                scope.append(f"{label}: {reason}" if reason else str(label))
            else:
                scope.append(str(item))

    return {"xfail": xfail, "scope_gaps": scope}


def load_profiles(targets, profile_dirs):
    """Load targets/<target>.json for every target seen in the reports."""
    profiles = {}

    for target in targets:
        profiles[target] = None

        for directory in profile_dirs:
            path = os.path.join(directory, f"{target}.json")
            if not os.path.isfile(path):
                continue

            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                profile = parse_profile(data if isinstance(data, dict) else {})
                profile["path"] = path
                profiles[target] = profile
            except (OSError, ValueError) as error:
                print(f"Ignoring profile {path}: {error}", file=sys.stderr)

            break

    return profiles


def declared_reason(profile, test):
    """Return the profile's reason for a test, or None if not declared."""
    if not profile:
        return None

    xfail = profile["xfail"]

    for name in (test["name"], test["name"].split("::")[-1], test["base"]):
        if name in xfail:
            return xfail[name]

    return None


# --------------------------------------------------------------------------
# Building the comparison
# --------------------------------------------------------------------------

def run_sort_key(run):
    return (run["timestamp"], run["path"])


def build_model(runs, all_runs, profiles):
    """Arrange runs into columns, rows, gaps, checks and consistency data."""
    by_target = {}
    for run in runs:
        by_target.setdefault(run["target"], []).append(run)

    for target in by_target:
        by_target[target].sort(key=run_sort_key)

    targets = sorted(by_target, key=lambda t: (TARGET_ORDER.get(t, 9), t))

    columns = []
    for target in targets:
        series = by_target[target]
        for run in (series if all_runs else series[-1:]):
            index = series.index(run) + 1
            columns.append(
                {
                    "label": f"{target}#{index}" if all_runs else target,
                    "target": target,
                    "run": run,
                    "totals": count(run),
                }
            )

    # Rows: grouped by test file, tests in first-seen order.
    seen = {}
    for column in columns:
        for key, test in column["run"]["tests"].items():
            seen.setdefault(key, test)

    rows = {}
    for key, test in seen.items():
        rows.setdefault(test["file"], []).append(key)

    row_groups = []
    for filename in sorted(rows):
        entries = []
        for key in rows[filename]:
            entries.append(
                {
                    "key": key,
                    "name": seen[key]["name"],
                    "cells": [c["run"]["tests"].get(key) for c in columns],
                }
            )
        row_groups.append({"file": filename, "tests": entries})

    # Gaps and profile checks use the latest run of every target.
    gaps, new_gaps, stale, declared_failed, no_profile = [], [], [], [], []

    for target in targets:
        latest = by_target[target][-1]
        profile = profiles.get(target)
        saw_xfail = False

        for test in latest["tests"].values():
            declared = declared_reason(profile, test)

            if test["outcome"] == "xfail":
                saw_xfail = True
                if declared is None:
                    new_gaps.append((target, test["name"]))
                    reason, source = test["message"], "report"
                else:
                    reason, source = declared or test["message"], "profile"
                gaps.append(
                    {
                        "target": target,
                        "test": test["name"],
                        "reason": reason,
                        "source": source,
                    }
                )
            elif declared is not None and test["outcome"] in ("passed", "xpass"):
                stale.append((target, test["name"]))
            elif declared is not None and test["outcome"] == "failed":
                declared_failed.append((target, test["name"]))
                # Still a declared gap: show its reason in the Gaps table.
                gaps.append(
                    {
                        "target": target,
                        "test": test["name"],
                        "reason": declared or test["message"],
                        "source": "profile, test FAILED (no xfail marker)",
                    }
                )

        if saw_xfail and not profile:
            no_profile.append(target)

        if profile:
            for item in profile["scope_gaps"]:
                gaps.append(
                    {
                        "target": target,
                        "test": "(scope)",
                        "reason": item,
                        "source": "profile",
                    }
                )

    # Run-to-run consistency per target.
    flaky, stable = [], {}
    for target in targets:
        series = by_target[target]
        if len(series) < 2:
            continue

        keys = []
        for run in series:
            keys += [k for k in run["tests"] if k not in keys]

        unstable = 0
        for key in keys:
            results = [
                run["tests"][key]["outcome"] if key in run["tests"] else None
                for run in series
            ]
            # A run that did not include the test says nothing about it, so
            # only runs that did execute it are compared.
            if len({r for r in results if r is not None}) > 1:
                unstable += 1
                flaky.append(
                    {
                        "target": target,
                        "test": seen.get(key, {}).get("name", key),
                        "outcomes": results,
                    }
                )
        stable[target] = (len(series), unstable)

    failures = []
    for column in columns:
        for test in column["run"]["tests"].values():
            if test["outcome"] in ("failed", "xpass"):
                failures.append(
                    {
                        "column": column["label"],
                        "report": column["run"]["report"],
                        "test": test["key"],
                        "error": short_error(test["message"]),
                        "full_error": test["message"],
                    }
                )

    return {
        "columns": columns,
        "row_groups": row_groups,
        "gaps": gaps,
        "new_gaps": new_gaps,
        "stale": stale,
        "declared_failed": declared_failed,
        "no_profile": no_profile,
        "flaky": flaky,
        "stable": stable,
        "failures": failures,
        "profiles": profiles,
        "by_target": by_target,
    }


def cell_label(test):
    return "-" if test is None else LABEL[test["outcome"]]


def cell_text(test):
    if test is None:
        return "-"
    return f"{LABEL[test['outcome']]:<6}{test['time']:>7.2f}s"


def totals_text(totals):
    parts = [f"{totals['passed']} passed", f"{totals['failed']} failed"]
    parts.append(f"{totals['xfail']} xfail")
    if totals["xpass"]:
        parts.append(f"{totals['xpass']} xpass")
    parts.append(f"{totals['skipped']} skipped")
    return ", ".join(parts)


def check_lines(model):
    """Plain-text lines describing profile checks and consistency."""
    lines = []

    for target in model["no_profile"]:
        lines.append(
            f"{target}: xfail tests found but no targets/{target}.json "
            "was found, so reasons come from the report."
        )
    for target, name in model["new_gaps"]:
        lines.append(
            f"NEW GAP {target}: {name} is xfail but not in the "
            f"{target} profile. Add it to targets/{target}.json."
        )
    for target, name in model["declared_failed"]:
        lines.append(
            f"{target}: {name} is declared as a gap but FAILED "
            "(not xfail). The xfail marker is not being applied."
        )
    for target, name in model["stale"]:
        lines.append(
            f"STALE {target}: {name} is declared as a gap but passed. "
            "Remove it from the profile if the gap is fixed."
        )

    return lines


def consistency_lines(model):
    lines = []

    for target, (runs, unstable) in model["stable"].items():
        if unstable == 0:
            lines.append(f"{target}: same result in all {runs} runs.")
        else:
            lines.append(
                f"{target}: {unstable} test(s) differ across {runs} runs."
            )

    for item in model["flaky"]:
        shown = ", ".join(
            LABEL.get(o, "-") if o else "-" for o in item["outcomes"]
        )
        lines.append(f"  DIFFERS {item['target']}: {item['test']}  [{shown}]")

    return lines


# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------

def render(model):
    out = []
    columns = model["columns"]

    out.append("Reports used:")
    for column in columns:
        out.append(
            f"  {column['label']:<10} {column['run']['report']}  "
            f"-> {totals_text(column['totals'])}"
        )
    out.append("")

    name_width = max(
        [len("Test")]
        + [
            len(test["name"]) + 2
            for group in model["row_groups"]
            for test in group["tests"]
        ]
    )
    cell_width = max([14] + [len(c["label"]) for c in columns])

    header = f"{'Test':<{name_width}}" + "".join(
        f"  {c['label']:<{cell_width}}" for c in columns
    )
    out.append(header)
    out.append("-" * len(header))

    for group in model["row_groups"]:
        out.append(group["file"])
        for test in group["tests"]:
            line = f"{'  ' + test['name']:<{name_width}}"
            line += "".join(
                f"  {cell_text(cell):<{cell_width}}" for cell in test["cells"]
            )
            out.append(line)

    out.append("")

    out.append("Gaps (xfail reasons)")
    out.append("-" * 20)
    if model["gaps"]:
        for gap in model["gaps"]:
            if gap["source"] == "profile":
                tag = ""
            elif gap["source"].startswith("profile"):
                tag = "  [declared in profile; test FAILED, no xfail marker]"
            else:
                tag = "  [not in profile]"
            out.append(f"  {gap['target']:<8} {gap['test']}")
            out.append(f"           {gap['reason'] or '(no reason given)'}{tag}")
    else:
        out.append("  none")
    out.append("")

    checks = check_lines(model)
    if checks:
        out.append("Profile checks")
        out.append("-" * 14)
        out += [f"  {line}" for line in checks]
        out.append("")

    consistency = consistency_lines(model)
    if consistency:
        out.append("Run consistency")
        out.append("-" * 15)
        out += [f"  {line}" for line in consistency]
        out.append("")

    if model["failures"]:
        out.append("Failures")
        out.append("-" * 8)
        for failure in model["failures"]:
            out.append(f"  FAIL {failure['column']}: {failure['test']}")
            out.append(f"       {failure['error']}")
        out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def md_cell(test):
    if test is None:
        return "-"
    return f"{LABEL[test['outcome']]} ({test['time']:.2f}s)"


def render_md(model):
    out = ["# Test summary", "", "## Reports", ""]
    columns = model["columns"]

    out += ["| Column | Report | Result |", "|---|---|---|"]
    for column in columns:
        out.append(
            f"| {column['label']} | {column['run']['report']} | "
            f"{totals_text(column['totals'])} |"
        )

    out += ["", "## Results side by side", ""]
    out.append("| Test | " + " | ".join(c["label"] for c in columns) + " |")
    out.append("|---|" + "---|" * len(columns))

    for group in model["row_groups"]:
        out.append(f"| **{group['file']}** |" + " |" * len(columns))
        for test in group["tests"]:
            cells = " | ".join(md_cell(c) for c in test["cells"])
            out.append(f"| {test['name']} | {cells} |")

    out += ["", "## Gaps (xfail reasons)", ""]
    if model["gaps"]:
        out += ["| Target | Test | Reason | Source |", "|---|---|---|---|"]
        for gap in model["gaps"]:
            reason = (gap["reason"] or "(no reason given)").replace("|", "/")
            out.append(
                f"| {gap['target']} | {gap['test']} | {reason} | {gap['source']} |"
            )
    else:
        out.append("None.")

    checks = check_lines(model)
    if checks:
        out += ["", "## Profile checks", ""] + [f"- {line}" for line in checks]

    consistency = consistency_lines(model)
    if consistency:
        out += ["", "## Run consistency", ""]
        out += [f"- {line.strip()}" for line in consistency]

    if model["failures"]:
        out += ["", "## Failures", ""]
        for failure in model["failures"]:
            out.append(
                f"- `{failure['column']}` `{failure['test']}` - {failure['error']}"
            )

    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

# SUMMARY_HTML_V2
# Compact HTML summary: filter chips per target, one counts table per test
# file, report names on the target cards, and a last section that lists every
# failure, xfail and gap. No horizontal scrolling at any width.
#
# Combined view: when several runs of one target are shown (--all-runs), a
# test is taken from the newest run that really executed it (a skip never
# hides a pass or fail from another run). So the counts here can differ from
# the per-run totals printed in the console.

HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAI-Challenger | Test Summary</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; overflow-x: hidden; }
  body { padding: 14px; background: #f6f7fb; color: #1f2937;
         font: 13px/1.4 system-ui, Arial, sans-serif; }
  main { max-width: 760px; margin: 0 auto; }
  [hidden] { display: none !important; }
  header { display: flex; flex-wrap: wrap; align-items: center;
           justify-content: space-between; gap: 8px; margin-bottom: 10px; }
  h1 { margin: 0; font-size: 17px; color: #172554; }
  .headline { margin: 2px 0 0; color: #64748b; font-size: 12px; }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; }
  .chip { border: 1px solid #cbd5e1; background: #fff; color: #334155;
          border-radius: 999px; padding: 3px 10px; font: inherit;
          font-size: 12px; cursor: pointer; }
  .chip.on { background: #1e3a8a; border-color: #1e3a8a; color: #fff; }
  .cards { display: grid; gap: 8px; margin-bottom: 10px;
           grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
  .card, section { background: #fff; border: 1px solid #e5eaf1;
                   border-radius: 8px; padding: 9px 11px; min-width: 0; }
  section { margin-bottom: 10px; }
  .card h2 { margin: 0 0 3px; font-size: 13px; display: flex;
             justify-content: space-between; align-items: center; gap: 6px; }
  .big { font-size: 20px; font-weight: 700; }
  .big small { font-size: 11px; font-weight: 400; color: #64748b; }
  .sm { font-size: 11px; color: #64748b; }
  .rep { margin-top: 4px; font: 10.5px/1.35 ui-monospace, Consolas, monospace;
         color: #64748b; overflow-wrap: anywhere; }
  .rep summary { cursor: pointer; }
  .pill { border-radius: 4px; padding: 1px 6px; font-size: 10px;
          font-weight: 700; }
  .ok { color: #166534; background: #dcfce7; }
  .bad { color: #991b1b; background: #fee2e2; }
  .warn { color: #92400e; background: #fef3c7; }
  .none { color: #475569; background: #e2e8f0; }
  h3 { margin: 0 0 6px; font-size: 13px; color: #243b53; }
  table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  th, td { text-align: left; padding: 5px 6px; vertical-align: top;
           border-bottom: 1px solid #eef1f5; overflow-wrap: anywhere; }
  th { font-size: 11px; color: #64748b; font-weight: 600; }
  th:first-child { width: 40%; }
  tr:last-child td { border-bottom: 0; }
  td.file { font: 11.5px ui-monospace, Consolas, monospace; }
  b.p, b.f, b.x, b.s { display: inline-block; min-width: 1.7em;
        text-align: center; border-radius: 4px; padding: 0 4px;
        margin: 0 3px 2px 0; font-size: 11px; }
  .p { background: #dcfce7; color: #166534; }
  .f { background: #fee2e2; color: #991b1b; }
  .x { background: #fef3c7; color: #92400e; }
  .s { background: #e2e8f0; color: #475569; }
  .legend { margin: 6px 0 0; font-size: 11px; color: #64748b; }
  .issue { border-left: 3px solid #94a3b8; background: #f8fafc;
           padding: 5px 8px; margin: 6px 0; overflow-wrap: anywhere; }
  .issue.FAIL, .issue.XPASS { border-color: #dc2626; background: #fff7f7; }
  .issue.XFAIL { border-color: #d97706; background: #fffbeb; }
  .tag { border-radius: 4px; padding: 0 5px; font-size: 10px;
         font-weight: 700; background: #e2e8f0; color: #334155; }
  .issue.FAIL .tag, .issue.XPASS .tag { background: #fee2e2; color: #991b1b; }
  .issue.XFAIL .tag { background: #fef3c7; color: #92400e; }
  .issue code { font: 11.5px ui-monospace, Consolas, monospace; }
  .issue p { margin: 2px 0 0; color: #475569; font-size: 12px; }
  .issue small { color: #94a3b8; font: 10.5px ui-monospace, Consolas, monospace; }
  footer { color: #94a3b8; font-size: 11px; text-align: right; }
</style>
</head>
<body>
<main>
"""

HTML_SCRIPT = """<script>
(function () {
  var chips = document.querySelectorAll('.chip');
  var els = document.querySelectorAll('[data-target]');
  var count = document.getElementById('n');
  var empty = document.getElementById('none');
  function show(t) {
    var n = 0;
    chips.forEach(function (c) { c.classList.toggle('on', c.dataset.f === t); });
    els.forEach(function (e) {
      var hide = t !== 'all' && e.dataset.target !== t;
      e.hidden = hide;
      if (!hide && e.classList.contains('issue')) { n += 1; }
    });
    if (count) { count.textContent = n; }
    if (empty) { empty.hidden = n > 0; }
  }
  chips.forEach(function (c) { c.onclick = function () { show(c.dataset.f); }; });
  show('all');
})();
</script>
"""

HTML_KNOWN_TARGETS = ("saivs", "saivs-thrift", "saivpp")


def _combine(model):
    """Per target: its runs and one entry per test (newest real result)."""
    combined = {}
    for column in model["columns"]:
        slot = combined.setdefault(column["target"], {"runs": [], "tests": {}})
        slot["runs"].append(column["run"])

    for slot in combined.values():
        slot["runs"].sort(key=run_sort_key)
        for run in slot["runs"]:                      # oldest -> newest
            for key, test in run["tests"].items():
                old = slot["tests"].get(key)
                if (old and test["outcome"] == "skipped"
                        and old["outcome"] != "skipped"):
                    continue
                slot["tests"][key] = dict(test, report=run["report"])
    return combined


def _tally(tests):
    result = dict.fromkeys(COUNT_KEYS, 0)
    for test in tests:
        result[test["outcome"]] += 1
    return result


def _chips(tally):
    """Small coloured numbers: pass, fail (incl. strict XPASS), xfail, skip."""
    bad = tally["failed"] + tally["xpass"]
    parts = []
    for number, css, tip in (
        (tally["passed"], "p", "pass"),
        (bad, "f", "fail"),
        (tally["xfail"], "x", "xfail"),
        (tally["skipped"], "s", "skip"),
    ):
        if number:
            parts.append(f'<b class="{css}" title="{tip}">{number}</b>')
    return "".join(parts) or "-"


def _status(tally):
    if tally["failed"] or tally["xpass"]:
        return "FAIL", "bad"
    if tally["xfail"]:
        return "GAPS", "warn"
    return "PASS", "ok"


def _issues(model, combined, order):
    """Every failure, strict XPASS, xfail and scope gap, worst first."""
    rank = {"FAIL": 0, "XPASS": 1, "XFAIL": 2, "GAP": 3}
    found = []

    for target in order:
        slot = combined.get(target)
        if not slot:
            continue
        profile = model["profiles"].get(target)

        for test in slot["tests"].values():
            outcome = test["outcome"]
            if outcome not in ("failed", "xpass", "xfail"):
                continue
            declared = declared_reason(profile, test)
            if outcome == "xfail":
                kind = "XFAIL"
                text = declared or short_error(test["message"]) or "no reason given"
            else:
                kind = "XPASS" if outcome == "xpass" else "FAIL"
                text = short_error(test["message"]) or "no message"
                if declared and outcome == "failed":
                    text += f" (declared gap: {declared})"
            found.append((rank[kind], order.index(target), kind, target,
                          test["name"], text, test["report"]))

        for item in (profile or {}).get("scope_gaps", []):
            found.append((rank["GAP"], order.index(target), "GAP", target,
                          "(out of scope)", item, ""))

    found.sort(key=lambda row: (row[0], row[1]))
    return found


def render_html(model):
    """Compact summary: cards, one counts table, issues section last."""
    esc = html.escape
    combined = _combine(model)
    order = list(HTML_KNOWN_TARGETS) + sorted(
        t for t in combined if t not in HTML_KNOWN_TARGETS)
    reported = [t for t in order if t in combined]
    tallies = {t: _tally(combined[t]["tests"].values()) for t in combined}
    failing = sum(c["failed"] + c["xpass"] for c in tallies.values())
    gaps = sum(c["xfail"] for c in tallies.values())
    issues = _issues(model, combined, order)

    out = [HTML_HEAD]
    out.append(
        "<header><div><h1>SAI-Challenger test summary</h1>"
        f'<p class="headline">{len(reported)} target(s) &middot; '
        f"{failing} failing &middot; {gaps} known gaps</p></div>"
        '<div class="chips">'
        + "".join(
            f'<button class="chip" data-f="{esc(t)}">'
            f'{"All" if t == "all" else esc(t)}</button>'
            for t in ["all"] + order)
        + "</div></header>"
    )

    # Target cards with their report files.
    out.append('<div class="cards">')
    for target in order:
        slot = combined.get(target)
        if slot is None:
            out.append(
                f'<article class="card" data-target="{esc(target)}">'
                f'<h2>{esc(target)} <span class="pill none">NO REPORT</span></h2>'
                '<div class="sm">No XML report included</div></article>')
            continue
        c = tallies[target]
        label, css = _status(c)
        names = [run["report"] for run in slot["runs"]]
        if len(names) == 1:
            rep = f'<div class="rep">{esc(names[0])}</div>'
        else:
            rep = (f'<details class="rep"><summary>{len(names)} reports'
                   "</summary>" + "<br>".join(esc(n) for n in names)
                   + "</details>")
        out.append(
            f'<article class="card" data-target="{esc(target)}">'
            f'<h2>{esc(target)} <span class="pill {css}">{label}</span></h2>'
            f'<div class="big">{c["passed"]}'
            f'<small> / {sum(c.values())} passed</small></div>'
            f'<div class="sm">{c["failed"] + c["xpass"]} fail &middot; '
            f'{c["xfail"]} xfail &middot; {c["skipped"]} skip</div>'
            f"{rep}</article>")
    out.append("</div>")

    # One row per test file, counts per target.
    out.append('<section><h3>Results by test file</h3><table><thead><tr>'
               "<th>Test file</th>")
    for target in reported:
        out.append(f'<th data-target="{esc(target)}">{esc(target)}</th>')
    out.append("</tr></thead><tbody>")
    for group in model["row_groups"]:
        out.append(f'<tr><td class="file">{esc(group["file"])}</td>')
        for target in reported:
            subset = [t for t in combined[target]["tests"].values()
                      if t["file"] == group["file"]]
            out.append(f'<td data-target="{esc(target)}">'
                       f"{_chips(_tally(subset)) if subset else '-'}</td>")
        out.append("</tr>")
    out.append(
        "</tbody></table>"
        '<p class="legend"><b class="p">n</b> pass '
        '<b class="f">n</b> fail <b class="x">n</b> xfail '
        '<b class="s">n</b> skip</p></section>')

    # Last section, only when there is something to show.
    if issues:
        out.append(
            '<section><h3>Failures, xfail and gaps '
            '(<span id="n">%d</span>)</h3>' % len(issues))
        for _, _, kind, target, name, text, report in issues:
            where = f" <small>{esc(report)}</small>" if report else ""
            out.append(
                f'<div class="issue {kind}" data-target="{esc(target)}">'
                f'<span class="tag">{kind}</span> <b>{esc(target)}</b> '
                f"<code>{esc(name)}</code><p>{esc(text)}{where}</p></div>")
        out.append('<p class="sm" id="none" hidden>Nothing for this target.</p>'
                   "</section>")

    out.append("<footer>Generated by summarize.py</footer></main>")
    out.append(HTML_SCRIPT)
    out.append("</body></html>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------

def model_to_json(model):
    return {
        "columns": [
            {
                "label": c["label"],
                "target": c["target"],
                "report": c["run"]["report"],
                "totals": c["totals"],
            }
            for c in model["columns"]
        ],
        "tests": [
            {
                "file": group["file"],
                "test": test["name"],
                "results": {
                    c["label"]: (
                        None
                        if cell is None
                        else {
                            "outcome": cell["outcome"],
                            "time": cell["time"],
                            "message": cell["message"],
                        }
                    )
                    for c, cell in zip(model["columns"], test["cells"])
                },
            }
            for group in model["row_groups"]
            for test in group["tests"]
        ],
        "gaps": model["gaps"],
        "new_gaps": [{"target": t, "test": n} for t, n in model["new_gaps"]],
        "stale": [{"target": t, "test": n} for t, n in model["stale"]],
        "declared_but_failed": [
            {"target": t, "test": n} for t, n in model["declared_failed"]
        ],
        "differs_across_runs": model["flaky"],
        "failures": model["failures"],
        "profiles": {
            t: (None if p is None else {"path": p["path"],
                "xfail": p["xfail"], "scope_gaps": p["scope_gaps"]})
            for t, p in model["profiles"].items()
        },
    }


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def collect(inputs):
    """Collect XML report paths from files, folders, globs or defaults."""
    paths = []

    for item in inputs or [d for d in DEFAULT_DIRS if os.path.isdir(d)]:
        if os.path.isdir(item):
            paths += sorted(glob.glob(os.path.join(item, "*.xml")))
        else:
            paths += sorted(glob.glob(item)) or [item]

    unique = list(dict.fromkeys(paths))
    return [path for path in unique if os.path.isfile(path)]


def profile_dirs(option):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dirs = [option] if option else []
    dirs += [
        os.path.join(script_dir, "targets"),
        "targets",
        os.path.join("poc", "targets"),
        os.path.join("tests", "poc", "targets"),
    ]
    return list(dict.fromkeys(dirs))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="XML files, globs or folders (default: {})".format(
            " + ".join(DEFAULT_DIRS)
        ),
    )
    parser.add_argument(
        "--profiles",
        metavar="DIR",
        help="folder holding <target>.json profiles (default: targets/)",
    )
    parser.add_argument(
        "--all-runs",
        action="store_true",
        help="one column per run instead of the latest run per target",
    )
    parser.add_argument(
        "--runs",
        type=int,
        metavar="N",
        help="use only the newest N runs of each target (for example 2)",
    )
    parser.add_argument("--md", help="also write a Markdown summary to FILE")
    parser.add_argument("--json", help="also write full data to FILE")
    parser.add_argument("--html", help="also write an HTML summary to FILE")

    args = parser.parse_args()
    paths = collect(args.inputs)

    if not paths:
        print(
            "No XML reports found. Pass file paths or a folder.",
            file=sys.stderr,
        )
        return 2

    runs = []
    for path in paths:
        try:
            runs.append(parse_report(path))
        except (ET.ParseError, OSError) as error:
            print(f"Skipping {path} (cannot read: {error})", file=sys.stderr)

    if not runs:
        print("No valid XML reports found.", file=sys.stderr)
        return 2

    if args.runs and args.runs > 0:
        kept = []
        for target in {run["target"] for run in runs}:
            series = sorted(
                (r for r in runs if r["target"] == target), key=run_sort_key
            )
            kept += series[-args.runs:]
        runs = kept

    targets = sorted({run["target"] for run in runs})
    profiles = load_profiles(targets, profile_dirs(args.profiles))
    model = build_model(runs, args.all_runs, profiles)

    print(render(model))

    if args.md:
        with open(args.md, "w", encoding="utf-8") as handle:
            handle.write(render_md(model))
        print(f"Markdown summary saved to: {args.md}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(model_to_json(model), handle, indent=2)
        print(f"JSON data saved to: {args.json}")

    if args.html:
        with open(args.html, "w", encoding="utf-8") as handle:
            handle.write(render_html(model))
        print(f"HTML report saved to: {args.html}")

    failed = any(
        count(run)["failed"] or count(run)["xpass"] for run in runs
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
