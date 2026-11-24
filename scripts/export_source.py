#!/usr/bin/env python3
"""Export the current (source) account for a migration, carrying on past permissions its key lacks.

Runs `catocli export all --strip-ids --no-account-suffix`, then `catocli export static_hosts --site-type all`
(`export all` only covers physical Socket sites) and scripts/export_webhooks.py (it keeps webhook bodies).

catocli 3.3.0 abandons `export all` at the first type its key is refused for lack of permission ("Access
denied", for example a role without the "Socket Bypass" permission), skipping every type after it. This
script records that type as refused and runs `export all` again for the types it skipped, until every type
has been tried. A newer catocli that carries on by itself needs only the one run.

Some exports leave a site's native range details (local IP, DHCP and the rest) on its LAN interface instead of
on the site, with only the subnet on the site; the site modules then miss the local IP. This script restates those
sites in the usual shape: the details on the site's native_range, and that interface as the default LAN. Nothing
is added or dropped. --normalize-only does just that to an existing export, with no API calls.

Everything refused or failed is written to <config-dir>/export_report.json. preflight_check.py reads it and
reports each refused type with the admin-role permission it needs, so the gap shows in the readiness report
instead of being found at deploy time.

Progress is shown as it happens: each step says what it does, catocli's own line for every type appears as
that type starts and finishes, and a "still working" line follows any quiet spell (a big policy can take a
minute), so a long export never looks stalled.

Usage:
  python3 scripts/export_source.py --profile source
  python3 scripts/export_source.py --profile source --config-dir config_data
  python3 scripts/export_source.py --normalize-only --config-dir config_data

Exit code: 0 when every type exported or was refused for lack of permission (the check reports those),
1 when anything else failed.
"""
import argparse
import datetime
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
DENIED = re.compile(r"access denied|insufficient privileges|does not have sufficient permissions", re.I)
PROGRESS = re.compile(r"^\[\d+/\d+\] (\S+): (.*)$")
ABORTED = re.compile(r"ABORTING:.*access denied on '([^']+)'", re.I)
START = time.monotonic()


def _clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def log(msg=""):
    """This script's own lines, stamped with the time since it started, set apart from catocli's."""
    print(f"[export_source {_clock(time.monotonic() - START)}] {msg}" if msg else "", flush=True)


def _stream(cmd, env, heartbeat, on_line=None, quiet=None):
    """Run cmd, echoing its output line by line as it comes; return (exit code, output lines).

    Python children (catocli, export_webhooks.py) hold their output back when it goes to a pipe instead of a
    terminal, so it all arrived at the end; PYTHONUNBUFFERED makes them hand over each line at once. When
    nothing arrives for `heartbeat` seconds, quiet(seconds since the last line) supplies a "still working" line.
    """
    proc = subprocess.Popen(cmd, env=dict(env, PYTHONUNBUFFERED="1"), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines_q = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines_q.put(line)
        lines_q.put(None)

    threading.Thread(target=pump, daemon=True).start()
    lines, last, nudged = [], time.monotonic(), time.monotonic()
    while True:
        try:
            line = lines_q.get(timeout=min(1.0, heartbeat / 4))
        except queue.Empty:
            now = time.monotonic()
            if now - nudged >= heartbeat:
                log(quiet(now - last) if quiet else f"Still working ({int(now - last)}s since the last update).")
                nudged = now
            continue
        if line is None:
            break
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line.rstrip("\n"))
        last = nudged = time.monotonic()
        if on_line:
            on_line(line.rstrip("\n"))
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


SITE_FILES = ("socket_sites.json", "vsocket_sites.json")
NATIVE_FIELDS = ("vlan", "mdns_reflector", "gateway", "range_type", "translated_subnet", "internet_only", "local_ip",
                 "dhcp_settings")


def lift_native_range(site):
    """Restate one site's native range in the shape the site modules read; True if it changed.

    The usual shape has every detail on site["native_range"] and the interface that carries it listed as
    {"default_lan": true, "network_ranges": [its other ranges]}. Some exports (sites with no WAN interfaces among
    them) give the site only the native subnet and leave the details on a LAN interface's range flagged
    native_range: true. Move them up; nothing is added or dropped.
    """
    nr = site.get("native_range") or {}
    lans = site.get("lan_interfaces") or []
    if not nr.get("subnet") or nr.get("local_ip") or any(li.get("default_lan") for li in lans):
        return False
    for i, li in enumerate(lans):
        ranges = li.get("network_ranges") or []
        native = next((r for r in ranges if r.get("native_range") is True and r.get("subnet") == nr["subnet"]), None)
        if native is None:
            continue
        site["native_range"] = dict(nr, range_name=nr.get("range_name") or native.get("name"),
                                    interface_name=li.get("name"), index=li.get("index") or native.get("interface_index"),
                                    dest_type=li.get("dest_type"), lag_min_links=li.get("lag_min_links"),
                                    **{k: native.get(k) for k in NATIVE_FIELDS})
        lans[i] = {"default_lan": True, "network_ranges": [r for r in ranges if r is not native]}
        return True
    return False


def lift_native_ranges(config_dir):
    """lift_native_range over the socket and vSocket site exports; returns 'file: site' for each site changed."""
    lifted = []
    for name in SITE_FILES:
        path = os.path.join(config_dir, name)
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            doc = json.load(fh)
        sites = doc.get("sites") if isinstance(doc, dict) else None
        changed = [s.get("name") for s in sites or [] if isinstance(s, dict) and lift_native_range(s)]
        if changed:
            with open(path, "w") as fh:
                json.dump(doc, fh, indent=2)
            lifted += [f"{name}: {n}" for n in changed]
    return lifted


def _log_lifted(lifted):
    if lifted:
        log(f"Restated the native range of {len(lifted)} site(s) with its details on the site, where the site "
            "modules read them (the export had left them on the LAN interface): "
            + ", ".join(x.split(": ", 1)[1] for x in lifted) + ".")


def _account(profile):
    """The account number the profile points at, for the opening line (None if it can't be read)."""
    try:
        sys.path.insert(0, SCRIPTS)
        import cato_profile
        return cato_profile.get_profile(profile).get("account_id")
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--profile", help="catocli profile of the current (source) account")
    ap.add_argument("--config-dir", default="config_data", help="where the export goes (default: config_data)")
    ap.add_argument("--no-webhooks", action="store_true", help="skip scripts/export_webhooks.py")
    ap.add_argument("--heartbeat", type=float, default=15, metavar="SECONDS",
                    help="after this many quiet seconds, print a line saying it's still working (default: 15)")
    ap.add_argument("--normalize-only", action="store_true",
                    help="only restate native ranges in an existing export (no API calls), then stop")
    args = ap.parse_args()
    if args.normalize_only:
        lifted = lift_native_ranges(args.config_dir)
        _log_lifted(lifted)
        if not lifted:
            log("Every site's native range is already in the usual shape; nothing changed.")
        return 0
    if not args.profile:
        ap.error("--profile is required (unless --normalize-only)")

    catocli = shutil.which("catocli")
    if not catocli:
        print("catocli not found. Activate the virtual environment it was installed into "
              '(export PATH="$PWD/.venv/bin:$PATH") and run this again.', file=sys.stderr)
        return 1
    env = dict(os.environ, CATO_PROFILE=args.profile)
    base = [catocli, "export", "all", "--strip-ids", "--no-account-suffix", "--output-directory", args.config_dir]
    n_steps = 2 if args.no_webhooks else 3

    acct = _account(args.profile)
    log(f"Exporting the current account (profile {args.profile}" + (f", account {acct}" if acct else "")
        + f") into {args.config_dir}/. This only reads; it changes nothing in the account.")
    remaining = export_types(catocli, env)  # empty if this catocli didn't say: one run of everything then
    total = len(remaining)
    log(f"Step 1 of {n_steps}: " + (f"{total} item types" if total else "every item type")
        + " (policies, sites, groups and more) with catocli export all. Each type gets a line as it starts and "
          "another as it finishes; a big one can take a minute or two.")

    results = {}  # type -> (ok, error)
    state = {"current": None, "since": time.monotonic()}

    def track(line):
        m = PROGRESS.match(line)
        if m and m.group(2).startswith("exporting"):
            state["current"], state["since"] = m.group(1), time.monotonic()
        elif m:
            results.setdefault(m.group(1), None)  # counted as done (the real result is parsed after the run)
            state["current"] = None

    def quiet_export(_):
        done = sum(1 for v in results.values())
        of = f"{done} of {total}" if total else f"{done}"
        if state["current"]:
            return (f"Still exporting {state['current']} ({int(time.monotonic() - state['since'])}s so far). "
                    f"{of} types done.")
        return f"Still working: {of} types done, waiting for catocli."

    while True:
        code, lines = _stream(base + (["--types", ",".join(remaining)] if remaining else []), env,
                              args.heartbeat, on_line=track, quiet=quiet_export)
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
        log()
        log(f"{', '.join(denied)} refused for lack of permission (the check will name the permission). "
            f"catocli stopped there, so carrying on with the {len(skipped)} type(s) it skipped.")
        remaining = skipped
    results = {t: v for t, v in results.items() if v is not None}
    ok_types = sum(1 for ok, _ in results.values() if ok)
    log()
    log(f"Step 1 done: {ok_types} of {len(results)} types exported.")

    log()
    log(f"Step 2 of {n_steps}: static hosts on every site type (catocli export all only covers physical Socket "
        "sites).")
    code, lines = _stream([catocli, "export", "static_hosts", "--site-type", "all", "-f", "json",
                           "--output-directory", args.config_dir], env, args.heartbeat,
                          quiet=lambda s: f"Still exporting static hosts ({int(s)}s since the last update).")
    results["static_hosts --site-type all"] = (code == 0, "" if code == 0 else next(
        (l for l in lines if "rror" in l or "denied" in l.lower()), f"exit code {code}"))
    log("Step 2 done." if code == 0 else "Step 2 failed; the summary below has the error.")
    if not args.no_webhooks:
        log()
        log(f"Step 3 of {n_steps}: webhooks, with their message bodies.")
        code, lines = _stream([sys.executable, os.path.join(SCRIPTS, "export_webhooks.py"), "--profile", args.profile,
                               "--config-dir", args.config_dir], env, args.heartbeat,
                              quiet=lambda s: f"Still exporting webhooks ({int(s)}s since the last update).")
        results["webhooks (export_webhooks.py)"] = (code == 0, "" if code == 0 else next(
            (l for l in lines if "rror" in l or "denied" in l.lower()), f"exit code {code}"))
        log("Step 3 done." if code == 0 else "Step 3 failed; the summary below has the error.")

    lifted = lift_native_ranges(args.config_dir)
    if lifted:
        log()
        _log_lifted(lifted)

    refused = [{"type": t, "error": e} for t, (ok, e) in results.items() if not ok and DENIED.search(e)]
    failed = [{"type": t, "error": e} for t, (ok, e) in results.items() if not ok and not DENIED.search(e)]
    exported = sorted(t for t, (ok, _) in results.items() if ok)
    os.makedirs(args.config_dir, exist_ok=True)
    with open(os.path.join(args.config_dir, "export_report.json"), "w") as fh:
        json.dump({"written_by": "export_source.py", "when": datetime.datetime.now().isoformat(timespec="seconds"),
                   "profile": args.profile, "exported": exported, "refused": refused, "failed": failed,
                   "native_range_lifted": lifted}, fh, indent=1)

    log()
    log(f"Finished: {len(exported)} exported, {len(refused)} refused for lack of permission, {len(failed)} failed. "
        f"Details: {os.path.join(args.config_dir, 'export_report.json')}")
    for r in refused:
        log(f"  refused: {r['type']}")
    for f in failed:
        log(f"  failed:  {f['type']}: {f['error'][:160]}")
    if refused:
        log("The key's admin role is missing a permission for each refused type. The check lists them with the "
            "permission each needs; add them to the role (or use a key from an admin whose role covers the whole "
            "account), then run this again.")
    if not failed:
        log("Next: run the check (python3 scripts/preflight_check.py ...), as in the getting started guide.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
