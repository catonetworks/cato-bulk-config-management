#!/usr/bin/env python3
"""Bulk plan/apply orchestrator for every Terraform folder in this repo.

Runs the folders in dependency order (0-dependencies -> 1-sites ->
2-app-connectors -> 3-groups -> all 4-policies/*; SCIM pushed before sites),
each through `terraform init` / `validate` / `plan -out=<plans-dir>/...`, and
prints a per-folder add/change/destroy/import summary table. Apply mode
replays the SAVED plan files only, one folder at a time, stopping on the
first failure.

Safety model (see docs/bulk-apply-runbook.md):
  - plan mode (default) never mutates a tenant.
  - apply mode requires an interactive typed confirmation of the resolved
    account id, refuses stale plan files, and refuses the source tenant
    (<source-profile> / <SOURCE_ACCOUNT_ID>) unless --source-tenant-apply is also given.
  - greenfield guard: BOTH plan and apply against a NON-source tenant stop
    hard on any folder containing migration.tf (import blocks evaluate at
    plan time and carry source-tenant ids, producing walls of "Cannot import
    non-existent remote object" errors on a target; an empty target needs
    pure creates). Park them first with --park-migrations (renames to
    migration.tf.parked; --unpark-migrations reverses). Parking is refused
    on the source tenant, where migration.tf is intentional. A warning (not
    a refusal) is printed if terraform.tfstate files are present for a
    target-tenant run -- replication expects a stateless tree.
  - webhook manual-gate warning (plan + apply, target tenants only): if
    config_data/subscription_groups.json references any webhook/integration,
    print the names that must already exist in the target CMA -- webhooks
    have no provider resource, so this is a warning, never a refusal.
  - SCIM push (apply/deploy, target tenants only, OPT-IN via --include-scim):
    when the flag is passed, scripts/scim_sync.py push runs FIRST, before any
    folder -- 11 policy families resolve usersGroup/user names at apply.
    Without the flag SCIM is untouched and a one-line skip notice is printed
    (the SCIM import is create-only, so pushing into a non-empty target fails
    as duplicates).
  - credentials come from the named catocli profile via cato_profile.py,
    explicitly overriding any TF_VAR_* inherited from the shell.
  - by default terraform runs under a CLEAN CLI config (TF_CLI_CONFIG_FILE
    pointing at a generated file with no dev_overrides), so the provider
    resolves the exact pinned version (currently 2.0.0-prerelease.1) straight
    from the public Terraform Registry, rather than whatever dev_overrides your
    personal ~/.terraformrc happens to have pointed at. Pass
    --allow-dev-overrides to use your ~/.terraformrc as-is instead, or --mirror
    to resolve from a local filesystem mirror (only for a LOCAL provider build).

Usage:
    python3 scripts/bulk_apply.py --profile <source-profile>                  # plan all
    python3 scripts/bulk_apply.py --profile <source-profile> --folders 4-policies/internet-firewall
    python3 scripts/bulk_apply.py --profile <target-profile> --mode apply # the operator only

Exit codes: 0 = all folders clean/successful, 1 = a folder failed,
2 = guard refusal / bad arguments.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cato_profile import get_profile, terraform_env, load_protected, warn_if_unprotected  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Configurable source-tenant guard (see cato_profile.load_protected). Defaults
# to EMPTY -- this package protects nothing until the operator declares their
# own source tenant via cbcm.config.json or CBCM_PROTECTED_ACCOUNTS /
# CBCM_PROTECTED_PROFILES. apply/deploy/destroy against a protected tenant then
# require --source-tenant-apply.
PROTECTED_ACCOUNTS, PROTECTED_PROFILES = load_protected()

# Fixed deployment order (restructured 2026-11-13):
#   0-dependencies -> [SCIM push] -> 1-sites -> 2-app-connectors -> 3-groups
#   -> 4-policies/*
# Rationale: account-level dependency objects first; SCIM users/groups pushed
# next (the inline run_scim_push step -- "0-scim-users"); then sites; then ZTNA
# app connectors + private apps (split out of dependencies so a flaky
# connector/private-app apply can't sit between the dependency tier and sites,
# and so private apps publish through connector groups that exist); then groups
# (members are site-scoped, so after sites); then every policy folder. Policy
# folders are independent of each other (CLAUDE.md) -- alphabetical. All 18
# policy folders live under 4-policies/. SCIM users/groups must be pushed before
# the first 4-policies/* folder applies -- see the gate printed in apply mode.
# 0-scim-users/ is a docs-only tier; the actual push is the inline SCIM step.
FOLDER_SEQUENCE = [
    "0-dependencies",
    "1-sites",
    "2-app-connectors",
    "3-groups",
    "4-policies/always-on",
    "4-policies/anti-malware-file-hash",
    "4-policies/app-tenant-restriction",
    "4-policies/application-control",
    "4-policies/client-connectivity",
    "4-policies/dynamic-ip-allocation",
    "4-policies/internet-firewall",
    "4-policies/lan-firewall",
    "4-policies/private-access",
    "4-policies/remote-port-forward",
    "4-policies/sdp-static-ip-allocation",
    "4-policies/site-web-proxy",
    "4-policies/socket-bypass",
    "4-policies/split-tunnel",
    "4-policies/terminal-server",
    "4-policies/tls-inspection",
    "4-policies/wan-firewall",
    "4-policies/wan-network",
]

# Families (compact names) that resolve SCIM usersGroup/user names at apply
# time -- the SCIM gate below lists these. See docs/bulk-apply-runbook.md /
# the dependency-matrix sweep for the full reference counts per family.
SCIM_DEPENDENT_FAMILIES = [
    "ao", "apc", "atr", "cc", "dia", "ifw", "pa", "st", "tls", "wf", "wnw",
]

# Default: resolve the PINNED provider version (currently 2.0.0-prerelease.1)
# straight from the public Terraform Registry, with no dev_overrides. That
# version is PUBLISHED, so `direct {}` is authoritative -- no filesystem mirror
# needed. The clean file still exists only to shadow any dev_overrides in the
# operator's personal ~/.terraformrc.
REGISTRY_TFRC_TEMPLATE = """\
# Generated by scripts/bulk_apply.py -- forces registry resolution of the
# PINNED provider version, with no dev_overrides.
plugin_cache_dir = "$HOME/.terraform.d/plugin-cache"
provider_installation {
  direct {}
}
"""

# --mirror only: serve the pinned provider from a LOCAL filesystem_mirror
# instead of the registry. Use this for a local provider build (`make
# install-mirror` writes the version-tagged binary to ~/.terraform.d/mirror);
# the registry path above is the default now that the pinned version ships on
# the registry. NOTE: unlike plugin_cache_dir, filesystem_mirror's `path` does
# NOT expand $HOME/~ -- it must be a literal absolute path, hence the
# os.path.expanduser() below.
MIRROR_TFRC_TEMPLATE = """\
# Generated by scripts/bulk_apply.py --mirror -- serves the PINNED provider
# from a local filesystem mirror (for a LOCAL provider build), no dev_overrides.
plugin_cache_dir = "$HOME/.terraform.d/plugin-cache"
provider_installation {{
  filesystem_mirror {{
    path    = "{mirror_dir}"
    include = ["registry.terraform.io/catonetworks/cato"]
  }}
  direct {{}}
}}
"""


def log(msg):
    print(msg, file=sys.stderr)


def run(cmd, cwd, env, capture=False):
    return subprocess.run(
        cmd, cwd=cwd, env=env, text=True,
        capture_output=capture,
    )


def newest_input_mtime(folder_path, config_dir):
    """Newest mtime among the folder's .tf files and the config_data JSONs
    it references -- used to detect stale plan files before apply."""
    newest = 0.0
    for name in os.listdir(folder_path):
        if name.endswith(".tf") or name.endswith(".tfvars"):
            newest = max(newest, os.path.getmtime(os.path.join(folder_path, name)))
    if os.path.isdir(config_dir):
        for name in os.listdir(config_dir):
            if name.endswith(".json"):
                newest = max(newest, os.path.getmtime(os.path.join(config_dir, name)))
    return newest


def webhook_integration_names(config_dir):
    """Distinct webhook/integration names referenced by
    <config_dir>/subscription_groups.json's integration[] arrays, sorted.
    Returns [] if the file is absent, unparsable, or has no integration refs
    -- this is advisory only, never a gate failure."""
    path = os.path.join(config_dir, "subscription_groups.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    names = set()
    for entry in data if isinstance(data, list) else []:
        for integration in (entry.get("integration") or []):
            name = integration.get("name")
            if name:
                names.add(name)
    return sorted(names)


def plan_summary(folder_path, plan_file, env):
    """Parse `terraform show -json` of a saved plan into counts."""
    result = run(["terraform", "show", "-json", plan_file], folder_path, env, capture=True)
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    counts = {"add": 0, "change": 0, "destroy": 0, "import": 0}
    for rc in data.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if rc.get("change", {}).get("importing"):
            counts["import"] += 1
        if actions == ["no-op"]:
            continue
        if "create" in actions:
            counts["add"] += 1
        if "update" in actions:
            counts["change"] += 1
        if "delete" in actions:
            counts["destroy"] += 1
    return counts


def confirm_account(account_id, preconfirmed=None):
    """Interactive typed confirmation; aborts unless the exact id is typed.

    An orchestrator that already confirmed the account can pass it via
    `preconfirmed` to skip the interactive prompt (the id must still match; the
    source-tenant block is enforced separately)."""
    if preconfirmed is not None:
        if preconfirmed == account_id:
            log(f"account {account_id} pre-confirmed.")
            return True
        log(f"REFUSED: pre-confirmed {preconfirmed!r} does not match resolved account {account_id}.")
        return False
    if not sys.stdin.isatty():
        log("REFUSED: apply/execute requires an interactive terminal (no CI applies).")
        return False
    typed = input(
        f"Type the target account id ({account_id}) to confirm: "
    ).strip()
    if typed != account_id:
        log(f"REFUSED: typed {typed!r} does not match resolved account {account_id}.")
        return False
    return True


# Allocated-IP resolution moved INTO Terraform (2026-11-11): the wan-network
# and remote-port-forward folders now resolve each rule's allocated-IP display
# name against the TARGET tenant at plan time via the cato_allocatedIp data
# source, joined on the stable (description, popLocation) identity from
# config_data/allocated_ips.json (see each folder's allocated_ip_resolution.tf
# and the modules' allocated_ip_name_map variable). A referenced allocation
# that doesn't resolve on the target fails the PLAN with a clear message.
# The former scripts/remap_allocated_ips.py staging-copy flow and its
# TF_VAR_*_json_file_path overrides are retired for these families.


def run_scim_push(profile_name, account_id, config_dir):
    """Push SCIM users/groups into the target BEFORE the first folder that
    resolves usersGroup/user names (3-groups and every 4-policies/* family).
    This is the "0-scim-users" identity tier; it runs before any TF folder.

    Delegates to scripts/scim_sync.py push --execute, passing the already-typed
    account id via --confirmed-account so the operator isn't prompted twice.
    scim_sync still hard-blocks the source tenant and verifies the id. Returns
    True on success. If the committed SCIM dataset is absent, logs and skips
    (treated as success -- nothing to push), so accounts without SCIM data
    aren't blocked.
    """
    users_csv = os.path.join(config_dir, "scim_users.csv")
    if not os.path.exists(users_csv):
        # HARD FAIL: the operator explicitly asked for a push (--include-scim), so
        # a missing dataset is an error, not a skip -- a silent skip here once
        # let a deploy march on without users and every usersGroup/user-
        # referencing policy rule failed with ruleHasInvalidEntity.
        log(f"GATE (SCIM push) FAILED: {users_csv} is missing.\n"
            "Regenerate the dataset first:\n"
            "    python3 scripts/scim_sync.py export --source-profile <source-profile>")
        return False
    scim_sync = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scim_sync.py")
    cmd = [sys.executable, scim_sync, "push",
           "--target-profile", profile_name, "--execute",
           "--confirmed-account", account_id, "--config-dir", config_dir]
    log(f"\nGATE (SCIM push): {' '.join(cmd)}")
    # Inherit stdio so scim_sync's own logging streams live; it does its own
    # source-tenant block + id check.
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        log(f"SCIM push FAILED (exit {result.returncode}) -- aborting before "
            "any usersGroup/user-referencing folder applies.")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", required=True,
                        help="catocli profile providing credentials (shell CATO_PROFILE/TF_VARs are ignored)")
    parser.add_argument("--mode", choices=["plan", "apply", "deploy", "destroy"], default="plan",
                        help="plan: write saved plans; apply: replay saved plans; "
                             "deploy: plan+apply each folder atomically in one run "
                             "(no saved-plan gap -> immune to stale-plan / profile-mismatch "
                             "/ lock-drift refusals; ideal for disposable-state test tenants); "
                             "destroy: terraform destroy each folder in REVERSE order "
                             "(4-policies/* -> 3-groups -> 2-app-connectors -> 1-sites -> 0-dependencies), the "
                             "teardown order -- same guards as apply (TTY + typed account-id, "
                             "<source-profile> blocked)")
    parser.add_argument("--folders", default=None,
                        help="comma-separated subset (default: all, in fixed order)")
    parser.add_argument("--start-at", default=None,
                        help="skip folders before this one in the sequence")
    parser.add_argument("--plans-dir", default=os.path.join(REPO_ROOT, ".bulk_plans"),
                        help="where saved plan files land (default: .bulk_plans/, gitignored)")
    parser.add_argument("--config-dir", default=os.path.join(REPO_ROOT, "config_data"),
                        help="directory holding the exported config_data/*.json (default: "
                             "./config_data). Passed to terraform as TF_VAR_config_data_dir "
                             "(absolute) and to the validate/SCIM sub-steps.")
    parser.add_argument("--purge-first", action="store_true",
                        help="run scripts/purge_all.py --dry-run against the profile first (prints inventory; never executes deletions itself)")
    parser.add_argument("--preflight-source", metavar="PROFILE", default=None,
                        help="run scripts/preflight_check.py (read-only) with this SOURCE profile against "
                             "--profile first, and stop if it reports a BLOCKER (dependencies the target is "
                             "missing: identities, PoPs, licences, CMA-only objects, features)")
    parser.add_argument("--ignore-preflight", action="store_true",
                        help="with --preflight-source: print the report but carry on even with BLOCKERs "
                             "(pair with --keep-going and a scoped --config-dir)")
    parser.add_argument("--skip-validate", action="store_true",
                        help="skip the scripts/validate_config_data.py gate")
    parser.add_argument("--no-upgrade", action="store_true",
                        help="deprecated/no-op: not upgrading is now the DEFAULT in every "
                             "mode. Retained so existing invocations don't break; pass "
                             "--upgrade to opt into `terraform init -upgrade`.")
    parser.add_argument("--upgrade", action="store_true",
                        help="opt into `terraform init -upgrade` in every mode (default is "
                             "NO -upgrade everywhere). Use when the committed "
                             ".terraform.lock.hcl pins an older provider version than the "
                             "current config requires (e.g. after the 2.0.0-prerelease.1 "
                             "bump) -- -upgrade re-resolves the provider against the "
                             "versions.tf constraints and rewrites the lock, instead of "
                             "failing with 'no available releases match the given "
                             "constraints'. In apply mode it can change the provider "
                             "selection vs the saved plan, so pass it only when intended.")
    parser.add_argument("--parallelism", type=int, default=1,
                        help="terraform -parallelism (default 1, per repo convention)")
    parser.add_argument("--allow-dev-overrides", action="store_true",
                        help="use ~/.terraformrc as-is (default: force clean resolution of the pinned provider version from the public registry)")
    parser.add_argument("--mirror", action="store_true",
                        help="serve the pinned provider from a LOCAL filesystem mirror (~/.terraform.d/mirror) "
                             "instead of the public registry (the default). Use only for a LOCAL provider build: "
                             "`make install-mirror` writes the version-tagged binary to the mirror dir. "
                             "No effect with --allow-dev-overrides.")
    parser.add_argument("--source-tenant-apply", action="store_true",
                        help="extra flag REQUIRED to apply against the source tenant (<source-profile>/<SOURCE_ACCOUNT_ID>)")
    parser.add_argument("--confirmed-account",
                        help="account id already confirmed by an orchestrator/operator; skips the "
                             "interactive typed-account prompt when it matches the resolved account "
                             "(the <source-profile>/source block still applies)")
    parser.add_argument("--keep-going", action="store_true",
                        help="continue to the next folder after one fails (default: stop at the "
                             "first failure). Useful to map which folders deploy cleanly vs which "
                             "reference objects outside the replication scope.")
    parser.add_argument("--include-scim", action="store_true",
                        help="push the SCIM users/groups from config_data/scim_*.csv (via "
                             "scripts/scim_sync.py) FIRST, before any folder, so user/usersGroup "
                             "policy references resolve. SCIM is NOT pushed by default -- without "
                             "this flag the deploy assumes the target's users/groups already exist "
                             "(pushed out of band or on a previous run). NOTE: the SCIM import is "
                             "create-only, so pushing into a tenant that already has the objects "
                             "fails them as duplicates -- only pass this against a purged/empty "
                             "target.")
    parser.add_argument("--park-migrations", action="store_true",
                        help="standalone action: rename migration.tf -> migration.tf.parked in the "
                             "selected folders (required before target-tenant plans/applies -- "
                             "import blocks carry SOURCE-tenant ids), then exit. Refused on the "
                             "source tenant, where migration.tf is intentional.")
    parser.add_argument("--unpark-migrations", action="store_true",
                        help="standalone action: rename migration.tf.parked back to migration.tf "
                             "in the selected folders, then exit")
    parser.add_argument("--allow-target-migrations", action="store_true",
                        help="opt out of the greenfield guard so plan/apply can run with "
                             "migration.tf present against a NON-source tenant. Use ONLY when "
                             "config_data was re-exported from THIS target (so the import ids "
                             "are the target's own object ids) and you are testing brownfield "
                             "state adoption INTO the target -- e.g. re-importing <ACCOUNT_ID>'s own "
                             "exported config back into state. Imports are skipped for resources "
                             "already in state, so clear/aside each folder's terraform.tfstate "
                             "first for the import to actually run.")
    args = parser.parse_args()

    try:
        profile = get_profile(args.profile)
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 2
    account_id = profile["account_id"]
    config_dir = os.path.abspath(args.config_dir)
    # Loudly warn when no source-tenant guard is declared, for the destructive
    # modes (apply/deploy/destroy); plan/validate are read-only so stay quiet.
    if args.mode in ("apply", "deploy", "destroy"):
        warn_if_unprotected(PROTECTED_ACCOUNTS, PROTECTED_PROFILES,
                            action=f"bulk {args.mode}")
    # Is this a protected source tenant? Empty guard => always False (nothing
    # protected) until the operator declares a source tenant.
    is_source = account_id in PROTECTED_ACCOUNTS or args.profile in PROTECTED_PROFILES
    log(f"profile {args.profile!r} -> account {account_id} "
        f"({'SOURCE tenant (protected)' if is_source else 'target tenant'})")

    folders = list(FOLDER_SEQUENCE)
    if args.folders:
        requested = [f.strip() for f in args.folders.split(",") if f.strip()]
        unknown = [f for f in requested if f not in FOLDER_SEQUENCE]
        if unknown:
            log(f"ERROR: unknown folder(s): {', '.join(unknown)}")
            return 2
        folders = [f for f in FOLDER_SEQUENCE if f in requested]
    if args.start_at:
        if args.start_at not in folders:
            log(f"ERROR: --start-at {args.start_at!r} not in the folder list")
            return 2
        folders = folders[folders.index(args.start_at):]

    # Destroy tears down in the REVERSE of create order: policy families ->
    # groups -> sites -> dependencies. Policies reference groups/sites/deps, so
    # they must be destroyed first; dependencies (referenced by everything) last.
    if args.mode == "destroy":
        folders = list(reversed(folders))

    # ---- park/unpark standalone actions -------------------------------------
    if args.park_migrations and args.unpark_migrations:
        log("ERROR: --park-migrations and --unpark-migrations are mutually exclusive.")
        return 2
    if args.park_migrations:
        if is_source:
            log("REFUSED: --park-migrations on a protected SOURCE tenant -- its "
                "migration.tf files are intentional re-adoption imports; parking them there "
                "would silently skip them.")
            return 2
        parked = 0
        for f in folders:
            src = os.path.join(REPO_ROOT, f, "migration.tf")
            if os.path.exists(src):
                os.rename(src, src + ".parked")
                log(f"parked: {f}/migration.tf -> migration.tf.parked")
                parked += 1
        log(f"parked {parked} migration.tf file(s). Re-enable later with --unpark-migrations.")
        return 0
    if args.unpark_migrations:
        unparked = 0
        for f in folders:
            src = os.path.join(REPO_ROOT, f, "migration.tf.parked")
            if os.path.exists(src):
                os.rename(src, src[: -len(".parked")])
                log(f"unparked: {f}/migration.tf.parked -> migration.tf")
                unparked += 1
        log(f"unparked {unparked} migration.tf file(s).")
        return 0

    # ---- gates -------------------------------------------------------------
    # Webhook manual-gate warning (plan + apply, target tenants only): printed
    # FIRST, ahead of the migration.tf greenfield guard below, so it's visible
    # even on a run that the guard is about to stop -- webhooks have no
    # Terraform provider resource, so this is advisory, never a refusal.
    if not is_source:
        webhooks = webhook_integration_names(config_dir)
        if webhooks:
            log("NOTE: subscription_groups.json references "
                f"{len(webhooks)} webhook integration(s): " + ", ".join(webhooks) +
                ".\nThese are now TERRAFORM-MANAGED (cato_webhook in "
                "0-dependencies/bulk_webhooks.tf, created/imported before subscription groups "
                "via depends_on), so no manual pre-creation is required. The only manual step is "
                "supplying real auth via the webhook_secrets tfvars map -- any webhook without an "
                "entry gets an interim placeholder custom-header auth.")

    # Import blocks evaluate at PLAN time, so a target-tenant tree containing
    # migration.tf (source-tenant import ids) fails with a wall of confusing
    # "Cannot import non-existent remote object" errors. Refuse up front with
    # one clear message instead -- for BOTH plan and apply modes.
    if not is_source:
        offenders = [f for f in folders
                     if os.path.exists(os.path.join(REPO_ROOT, f, "migration.tf"))]
        if offenders and not args.allow_target_migrations:
            log("REFUSED (greenfield guard): these folders contain migration.tf, whose "
                "import blocks carry SOURCE-tenant ids and cannot run against a target "
                "tenant (import blocks evaluate at plan time):\n  " + "\n  ".join(offenders) +
                "\nPark them first: python3 scripts/bulk_apply.py --profile "
                f"{args.profile} --park-migrations"
                "\nOR, if config_data was re-exported from THIS target and you are testing "
                "brownfield import INTO it, re-run with --allow-target-migrations."
                "\n(see docs/teardown-and-replication-walkthrough.md step B4)")
            return 2
        if offenders and args.allow_target_migrations:
            log("NOTE (--allow-target-migrations): running with migration.tf present against "
                f"target account {account_id}. This assumes config_data holds THIS target's "
                "object ids (import blocks will adopt existing live objects). Imports are "
                "skipped for resources already in state -- clear/aside each folder's "
                "terraform.tfstate first for the import to actually run:\n  " +
                "\n  ".join(offenders))
        # Parked migrations against a target = GREENFIELD CREATE, not import. The
        # import blocks only run when migration.tf is ACTIVE (unparked); with them
        # parked, plan/apply create fresh objects (imp=0) instead of adopting the
        # live ones -- the #1 "why didn't it import?" footgun.
        if args.mode in ("plan", "apply", "deploy"):
            parked = [f for f in folders
                      if os.path.exists(os.path.join(REPO_ROOT, f, "migration.tf.parked"))
                      and not os.path.exists(os.path.join(REPO_ROOT, f, "migration.tf"))]
            if parked:
                log("WARNING: PARKED migrations -- this run GREENFIELD-CREATES these folders' "
                    "objects, it does NOT import the live ones (imp=0):\n  " + "\n  ".join(parked) +
                    "\nTo adopt the live config instead, unpark first, then re-run with the flag:\n"
                    f"  python3 scripts/bulk_apply.py --profile {args.profile} --unpark-migrations\n"
                    f"  python3 scripts/bulk_apply.py --profile {args.profile} --mode {args.mode} --allow-target-migrations")
        # destroy is the exception: it REQUIRES state (there's nothing to tear
        # down without it), so the stateless-tree expectation doesn't apply.
        if args.mode != "destroy":
            stateful = [f for f in folders
                        if os.path.exists(os.path.join(REPO_ROOT, f, "terraform.tfstate"))]
            if stateful:
                log("WARNING: terraform.tfstate present in: " + ", ".join(stateful) +
                    " -- target-tenant replication expects a stateless tree. With migration.tf "
                    "ACTIVE, import blocks are SKIPPED for any resource already in state, so a "
                    "populated state silently turns an intended import into a no-op / partial "
                    "greenfield. Clear or set each folder's state aside first.")

    if not args.skip_validate:
        validate_cmd = [sys.executable, os.path.join(REPO_ROOT, "scripts", "validate_config_data.py"),
                        "--config-data", config_dir]
        # Greenfield target (non-source tenant, every selected migration.tf
        # parked): no import block will run, so a --strip-ids export's missing
        # source-tenant ids are expected, not errors. Without this the documented
        # greenfield path (export all --strip-ids -> bulk_apply) fails its own
        # validation gate with one "missing id" ERROR per object.
        greenfield = (not is_source and not any(
            os.path.exists(os.path.join(REPO_ROOT, f, "migration.tf")) for f in folders))
        if greenfield:
            validate_cmd.append("--greenfield")
        log("gate: scripts/validate_config_data.py" + (" --greenfield" if greenfield else ""))
        result = run(validate_cmd, REPO_ROOT, os.environ.copy())
        if result.returncode != 0:
            log("ABORT: config_data validation failed (ERROR severity). Fix exports first.")
            return 1

    if args.mode in ("apply", "deploy", "destroy"):
        if is_source and not args.source_tenant_apply:
            verb = "destroying" if args.mode == "destroy" else "applying"
            log(f"REFUSED: {verb} against a protected SOURCE tenant requires --source-tenant-apply.")
            return 2
        # (migration.tf greenfield guard already enforced above for both modes)
        if not confirm_account(account_id, args.confirmed_account):
            return 2

    if args.purge_first:
        log("purge preview: scripts/purge_all.py --dry-run (never executes deletions from here)")
        result = run([sys.executable, os.path.join(REPO_ROOT, "scripts", "purge_all.py"),
                      "--profile", args.profile, "--dry-run", "--config-dir", config_dir],
                     REPO_ROOT, os.environ.copy())
        if result.returncode not in (0,):
            log("ABORT: purge dry-run failed or was refused.")
            return 1

    if args.preflight_source:
        log(f"preflight: scripts/preflight_check.py --source-profile {args.preflight_source} "
            f"--target-profile {args.profile} (read-only)")
        result = run([sys.executable, os.path.join(REPO_ROOT, "scripts", "preflight_check.py"),
                      "--source-profile", args.preflight_source, "--target-profile", args.profile,
                      "--config-dir", config_dir],
                     REPO_ROOT, os.environ.copy())
        if result.returncode == 2 and not args.ignore_preflight:
            log("ABORT: preflight found BLOCKERs (see preflight/preflight_report.md). Fix them, scope the "
                "policies to the target (scripts/scope_policies_to_target.py), or pass --ignore-preflight.")
            return 1
        if result.returncode not in (0, 1, 2):
            log(f"ABORT: preflight_check.py failed (exit {result.returncode}).")
            return 1

    # ---- terraform env -----------------------------------------------------
    env = terraform_env(args.profile)
    # Point every folder's config_data_dir variable at the resolved (absolute)
    # config dir, so --config-dir redirects all the folders' file() reads.
    env["TF_VAR_config_data_dir"] = config_dir
    # wan-network and remote-port-forward read their files through their own
    # *_json_file_path variables (repo-relative defaults), NOT config_data_dir,
    # so without these --config-dir silently did not redirect those two folders.
    env["TF_VAR_wnw_rules_json_file_path"] = os.path.join(config_dir, "all_wnw_rules_and_sections.json")
    env["TF_VAR_rpf_rules_json_file_path"] = os.path.join(config_dir, "all_rpf_rules_and_sections.json")
    env["TF_VAR_allocated_ips_json_file_path"] = os.path.join(config_dir, "allocated_ips.json")
    if args.mode in ("plan", "deploy", "destroy"):
        # The provider's cleanupDrafts fires a policyPrivateAccessDiscardRevision
        # mutation on first Configure (see docs/release-instructions.md) -- it can
        # discard a human's in-progress CMA draft. The plan phase must never mutate;
        # deploy keeps it set through its apply phase too (not auto-discarding
        # drafts is the safe default for a dev deploy).
        env["DISABLE_POLICY_RULE_CLEANUP"] = "true"
    os.makedirs(args.plans_dir, exist_ok=True)
    if not args.allow_dev_overrides:
        # Terraform refuses to CREATE plugin_cache_dir itself -- make sure it
        # exists or init fails on a fresh clone/machine.
        os.makedirs(os.path.expanduser("~/.terraform.d/plugin-cache"), exist_ok=True)
        tfrc = os.path.join(args.plans_dir, "clean.tfrc")
        # Default: resolve the pinned provider straight from the public registry
        # (it is published), no dev_overrides. Pass --mirror only for a LOCAL
        # provider build -- that serves the version-tagged binary from
        # ~/.terraform.d/mirror (where `make install-mirror` writes it) via a
        # filesystem_mirror instead of the registry.
        if args.mirror:
            mirror_path = os.path.expanduser("~/.terraform.d/mirror")
            with open(tfrc, "w") as f:
                f.write(MIRROR_TFRC_TEMPLATE.format(mirror_dir=mirror_path))
            env["TF_CLI_CONFIG_FILE"] = tfrc
            log(f"provider resolution: pinned version via LOCAL filesystem mirror "
                f"{mirror_path} (TF_CLI_CONFIG_FILE={tfrc})")
        else:
            with open(tfrc, "w") as f:
                f.write(REGISTRY_TFRC_TEMPLATE)
            env["TF_CLI_CONFIG_FILE"] = tfrc
            log(f"provider resolution: pinned version from the public registry "
                f"(direct), no dev_overrides (TF_CLI_CONFIG_FILE={tfrc})")
    else:
        log("provider resolution: using your ~/.terraformrc (dev_overrides may apply!)")

    # ---- SCIM push (FIRST, before any folder; opt-in via --include-scim) --------
    # The SCIM users/groups from config_data/scim_*.csv must exist in the target
    # before any folder that resolves usersGroup/user names (3-groups and every
    # 4-policies/* family). They depend on NOTHING Terraform creates, so when
    # --include-scim is passed the push runs before the first folder -- a folder
    # failure under the default stop-on-first-failure can then never skip it.
    # Without the flag SCIM is untouched (the import is create-only, so an
    # unconditional push would fail as duplicates on any non-empty target).
    if args.mode in ("apply", "deploy") and not is_source:
        if args.include_scim:
            if not run_scim_push(args.profile, account_id, config_dir):
                return 1
        else:
            log("\nGATE (SCIM): skipped (--include-scim not set) -- assuming the target's "
                "users/groups already exist. "
                + str(len(SCIM_DEPENDENT_FAMILIES)) + " families resolve usersGroup/user "
                "names at apply: " + ", ".join(SCIM_DEPENDENT_FAMILIES) + ".")

    # ---- per-folder loop ---------------------------------------------------
    results = []
    failed = False
    for folder in folders:
        folder_path = os.path.join(REPO_ROOT, folder)
        # folder may contain "/" (e.g. "4-policies/internet-firewall") -- flatten
        # for the plan filename so it stays a single file directly under
        # .bulk_plans/ rather than requiring a nested subdirectory.
        plan_file = os.path.join(os.path.abspath(args.plans_dir), f"{folder.replace('/', '__')}.tfplan")

        log(f"\n=== {folder} ({args.mode}) ===")

        if args.mode in ("plan", "deploy"):
            if args.mirror:  # plan AND deploy: a lock from the registry build never matches a local build
                # Only when exercising a LOCAL provider build (--mirror): a
                # rebuild changes the binary hash, so every folder's committed
                # .terraform.lock.hcl (pinned to the same version) no longer
                # matches -- and `init -upgrade` refuses a same-version/new-hash
                # swap ("doesn't match any of the checksums previously
                # recorded"). Regenerate the lock from the mirror each run
                # instead of failing. The default (published-registry) path
                # keeps its committed lock -- that path is credible and
                # reproducible against the pinned registry version.
                lock = os.path.join(folder_path, ".terraform.lock.hcl")
                if os.path.exists(lock):
                    os.remove(lock)
            init_cmd = ["terraform", "init", "-input=false"]
            if args.upgrade:
                init_cmd.append("-upgrade")
            steps = [
                (init_cmd, "init"),
                (["terraform", "validate"], "validate"),
                (["terraform", "plan", "-input=false",
                  f"-parallelism={args.parallelism}", f"-out={plan_file}"], "plan"),
            ]
            status = "ok"
            for cmd, label in steps:
                result = run(cmd, folder_path, env)
                if result.returncode != 0:
                    status = f"FAILED ({label})"
                    failed = True
                    break
            counts = plan_summary(folder_path, plan_file, env) if status == "ok" else None
            if status == "ok":
                # Record who planned: a saved plan embeds the plan-time
                # credentials/variables, so apply-mode must verify the same
                # account is being targeted.
                with open(plan_file + ".meta", "w") as f:
                    json.dump({"profile": args.profile, "account_id": account_id}, f)
            # deploy: apply the plan we JUST created, in the same invocation. There
            # is no gap, so none of the saved-plan guards (stale-plan, profile
            # mismatch, inconsistent-lock) can fire -- init -upgrade above already
            # restored providers + lock. This is the seamless path for disposable-
            # state test tenants (clear -> purge -> tfcleardir -> deploy).
            if status == "ok" and args.mode == "deploy":
                apply_res = run(["terraform", "apply", "-input=false",
                                 f"-parallelism={args.parallelism}", plan_file],
                                folder_path, env)
                status = "applied" if apply_res.returncode == 0 else "FAILED (apply)"
                if apply_res.returncode != 0:
                    failed = True
            results.append((folder, status, counts))
            if failed and not args.keep_going:
                break
        elif args.mode == "destroy":
            # Tear the folder down. init first (same reasoning as apply: restore
            # providers + lock so a drifted .terraform/lock still works), then
            # `terraform destroy -auto-approve`. No saved plan is
            # involved, so none of the saved-plan guards apply -- the TTY +
            # typed-account-id + <source-profile>-block guards above already gated this.
            if not os.path.exists(os.path.join(folder_path, "terraform.tfstate")):
                # Nothing to destroy -- an empty/never-applied folder. Skip
                # rather than failing the whole run (reverse order means later
                # folders may still hold state).
                log(f"skipped: no terraform.tfstate in {folder} -- nothing to destroy.")
                results.append((folder, "no state", None))
                continue
            destroy_init = ["terraform", "init", "-input=false"]
            if args.upgrade:
                # Re-resolve the provider against current versions.tf constraints and
                # rewrite a stale committed lock (e.g. one still pinning
                # 1.5.0-prerelease.3 after the 2.0.0-prerelease.1 bump). Without this,
                # a plain init fails with "no available releases match the given
                # constraints" when the locked version no longer satisfies the config.
                destroy_init.append("-upgrade")
            init_result = run(destroy_init, folder_path, env)
            if init_result.returncode != 0:
                results.append((folder, "FAILED (destroy-init)", None))
                failed = True
                break
            result = run(["terraform", "destroy", "-input=false", "-auto-approve",
                          f"-parallelism={args.parallelism}"],
                         folder_path, env)
            if result.returncode != 0:
                results.append((folder, "FAILED (destroy)", None))
                failed = True
                break
            results.append((folder, "destroyed", None))
        else:  # apply
            if not os.path.exists(plan_file):
                log(f"REFUSED: no saved plan for {folder} -- run --mode plan first.")
                results.append((folder, "REFUSED (no plan)", None))
                failed = True
                break
            if os.path.getmtime(plan_file) < newest_input_mtime(folder_path, config_dir):
                log(f"REFUSED: {folder}'s plan file is older than its inputs -- re-plan first.")
                results.append((folder, "REFUSED (stale plan)", None))
                failed = True
                break
            # A saved plan embeds the credentials/variables captured at plan
            # time -- applying it under a different profile would mutate the
            # PLAN-TIME tenant, not the one named here. Refuse any mismatch.
            meta_file = plan_file + ".meta"
            try:
                with open(meta_file) as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                meta = None
            if not meta or meta.get("account_id") != account_id:
                planned_for = (meta or {}).get("account_id", "unknown")
                log(f"REFUSED: {folder}'s plan was created for account {planned_for}, "
                    f"but --profile {args.profile!r} resolves to {account_id}. "
                    f"Re-plan with this profile first.")
                results.append((folder, "REFUSED (profile mismatch)", None))
                failed = True
                break
            # A saved plan references the provider but does NOT carry the
            # installed plugin or lock. plan and apply are separate invocations,
            # so `.terraform/providers` + `.terraform.lock.hcl` may be absent or
            # drifted at apply time (account switches, lock clears, the
            # plugin_cache_dir producing fragile h1-only locks). Restore them
            # first -- a plain `init` (NOT -upgrade) reinstalls
            # the pinned provider per the existing/deterministic lock without
            # changing the selection, so the saved plan stays applicable. Without
            # this, apply fails with "Inconsistent dependency lock file ... no
            # version is selected".
            apply_init = ["terraform", "init", "-input=false"]
            if args.upgrade:
                # Opt-in: re-resolve/rewrite a stale lock. NOTE this can change the
                # provider selection vs the one the saved plan was built against; only
                # pass --upgrade in apply mode when you intend that (e.g. after a
                # provider-version bump), otherwise keep the default (no -upgrade) so
                # the saved plan stays applicable.
                apply_init.append("-upgrade")
            init_result = run(apply_init, folder_path, env)
            if init_result.returncode != 0:
                results.append((folder, "FAILED (apply-init)", None))
                failed = True
                break
            result = run(["terraform", "apply", "-input=false",
                          f"-parallelism={args.parallelism}", plan_file],
                         folder_path, env)
            if result.returncode != 0:
                results.append((folder, "FAILED (apply)", None))
                failed = True
                break
            results.append((folder, "applied", None))

    # ---- summary -----------------------------------------------------------
    log("\n" + "=" * 72)
    log(f"{'folder':<28} {'status':<18} add  chg  del  imp")
    log("-" * 72)
    for folder, status, counts in results:
        if counts:
            log(f"{folder:<28} {status:<18} "
                f"{counts['add']:>3}  {counts['change']:>3}  "
                f"{counts['destroy']:>3}  {counts['import']:>3}")
        else:
            log(f"{folder:<28} {status:<18}   -    -    -    -")
    skipped = [f for f in folders if f not in [r[0] for r in results]]
    if skipped:
        log(f"not reached (stop-on-failure): {', '.join(skipped)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
