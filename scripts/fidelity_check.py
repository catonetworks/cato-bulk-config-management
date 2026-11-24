#!/usr/bin/env python3
"""Field-by-field check that every policy rule in the target matches its source.

Compares two `catocli export all` directories rule by rule (matched by name),
ignoring things that are expected to differ between tenants: ids, audit data,
positions (scripts/order_check.py covers order) and the id half of a
{id, name} reference. Prints one line per differing field path, grouped by
policy, and exits 1 if any rule differs.

Usage: python3 scripts/fidelity_check.py SOURCE_DIR TARGET_DIR [--json OUT]
  SOURCE_DIR  the export you deployed from (config_data, or the scoped copy)
  TARGET_DIR  a fresh `catocli export all` of the target, taken after the deploy
Exit code 1 if any rule differs, 2 on bad arguments.
"""
import glob
import json
import os
import sys
from collections import defaultdict

argv = sys.argv[1:]
OUT = None
if "--json" in argv:
    i = argv.index("--json")
    OUT = argv[i + 1]
    del argv[i:i + 2]
if len(argv) != 2:
    print(__doc__, file=sys.stderr)
    sys.exit(2)
S, T = argv
for _d in (S, T):
    if not os.path.isdir(_d):
        print(f"Not a folder: {_d}", file=sys.stderr)
        sys.exit(2)

IGNORE_KEYS = {"id", "audit", "properties", "index", "metadata", "section"}


def norm(o):
    """Drop tenant-specific ids; a {id, name} ref compares by name only."""
    if isinstance(o, dict):
        if "name" in o and "id" in o and len(o) <= 3:
            return {"name": o["name"]}
        return {k: norm(v) for k, v in o.items() if k not in IGNORE_KEYS}
    if isinstance(o, list):
        items = [norm(x) for x in o]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    return o


def diff(a, b, path, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            diff(a.get(k), b.get(k), f"{path}/{k}", out)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append((path, a, b))


def rules(path):
    doc = json.load(open(path))
    fam = next(iter(doc["data"]["policy"]))
    pol = doc["data"]["policy"][fam]["policy"]
    found = {}
    for rw in pol.get("rules") or []:
        r = rw.get("rule", rw)
        found[r["name"]] = r
    # LAN firewall: network rules, each carrying nested firewall rules
    for nw in pol.get("network_rules") or []:
        r = nw.get("rule", nw)
        found[r["name"]] = r
        for fw in nw.get("firewall_rules") or []:
            fr = fw.get("rule", fw)
            found[f'{r["name"]} :: {fr["name"]}'] = fr
    return fam, found


def main():
    report = {}
    total_rules = total_bad = 0
    for f in sorted(glob.glob(os.path.join(S, "all_*_rules_and_sections.json"))):
        tf = os.path.join(T, os.path.basename(f))
        if not os.path.exists(tf):
            continue
        fam, src = rules(f)
        _, tgt = rules(tf)
        both = sorted(set(src) & set(tgt))
        if not both:
            continue
        per_path = defaultdict(list)
        for name in both:
            d = []
            diff(norm(src[name]), norm(tgt[name]), "", d)
            for p, a, b in d:
                per_path[p].append({"rule": name, "source": a, "target": b})
        bad = {r["rule"] for v in per_path.values() for r in v}
        total_rules += len(both)
        total_bad += len(bad)
        print(f"{fam:22s} rules {len(both):3d}   identical: {len(both) - len(bad):3d}   differ: {len(bad)}")
        for p, rows in sorted(per_path.items()):
            ex = rows[0]
            print(f"    {p}  ({len(rows)} rule(s)), e.g. {ex['rule']!r}: {json.dumps(ex['source'])[:80]} -> {json.dumps(ex['target'])[:80]}")
        report[fam] = {"rules": len(both), "differ": sorted(bad), "fields": per_path}
    print(f"TOTAL rules compared {total_rules}, identical {total_rules - total_bad}, differ {total_bad}")
    if OUT:
        json.dump(report, open(OUT, "w"), indent=1, default=list)
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
