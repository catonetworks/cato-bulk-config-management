#!/usr/bin/env python3
"""Scope the policy exports to what a TARGET tenant can actually hold.

Why: a policy folder only orders its rules and sets the policy's enabled flag
once EVERY rule in it has applied. If some rules can never apply on a target
(their users/groups were deliberately not pushed, or they reference objects
that can only be created by hand in the CMA), the whole policy is left
misordered and switched off. This script writes a COPY of config_data where
exactly those rules are left out, so every policy folder can complete, and
reports every rule it removed and why. The source export is never modified.

It never edits a rule. It only drops whole rules. (Dropping a reference from
inside a rule could WIDEN what the rule matches, which is never safe.)

Usage:
  python3 scripts/scope_policies_to_target.py --config-dir config_data \\
      --out config_data_<target> --skip-scim \\
      --missing globalIpRange=AzureVDI-2 --missing bandwidthPriority=50 \\
      --report scope-report.json

  --skip-scim        every usersGroup / user reference counts as missing
                     (use when SCIM identities are not pushed to this target)
  --skip-rule NAME   leave out one rule the target cannot hold for a reason
                     that is not a missing reference (e.g. it needs a site
                     setting, such as LAN Layer 7 enforcement, that only the
                     CMA UI can switch on). Nested LAN firewall rules are named
                     "<network rule> / <firewall rule>". Repeatable.
  --missing KEY=NAME a referenced object that does not exist on the target,
                     by the JSON key rules use for it (usersGroup, user, group,
                     globalIpRange, device, customCategory, bandwidthPriority,
                     siteNetworkSubnet, ...). Repeatable.

Then point bulk_apply at the copy:
  python3 scripts/bulk_apply.py --profile <target> --mode deploy --config-dir config_data_<target>
"""
import argparse
import copy
import glob
import json
import os
import shutil
import sys


def refs(obj, key_filter=None):
    """Yield (key, name) for every {name: ...} ref under a key in obj."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and "name" in item and (key_filter is None or k in key_filter):
                        yield k, item.get("name")
            elif isinstance(v, dict) and "name" in v and (key_filter is None or k in key_filter):
                yield k, v.get("name")
            yield from refs(v, key_filter)
    elif isinstance(obj, list):
        for v in obj:
            yield from refs(v, key_filter)


# The same object under another key in some policies (keep in step with preflight_check.REF_ALIASES):
# device posture in Always-On, TLS Inspection and Dynamic IP Allocation; global ranges in Terminal Server.
REF_ALIASES = {"devicePostureProfile": "device", "allowedHostIP": "globalIpRange", "excludeTraffic": "globalIpRange"}


def blockers(rule, missing, skip_scim, skip_rules=(), qualified_name=None):
    out = set()
    if (qualified_name or rule.get("name")) in skip_rules:
        out.add("skipped:" + (qualified_name or rule.get("name")))
    for k, name in refs(rule):
        k = REF_ALIASES.get(k, k)
        if skip_scim and k in ("usersGroup", "user"):
            out.add(f"{k}:{name}")
        elif name in missing.get(k, set()):
            out.add(f"{k}:{name}")
    return sorted(out)


def renumber(rows, removed, index_key, group_key=None):
    kept, counters = [], {}
    for row in rows:
        if row.get("rule_name") in removed:
            continue
        row = dict(row)
        g = row.get(group_key) if group_key else None
        counters[g] = counters.get(g, 0) + 1
        row[index_key] = counters[g]
        kept.append(row)
    return kept


def scope_file(path, missing, skip_scim, skip_rules=()):
    doc = json.load(open(path))
    family = next(iter(doc["data"]["policy"]))
    pol = doc["data"]["policy"][family]["policy"]
    removed = {}
    for list_key in ("rules", "network_rules"):
        items = pol.get(list_key)
        if not isinstance(items, list):
            continue
        kept = []
        for it in items:
            rule = it.get("rule") or {}
            b = blockers(rule, missing, skip_scim, skip_rules)
            if b:
                removed[rule.get("name")] = b
                continue
            # LAN firewall: nested firewall rules under a kept network rule
            if isinstance(it.get("firewall_rules"), list):
                it = copy.deepcopy(it)
                nested = []
                for fr in it["firewall_rules"]:
                    fb = blockers(fr.get("rule") or {}, missing, skip_scim, skip_rules,
                                  f"{rule.get('name')} / {(fr.get('rule') or {}).get('name')}")
                    if fb:
                        removed[f"{rule.get('name')} / {(fr.get('rule') or {}).get('name')}"] = fb
                    else:
                        nested.append(fr)
                it["firewall_rules"] = nested
            kept.append(it)
        pol[list_key] = kept
    if "rules_in_sections" in pol:
        pol["rules_in_sections"] = renumber(pol["rules_in_sections"], removed, "index_in_section", "section_name")
    if "rules_in_policy" in pol:
        pol["rules_in_policy"] = renumber(pol["rules_in_policy"], removed, "index_in_policy")
    return doc, family, removed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip-scim", action="store_true")
    ap.add_argument("--missing", action="append", default=[], metavar="KEY=NAME")
    ap.add_argument("--skip-rule", action="append", default=[], metavar="NAME")
    ap.add_argument("--report")
    a = ap.parse_args()
    if os.path.abspath(a.out) == os.path.abspath(a.config_dir):
        print("REFUSED: --out must differ from --config-dir (the source export is never modified).", file=sys.stderr)
        return 2
    missing = {}
    for m in a.missing:
        k, _, v = m.partition("=")
        missing.setdefault(k, set()).add(v)
    if os.path.exists(a.out):
        shutil.rmtree(a.out)
    shutil.copytree(a.config_dir, a.out, ignore=shutil.ignore_patterns(".remapped"))
    report = []
    for path in sorted(glob.glob(os.path.join(a.config_dir, "all_*_rules_and_sections.json"))):
        doc, family, removed = scope_file(path, missing, a.skip_scim, set(a.skip_rule))
        with open(os.path.join(a.out, os.path.basename(path)), "w") as f:
            json.dump(doc, f, indent=2)
        for name, why in removed.items():
            report.append({"file": os.path.basename(path), "family": family, "rule": name, "blocked_by": why})
        print(f"{os.path.basename(path):40s} left out {len(removed):3d} rule(s)")
    print(f"total left out: {len(report)} -> {a.out}")
    if a.report:
        with open(a.report, "w") as f:
            json.dump(report, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
