#!/usr/bin/env python3
"""
summarize.py - turn pytest JUnit XML reports into a short pass/fail summary.

INPUT : one or more JUnit XML files (the --junitxml files made by `run saivpp ...`),
        or a directory holding them. With no argument it reads *.xml from poc/reports and
        poc_shubham/reports (whichever exist), relative to the folder you run it from
OUTPUT: a table printed on screen: per run -> per test file -> passed/failed/skipped,
        then one line per failure (test name + short error), then the report file name.
        Optional: --md FILE (markdown copy), --json FILE (full, untruncated data).

Examples:
  python3 summarize.py poc_shubham/reports/saivpp-1009-164211.xml
  python3 summarize.py poc/reports/                    # every xml in the folder
  python3 summarize.py                                 # default folders
  python3 summarize.py reports/*.xml --md summary.md --json summary.json

Exit code: 0 = no failures anywhere, 1 = at least one failure/error, 2 = no input found.
"""
import argparse
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

DEFAULT_DIRS = [os.path.join("poc", "reports"), os.path.join("poc_shubham", "reports")]
MAX_ERR = 170


def source_file(classname):
    """'ut.test_acl_ut.TestIngressACL' -> 'ut/test_acl_ut.py'"""
    parts = classname.split(".") if classname else ["unknown"]
    for i, p in enumerate(parts):
        if p.startswith("test"):
            return "/".join(parts[: i + 1]) + ".py"
    return "/".join(parts)


def short_error(message):
    """One readable line from a long pytest failure message."""
    first = (message or "").strip().splitlines()[0] if message else ""
    fields = re.findall(r"SAI_ACL_ENTRY_ATTR_FIELD_(\w+?)(?=['\",\s])", message or "")
    fields = [f for f in dict.fromkeys(fields)]          # unique, keep order
    if "Failed to create ACL entry" in first and fields:
        first = "Failed to create ACL entry (fields: %s) -> status FAILURE" % ", ".join(fields)
    # SAI status codes are the most useful part of remove() failures
    m = re.search(r"remove\((\S+?)\)\s*-->.*?(SAI_STATUS_\w+)", message or "")
    if m:
        first = "remove(%s) --> %s" % (m.group(1), m.group(2))
    first = " ".join(first.split())
    return first if len(first) <= MAX_ERR else first[: MAX_ERR - 3] + "..."


def parse_report(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    files = {}
    for suite in suites:
        for tc in suite.iter("testcase"):
            f = files.setdefault(source_file(tc.get("classname", "")),
                                 {"passed": 0, "failed": 0, "skipped": 0, "failures": []})
            bad = tc.find("failure")
            if bad is None:
                bad = tc.find("error")
            if bad is not None:
                f["failed"] += 1
                f["failures"].append({"test": tc.get("name", "?"),
                                      "error": short_error(bad.get("message", "")),
                                      "full_error": bad.get("message", "")})
            elif tc.find("skipped") is not None:
                f["skipped"] += 1
            else:
                f["passed"] += 1
    m = re.match(r"([A-Za-z0-9]+)", os.path.basename(path))
    return {"report": os.path.basename(path), "target": m.group(1) if m else "?", "files": files}


def totals(run):
    t = {"passed": 0, "failed": 0, "skipped": 0}
    for f in run["files"].values():
        for k in t:
            t[k] += f[k]
    return t


def render(runs):
    out = []
    for run in runs:
        t = totals(run)
        out.append("Report : %s   (target: %s)" % (run["report"], run["target"]))
        out.append("Total  : %d passed, %d failed, %d skipped" % (t["passed"], t["failed"], t["skipped"]))
        out.append("")
        out.append("%-34s %7s %7s %8s" % ("Test file", "Passed", "Failed", "Skipped"))
        out.append("-" * 59)
        for name, f in sorted(run["files"].items()):
            out.append("%-34s %7d %7d %8d" % (name, f["passed"], f["failed"], f["skipped"]))
        fails = [(n, x) for n, f in sorted(run["files"].items()) for x in f["failures"]]
        if fails:
            out.append("")
            out.append("Failures:")
            for n, x in fails:
                out.append("  FAIL %s::%s" % (n, x["test"]))
                out.append("       %s" % x["error"])
        out.append("")
        out.append("=" * 59)
        out.append("")
    if len(runs) > 1:
        g = {"passed": 0, "failed": 0, "skipped": 0}
        for r in runs:
            for k, v in totals(r).items():
                g[k] += v
        out.append("ALL REPORTS (%d): %d passed, %d failed, %d skipped"
                   % (len(runs), g["passed"], g["failed"], g["skipped"]))
    return "\n".join(out)


def render_md(runs):
    out = ["# Test summary", ""]
    for run in runs:
        t = totals(run)
        out += ["## %s (%s)" % (run["report"], run["target"]),
                "**%d passed, %d failed, %d skipped**" % (t["passed"], t["failed"], t["skipped"]), "",
                "| Test file | Passed | Failed | Skipped |", "|---|---:|---:|---:|"]
        for name, f in sorted(run["files"].items()):
            out.append("| %s | %d | %d | %d |" % (name, f["passed"], f["failed"], f["skipped"]))
        fails = [(n, x) for n, f in sorted(run["files"].items()) for x in f["failures"]]
        if fails:
            out += ["", "Failures:", ""]
            for n, x in fails:
                out.append("- `%s::%s` - %s" % (n, x["test"], x["error"]))
        out.append("")
    return "\n".join(out)


def collect(inputs):
    paths = []
    for item in inputs or [d for d in DEFAULT_DIRS if os.path.isdir(d)]:
        if os.path.isdir(item):
            paths += sorted(glob.glob(os.path.join(item, "*.xml")))
        else:
            paths += sorted(glob.glob(item)) or [item]
    return [p for p in paths if os.path.isfile(p)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", help="XML files or folders (default: %s)" % " + ".join(DEFAULT_DIRS))
    ap.add_argument("--md", help="also write a markdown summary to this file")
    ap.add_argument("--json", help="also write full data (untruncated errors) to this file")
    a = ap.parse_args()

    paths = collect(a.inputs)
    if not paths:
        print("No XML reports found. Pass file paths or a folder.", file=sys.stderr)
        return 2
    runs = []
    for p in paths:
        try:
            runs.append(parse_report(p))
        except ET.ParseError as e:
            print("Skipping %s (not valid XML: %s)" % (p, e), file=sys.stderr)
    print(render(runs))
    if a.md:
        open(a.md, "w").write(render_md(runs))
    if a.json:
        json.dump(runs, open(a.json, "w"), indent=2)
    return 1 if any(totals(r)["failed"] for r in runs) else 0


if __name__ == "__main__":
    sys.exit(main())
