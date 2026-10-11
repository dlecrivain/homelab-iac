#!/usr/bin/env python3
"""Summarises an OpenSCAP XCCDF results file as JSON on stdout.

    summarize_xccdf.py <results.xml> <datastream.xml>

The results file has the score and the verdict of each rule but not the rule titles, which
are read from the datastream that was scanned against. Only the titles of failed rules are kept.
"""
import json
import sys
import xml.etree.ElementTree as ET

RULE_PREFIX = "xccdf_org.ssgproject.content_rule_"
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def local(tag):
    return tag.rsplit("}", 1)[-1]


def read_results(path):
    counts = {}
    failed = []
    score = None
    for element in ET.parse(path).iter():
        name = local(element.tag)
        if name == "score" and score is None:
            score = float(element.text)
        elif name == "rule-result":
            verdict = next((c.text for c in element if local(c.tag) == "result"), "unknown")
            counts[verdict] = counts.get(verdict, 0) + 1
            if verdict == "fail":
                failed.append({
                    "id": element.get("idref", "").removeprefix(RULE_PREFIX),
                    "severity": element.get("severity", "unknown"),
                })
    return score, counts, failed


def read_titles(path, wanted):
    titles = {}
    for _, element in ET.iterparse(path):
        if local(element.tag) == "Rule":
            rule_id = element.get("id", "").removeprefix(RULE_PREFIX)
            if rule_id in wanted:
                title = next((c for c in element if local(c.tag) == "title"), None)
                titles[rule_id] = "".join(title.itertext()).strip() if title is not None else ""
            element.clear()
    return titles


def main():
    results_path, datastream_path = sys.argv[1:3]
    score, counts, failed = read_results(results_path)
    titles = read_titles(datastream_path, {rule["id"] for rule in failed})
    for rule in failed:
        rule["title"] = titles.get(rule["id"], rule["id"])
    failed.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 3), r["id"]))
    json.dump({"score": score, "counts": counts, "failed": failed}, sys.stdout)


if __name__ == "__main__":
    main()
