#!/usr/bin/env python3
"""Check that every rule migrated to the target sits in the same section and
relative position as in the source, and that sections are in the same order.

Compares two `catocli export all` directories. A re-plan cannot catch an
ordering problem (the bulk move resources do not read the live order back),
so run this after every deploy.

Usage: python3 scripts/order_check.py SOURCE_DIR TARGET_DIR
  SOURCE_DIR  the export you deployed from (config_data, or the scoped copy)
  TARGET_DIR  a fresh `catocli export all` of the target, taken after the deploy
Exit code 1 if any policy differs, 2 on bad arguments.
"""
import glob
import json
import os
import sys

if len(sys.argv) != 3:
    print(__doc__, file=sys.stderr)
    sys.exit(2)
S, T = sys.argv[1], sys.argv[2]
for _d in (S, T):
    if not os.path.isdir(_d):
        print(f"Not a folder: {_d}", file=sys.stderr)
        sys.exit(2)


def policy(path):
    doc = json.load(open(path))
    fam = next(iter(doc["data"]["policy"]))
    return fam, doc["data"]["policy"][fam]["policy"]


def main():
    all_ok = True
    for f in sorted(glob.glob(os.path.join(S, "all_*_rules_and_sections.json"))):
        b = os.path.basename(f)
        fam, ps = policy(f)
        _, pt = policy(os.path.join(T, b))
        key = "rules_in_sections" if "rules_in_sections" in ps else "rules_in_policy"
        idx = "index_in_section" if key == "rules_in_sections" else "index_in_policy"

        def seq(p):
            rows = p.get(key) or []
            return [(r.get("section_name", ""), r["rule_name"])
                    for r in sorted(rows, key=lambda r: (r.get("section_name", ""), r[idx]))]

        def sections(p):
            return [s["section_name"] for s in sorted(p.get("sections") or [], key=lambda s: s["section_index"])]

        in_target = {r["rule_name"] for r in (pt.get(key) or [])}
        src = [x for x in seq(ps) if x[1] in in_target]
        tgt = [x for x in seq(pt) if x[1] in {y[1] for y in src}]
        order_ok, sect_ok = src == tgt, sections(ps) == sections(pt)
        all_ok &= order_ok and sect_ok
        if not src and not ps.get("sections"):
            continue
        print(f"{fam:22s} rules {len(src):3d}   order kept: {'yes' if order_ok else 'NO'}"
              f"   sections in order: {'yes' if sect_ok else 'NO'}")
    print("ALL ORDERING MATCHES" if all_ok else "MISMATCH")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
