#!/usr/bin/env python3
"""Purge EVERY policy, site, and dependency type from a TARGET tenant, in safe order.

Orchestrates the per-type purge engine (scripts/purge/) as the EXACT REVERSE
of the current create order (bulk_apply FOLDER_SEQUENCE: 0-dependencies ->
1-sites -> 2-app-connectors -> 3-groups -> 4-policies/*), so the teardown tiers
are its strict reverse, folder for folder:

    1. 18 policy families (4-policies/*; see scripts/purge/families.py) -- reference everything
    2. groups                -- 3-groups (site-scoped members)
    3. app connectors        -- 2-app-connectors: private_apps (reference connector
       groups) then app_connectors; created AFTER sites, so deleted BEFORE them
    4. sites                 -- 1-sites; removeSite cascades ranges/interfaces/wifi/hosts
    5. account-level dependencies -- 0-dependencies (first folder applied -> last
       torn down): allocated_ips, then subscription_groups -> mailing_lists /
       webhooks (notification chain, children after referrers) -> custom_applications
       -> fqdn_containers -> ip_range_containers -> dhcp_relay_groups -> global_ip_ranges
       (ranges LAST, after all referencing policies, including Dynamic IP Allocation,
       have been removed and their revisions published)

The tier list is scripts/purge/families.py's NON_POLICY_TYPES; keep it in
lock-step with FOLDER_SEQUENCE so delete stays the exact reverse of create.

Sites ARE purged here now (previously excluded). Caveats: removing a site
deletes the live socket site and everything under it, so after a site purge
1-sites/ Terraform state must be reset to stateless before re-apply and sockets
need re-registration -- this is the exact reverse of 1-sites create, intended
for a full fresh build. The system mailing list 'All Admins' (id -100) is
skipped (API refuses deletion), the accepted 'system defaults remain' end state.
    6. SCIM groups+users -- OPT-IN via --include-scim (skipped by default),
       mirroring bulk_apply's --include-scim so SCIM is an explicit choice on both
       sides. When requested, purged LAST via scripts/scim_sync.py purge: each
       group then each user is DISABLED first, then DELETED (individually), so
       one run clears the whole tenant including users ahead of a re-push.

Safety: identical to the per-type wrappers -- dry-run default, <source-profile>/<SOURCE_ACCOUNT_ID>
hard-blocked, typed account-id confirmation + TTY required for --execute.
Stops at the first type with failures unless --keep-going.

Usage:
    python3 scripts/purge_all.py --profile <test-profile>                 # inventory
    python3 scripts/purge_all.py --profile <test-profile> --execute       # the operator only
    python3 scripts/purge_all.py --profile <test-profile> --only groups,internet_firewall
Exit codes: 0 clean, 1 failures, 2 guard refusal.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from purge.engine import confirm_account, guard_profile, log, run_one, set_config_dir  # noqa: E402
from purge.families import ALL_KEYS  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True,
                        help="catocli profile of the TARGET tenant")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True,
                       help="print the full would-delete inventory (default)")
    group.add_argument("--execute", action="store_true",
                       help="actually delete (interactive confirmation required)")
    parser.add_argument("--only", default=None,
                        help="comma-separated subset of types (still in safe order)")
    parser.add_argument("--skip", default=None,
                        help="comma-separated types to skip")
    parser.add_argument("--keep-going", action="store_true",
                        help="continue past a type that had failures")
    parser.add_argument("--config-dir", default=os.path.join(REPO_ROOT, "config_data"),
                        help="config_data dir; enumeration snapshots land under "
                             "<config-dir>/purge_snapshots, threaded to scim_sync too "
                             "(default: ./config_data)")
    parser.add_argument("--include-scim", action="store_true",
                        help="ALSO purge SCIM groups+users (opt-in, skipped by default -- mirrors "
                             "bulk_apply's --include-scim). When set, SCIM is purged last via "
                             "scripts/scim_sync.py so a single purge clears the whole tenant "
                             "including users ahead of a re-push)")
    parser.add_argument("--confirmed-account",
                        help="account id already confirmed by an orchestrator/operator; skips the "
                             "interactive typed-account prompt when it matches (<source-profile> block still applies)")
    parser.add_argument("--json-report", default=None,
                        help="write combined results as JSON to this path")
    args = parser.parse_args()
    set_config_dir(args.config_dir)

    keys = list(ALL_KEYS)
    if args.only:
        wanted = {k.strip() for k in args.only.split(",") if k.strip()}
        unknown = wanted - set(ALL_KEYS)
        if unknown:
            log(f"ERROR: unknown type(s): {', '.join(sorted(unknown))}")
            return 2
        keys = [k for k in keys if k in wanted]
    if args.skip:
        skipped = {k.strip() for k in args.skip.split(",") if k.strip()}
        keys = [k for k in keys if k not in skipped]

    profile = guard_profile(args.profile)
    log(f"target: profile {args.profile!r} -> account {profile['account_id']}")
    log(f"types ({len(keys)}, safe order): {', '.join(keys)}")

    if args.execute and not confirm_account(profile["account_id"], args.confirmed_account):
        return 2

    reports = []
    failed = False
    for key in keys:
        log(f"\n--- {key} ---")
        try:
            report = run_one(args.profile, key, args.execute)
        except (RuntimeError, OSError, ValueError) as exc:
            log(f"ERROR ({key}): {exc}")
            failed = True
            if not args.keep_going:
                break
            continue
        reports.append(report)
        if report.exit_code != 0:
            failed = True
            if not args.keep_going:
                log("stopping (use --keep-going to continue past failures)")
                break

    if args.include_scim:
        log("\n--- scim (groups+users via scripts/scim_sync.py) ---")
        cmd = [sys.executable, os.path.join(REPO_ROOT, "scripts", "scim_sync.py"),
               "purge", "--target-profile", args.profile, "--config-dir", args.config_dir]
        if args.execute:
            # purge_all already did its own typed-account confirmation; pass it
            # through so scim_sync doesn't prompt a second time.
            cmd += ["--execute", "--confirmed-account", profile["account_id"]]
        if subprocess.run(cmd).returncode != 0:
            failed = True
    else:
        log("\n--- scim: skipped (--include-scim not set) -- SCIM groups+users left "
            "in place; pass --include-scim to also clear them ---")

    if args.execute:
        log("\n" + "=" * 60)
        for report in reports:
            log(report.summary_line())
    if args.json_report:
        with open(args.json_report, "w") as f:
            json.dump([r.as_dict() for r in reports], f, indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
