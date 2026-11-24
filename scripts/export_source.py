#!/usr/bin/env python3
"""Export the current (source) account for a migration, carrying on past permissions its key lacks.

Runs `catocli export all --strip-ids --no-account-suffix`, then `catocli export static_hosts --site-type all`
(`export all` only covers physical Socket sites) and scripts/export_webhooks.py (it keeps webhook bodies).

catocli 3.3.0 abandons `export all` at the first type its key is refused for lack of permission ("Access
denied", for example a role without the "Socket Bypass" permission), skipping every type after it. This
script records that type as refused and runs `export all` again for the types it skipped, until every type
has been tried. A newer catocli that carries on by itself needs only the one run.

Everything refused or failed is written to <config-dir>/export_report.json. preflight_check.py reads it and
reports each refused type with the admin-role permission it needs, so the gap shows in the readiness report
instead of being found at deploy time.

Usage:
  python3 scripts/export_source.py --profile source
  python3 scripts/export_source.py --profile source --config-dir config_data

Exit code: 0 when every type exported or was refused for lack of permission (the check reports those),
1 when anything else failed.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
DENIED = re.compile(r"access denied|insufficient privileges|does not have sufficient permissions", re.I)
PROGRESS = re.compile(r"^\[\d+/\d+\] (\S+): (.*)$")
ABORTED = re.compile(r"ABORTING:.*access denied on '([^']+)'", re.I)


def _stream(cmd, env):
    """Run cmd, echoing its output as it comes; return (exit code, output lines)."""
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line.rstrip("\n"))
    return proc.wait(), lines


def export_types(catocli, env):
    """Every type `export all` knows, from its own answer to an unknown --types value (no API call)."""
    r = subprocess.run([catocli, "export", "all", "--types", "_"], env=env, capture_output=True, text=True)
    m = re.search(r"Valid types: ([^\n]+)", r.stdout + r.stderr)
    return [t.strip() for t in m.group(1).split(",") if t.strip()] if m else []


def parse_run(lines):
    """{type: (ok, error)} for every type a run attempted, and whether it aborted on an access denial."""
    seen, aborted = {}, False
    for line in lines:
        m = PROGRESS.match(line)
        if m:
            name, rest = m.group(1), m.group(2)
            if rest.startswith("OK"):
                seen[name] = (True, "")
            elif "FAILED" in rest:
                err = rest.split("FAILED", 1)[1].strip(" ()")
                seen[name] = (False, err or rest)
        if ABORTED.search(line):
            aborted = True
    return seen, aborted


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--profile", required=True, help="catocli profile of the current (source) account")
    ap.add_argument("--config-dir", default="config_data", help="where the export goes (default: config_data)")
    ap.add_argument("--no-webhooks", action="store_true", help="skip scripts/export_webhooks.py")
    args = ap.parse_args()

    catocli = shutil.which("catocli")
    if not catocli:
        print("catocli not found. Activate the virtual environment it was installed into "
              '(export PATH="$PWD/.venv/bin:$PATH") and run this again.', file=sys.stderr)
        return 1
    env = dict(os.environ, CATO_PROFILE=args.profile)
    base = [catocli, "export", "all", "--strip-ids", "--no-account-suffix", "--output-directory", args.config_dir]

    results = {}  # type -> (ok, error)
    remaining = export_types(catocli, env)  # empty if this catocli didn't say: one run of everything then
    while True:
        code, lines = _stream(base + (["--types", ",".join(remaining)] if remaining else []), env)
        seen, aborted = parse_run(lines)
        results.update(seen)
        if not seen:  # nothing attempted: the pre-flight credential check failed, or catocli itself did
            results["export all"] = (False, next((l for l in lines if "ERROR" in l), f"exit code {code}"))
            break
        if not aborted:
            break
        skipped = [t for t in remaining if t not in seen]
        if not skipped:
            if not remaining:  # aborted, but without the type list there's no telling what it skipped
                results["types after the refused one"] = (False, "catocli skipped them after an access denial")
            break
        denied = [t for t, (ok, e) in seen.items() if not ok and DENIED.search(e)]
        print(f"\nexport_source.py: {', '.join(denied)} refused; carrying on with the "
              f"{len(skipped)} type(s) catocli skipped.\n")
        remaining = skipped

    print("\nexport_source.py: static hosts on every site type")
    code, lines = _stream([catocli, "export", "static_hosts", "--site-type", "all", "-f", "json",
                           "--output-directory", args.config_dir], env)
    results["static_hosts --site-type all"] = (code == 0, "" if code == 0 else next(
        (l for l in lines if "rror" in l or "denied" in l.lower()), f"exit code {code}"))
    if not args.no_webhooks:
        print("\nexport_source.py: webhooks, with their bodies")
        code, lines = _stream([sys.executable, os.path.join(SCRIPTS, "export_webhooks.py"), "--profile", args.profile,
                               "--config-dir", args.config_dir], env)
        results["webhooks (export_webhooks.py)"] = (code == 0, "" if code == 0 else next(
            (l for l in lines if "rror" in l or "denied" in l.lower()), f"exit code {code}"))

    refused = [{"type": t, "error": e} for t, (ok, e) in results.items() if not ok and DENIED.search(e)]
    failed = [{"type": t, "error": e} for t, (ok, e) in results.items() if not ok and not DENIED.search(e)]
    exported = sorted(t for t, (ok, _) in results.items() if ok)
    os.makedirs(args.config_dir, exist_ok=True)
    with open(os.path.join(args.config_dir, "export_report.json"), "w") as fh:
        json.dump({"written_by": "export_source.py", "when": datetime.datetime.now().isoformat(timespec="seconds"),
                   "profile": args.profile, "exported": exported, "refused": refused, "failed": failed}, fh, indent=1)

    print(f"\nexport_source.py: {len(exported)} exported, {len(refused)} refused for lack of permission, "
          f"{len(failed)} failed. Details: {os.path.join(args.config_dir, 'export_report.json')}")
    for r in refused:
        print(f"  refused: {r['type']}")
    for f in failed:
        print(f"  failed:  {f['type']}: {f['error'][:160]}")
    if refused:
        print("The key's admin role is missing a permission for each refused type. The check lists them with the "
              "permission each needs; add them to the role (or use a key from an admin whose role covers the whole "
              "account), then run this again.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
