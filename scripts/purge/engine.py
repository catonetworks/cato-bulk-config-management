"""Shared purge engine: enumerate-then-delete for one purgeable type.

Used by the thin per-type wrappers (scripts/purge/purge_<type>.py) and by
scripts/purge_all.py. Enumerates live objects from the TARGET tenant via
`catocli export <type>`, then deletes them via the family's catocli
mutations, then publishes the policy revision (policy families only).

Safety model (shared with scim_sync.py / bulk_apply.py):
  - --profile is REQUIRED and resolved via scripts/cato_profile.py; the
    shell's CATO_PROFILE is ignored.
  - HARD BLOCKLIST, no override flag: profiles named <source-profile> /
    <source-profile-readonly>, or ANY profile resolving to account <SOURCE_ACCOUNT_ID> (the
    source of truth). Exit 2.
  - --dry-run is the DEFAULT and is read-only (one export call). --execute
    requires a TTY and an interactively typed account id.
  - per-item failures are tolerated and reported (deleted / skipped /
    failed); exit 1 if anything failed, 0 otherwise.
  - sections named like catch-all/default are skipped by NAME convention
    (exports carry no system flag); true system rules the API refuses to
    delete surface as `failed` with the API error -- that is the expected
    end state ("system defaults remain").
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cato_profile import catocli_env, get_profile, load_protected, warn_if_unprotected  # noqa: E402
from purge.families import DepType, PolicyFamily, lookup  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Repo convention: all catocli export data stays under config_data/ in the
# working directory -- never system temp dirs. Enumeration snapshots are
# gitignored and left on disk as the record of what each purge run saw.
# Overridable via set_config_dir() (wired to --config-dir).
SNAPSHOT_DIR = os.path.join(REPO_ROOT, "config_data", "purge_snapshots")

# Configurable source-tenant guard -- defaults to EMPTY (nothing protected)
# until the operator declares a source tenant via cbcm.config.json or the
# CBCM_PROTECTED_ACCOUNTS / CBCM_PROTECTED_PROFILES env vars. There is NO
# override flag -- a declared protected account/profile can never be purged.
PROTECTED_ACCOUNTS, PROTECTED_PROFILES = load_protected()


def set_config_dir(config_dir):
    """Point the enumeration-snapshot dir at <config_dir>/purge_snapshots."""
    global SNAPSHOT_DIR
    SNAPSHOT_DIR = os.path.join(os.path.abspath(config_dir), "purge_snapshots")
    return SNAPSHOT_DIR

# Section names that are never deleted (catch-all / platform defaults).
PROTECTED_SECTION_MARKERS = ("catch all", "catch-all", "default")


def log(msg):
    print(msg, file=sys.stderr)


def guard_profile(profile_name):
    """Resolve the profile and refuse the source tenant. Returns profile dict."""
    warn_if_unprotected(PROTECTED_ACCOUNTS, PROTECTED_PROFILES, action="purge")
    if profile_name in PROTECTED_PROFILES:
        log(f"REFUSED: {profile_name!r} is a protected source-of-truth tenant profile.")
        sys.exit(2)
    profile = get_profile(profile_name)
    if profile["account_id"] in PROTECTED_ACCOUNTS:
        log(f"REFUSED: profile {profile_name!r} resolves to protected account "
            f"{profile['account_id']} (a declared source of truth).")
        sys.exit(2)
    return profile


def confirm_account(account_id, preconfirmed=None):
    if preconfirmed is not None:
        if preconfirmed == account_id:
            log(f"account {account_id} pre-confirmed.")
            return True
        log(f"REFUSED: pre-confirmed {preconfirmed!r} != resolved account {account_id}.")
        return False
    if not sys.stdin.isatty():
        log("REFUSED: --execute requires an interactive terminal (no CI purges).")
        return False
    typed = input(f"Type the target account id ({account_id}) to confirm purge: ").strip()
    if typed != account_id:
        log(f"REFUSED: typed {typed!r} != resolved account {account_id}.")
        return False
    return True


def run_catocli(profile_name, *args):
    """Returns (ok, output)."""
    result = subprocess.run(
        ["catocli", *args], env=catocli_env(profile_name),
        capture_output=True, text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    ok = result.returncode == 0
    if ok:
        # catocli exits 0 even on GraphQL / API errors. Parse the body when it is
        # JSON and fail on a genuine error shape (a substring scan would
        # false-positive on legitimate payload fields):
        #   - {"errors": [...]}                   GraphQL errors (plural)
        #   - {"error": "...", "success": false}  catocli wrapper error, e.g.
        #                                         "permission denied" on export
        # NOTE: a bare {"success": false, "message": "No sites found ..."} is NOT
        # an error -- it's an empty result; keyed on `error`/`errors`, not on
        # `success`, so empty-account exports are not treated as failures.
        try:
            body = json.loads(result.stdout or "")
        except ValueError:
            body = None
        if isinstance(body, dict) and (body.get("errors") or body.get("error")):
            ok = False
    return ok, output.strip()


def export_json(profile_name, export_type):
    snap_dir = os.path.join(SNAPSHOT_DIR, profile_name)
    os.makedirs(snap_dir, exist_ok=True)
    filename = f"{export_type}.json"
    path = os.path.join(snap_dir, filename)
    if os.path.exists(path):
        os.remove(path)  # stale snapshot from a prior run
    ok, output = run_catocli(
        profile_name, "export", export_type,
        "--output-directory", snap_dir, "--json-filename", filename,
    )
    if not ok:
        raise RuntimeError(f"catocli export {export_type} failed:\n{output}")
    if not os.path.exists(path):
        # Reported OK but wrote no file -- an empty result (e.g. catocli's
        # "No sites found in account"). Real errors (permission denied, etc.)
        # carry an error key and were already caught above as not-ok, so a
        # missing file here means zero objects.
        return []
    with open(path) as f:
        return json.load(f)


def is_protected_section(name):
    lowered = (name or "").lower()
    return any(marker in lowered for marker in PROTECTED_SECTION_MARKERS)


# ---------------------------------------------------------------------------
# enumeration


def enumerate_policy(data, family: PolicyFamily):
    """Returns (rules, firewall_rules, sub_policies, sections) as lists of (id, name)."""
    # catocli export normally writes {"data":{"policy":{...}}}, but has been seen
    # to wrap the GraphQL response in a single-element list ([{"data":{...}}]).
    # Unwrap it, and treat any other unexpected shape as an empty policy so one
    # odd export can't crash the whole purge run (the family re-exports next run).
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), {})
    if not isinstance(data, dict):
        log(f"  WARNING: unexpected export shape for {family.key} "
            f"({type(data).__name__}); treating as empty policy.")
        return [], [], [], []
    policy = (data.get("data", {}).get("policy", {})
              .get(family.family, {}).get("policy", {})) or {}
    rules, fw_rules = [], []
    rule_key = "network_rules" if family.nested_firewall else "rules"
    for wrapper in policy.get(rule_key) or []:
        rule = (wrapper or {}).get("rule") or {}
        if rule.get("id"):
            rules.append((rule["id"], rule.get("name", "?")))
        if family.nested_firewall:
            for fw_wrapper in wrapper.get("firewall_rules") or []:
                fw_rule = (fw_wrapper or {}).get("rule") or {}
                if fw_rule.get("id"):
                    fw_rules.append((fw_rule["id"], fw_rule.get("name", "?")))
    # Sub-policies (Internet Firewall, WAN Firewall, and socketLan/LAN Firewall)
    # live at policy.sub_policies[] with a flat {sub_policy_id, sub_policy_name}
    # shape -- removeSubPolicy cascades the sub-policy's scope rule and inner rules.
    # Enumerated for EVERY family, not just nested_firewall: IFW/WF sub-policies
    # otherwise survive a purge, and a re-apply then collides with
    # ruleNameAlreadyExists when cato_if_sub_policy/cato_wf_sub_policy recreate
    # them. Families without sub-policy support simply have no sub_policies[] key.
    sub_policies = []
    for sp in policy.get("sub_policies") or []:
        sp_id = (sp or {}).get("sub_policy_id")
        if sp_id:
            sub_policies.append((sp_id, sp.get("sub_policy_name", "?")))
    sections = [
        (s["section_id"], s.get("section_name", "?"))
        for s in policy.get("sections") or []
        if s.get("section_id")
    ]
    return rules, fw_rules, sub_policies, sections


def enumerate_dep(data, dep: DepType):
    if isinstance(data, list):
        # Flat list, or [] returned by export_json for an empty result.
        items = data
    elif dep.list_key:
        # Export nests its array under a named key (e.g. sites -> {"sites":[...]}).
        items = (data or {}).get(dep.list_key) if isinstance(data, dict) else None
    else:
        items = (data or {}).get("data", None)
    if not isinstance(items, list):
        # Fail loudly: a silent [] here would make dry-run report "0 objects"
        # for a tenant that actually has them, corrupting the purge preview.
        raise RuntimeError(
            f"unrecognized export shape for {dep.key!r} "
            f"(expected a JSON array, got {type(data).__name__})"
        )
    return [(i["id"], i.get("name", "?")) for i in items if isinstance(i, dict) and i.get("id")]


def is_protected_dep(dep: DepType, obj_id):
    """System objects that cannot (and must not) be deleted -- reported as skipped,
    the same accepted 'system defaults remain' end state as catch-all sections.
    System mailing lists (e.g. 'All Admins') carry negative ids."""
    return dep.key == "mailing_lists" and str(obj_id).startswith("-")


# ---------------------------------------------------------------------------
# deletion


class Report:
    def __init__(self, key):
        self.key = key
        self.deleted, self.skipped, self.failed = [], [], []

    def record(self, kind, label, ok, detail=""):
        entry = {"kind": kind, "name": label, "detail": detail}
        (self.deleted if ok else self.failed).append(entry)

    def skip(self, kind, label, reason):
        self.skipped.append({"kind": kind, "name": label, "detail": reason})

    @property
    def exit_code(self):
        return 1 if self.failed else 0

    def summary_line(self):
        return (f"{self.key:<26} deleted={len(self.deleted):<4} "
                f"skipped={len(self.skipped):<3} failed={len(self.failed)}")

    def as_dict(self):
        return {"type": self.key, "deleted": self.deleted,
                "skipped": self.skipped, "failed": self.failed}


def purge_policy_family(profile_name, family: PolicyFamily, report: Report):
    data = export_json(profile_name, family.export_type)
    rules, fw_rules, sub_policies, sections = enumerate_policy(data, family)

    # LAN firewall rules first (nested under network rules), then rules,
    # then sub-policies, then sections -- children before containers.
    for rule_id, name in fw_rules:
        payload = {family.wrapper_key: {},
                   "socketLanFirewallRemoveRuleInput": {"id": rule_id}}
        ok, out = run_catocli(profile_name, *family.mutation_path, "firewall",
                              "removeRule", json.dumps(payload))
        report.record("firewall_rule", name, ok, "" if ok else out[-300:])

    for rule_id, name in rules:
        payload = {family.wrapper_key: {},
                   family.remove_rule_key: {"id": rule_id}}
        ok, out = run_catocli(profile_name, *family.mutation_path,
                              "removeRule", json.dumps(payload))
        report.record("rule", name, ok, "" if ok else out[-300:])

    # Sub-policies (IFW, WF, socketLan): removeSubPolicy cascades the sub-policy's
    # scope rule (which can reference dependency objects like global IP ranges) and
    # its inner rules. The input key is per-family (internetFirewall/wanFirewall/
    # socketLanRemoveSubPolicyInput) -- hardcoding socketLan skipped IFW/WF sub-
    # policies, which then collided with ruleNameAlreadyExists on the next apply.
    for sp_id, name in sub_policies:
        payload = {family.wrapper_key: {},
                   family.remove_sub_policy_key: {
                       "ref": {"by": "ID", "input": sp_id}}}
        ok, out = run_catocli(profile_name, *family.mutation_path,
                              "removeSubPolicy", json.dumps(payload))
        report.record("sub_policy", name, ok, "" if ok else out[-300:])

    for section_id, name in sections:
        # ATTEMPT every section delete -- a name-based pre-skip ("catch all"/
        # "default") wrongly preserved OUR OWN exported sections (e.g. IFW
        # "Catch All", WNW "Default Rules"), making redeploys collide with
        # sectionNameAlreadyExists. Genuine system sections refuse deletion at
        # the API; classify that refusal as the skip instead.
        payload = {family.wrapper_key: {},
                   "policyRemoveSectionInput": {"id": section_id}}
        ok, out = run_catocli(profile_name, *family.mutation_path,
                              "removeSection", json.dumps(payload))
        if not ok and is_protected_section(name):
            report.skip("section", name, "API refused deletion (system default section)")
            continue
        report.record("section", name, ok, "" if ok else out[-300:])

    if report.deleted:
        payload = {family.wrapper_key: {},
                   "policyPublishRevisionInput": {
                       "name": "bulk purge",
                       "description": "scripts/purge engine"}}
        ok, out = run_catocli(profile_name, *family.mutation_path,
                              "publishPolicyRevision", json.dumps(payload))
        if not ok:
            report.record("publish", f"{family.family} revision", False, out[-300:])
        else:
            report.record("publish", f"{family.family} revision", True)


def _delete_and_verify(profile_name, dep, singular, deletes, report):
    """Record delete results, reclassifying a 'failed' delete as success when the
    object is actually gone.

    The Cato `notification` service returns an invalid response body (a non-null
    ActorRef under audit, or a DOWNSTREAM_SERVICE_ERROR "Response is not valid"
    when echoing a just-deleted group's nested mailingList/integration) AFTER the
    delete mutation has already committed. run_catocli then reports failure even
    though the object was deleted -- which stop-on-failure turns into a spurious
    abort. So for any reported failure, re-enumerate the type ONCE and treat an
    id that no longer exists as a verified-successful delete. A genuine failure
    (permission denied, still-present object) stays failed.

    deletes: list of (obj_id, name, ok, out).
    """
    failed = [(oid, name, out) for oid, name, ok, out in deletes if not ok]
    gone = set()
    if failed:
        try:
            data = export_json(profile_name, dep.export_type)
            surviving = {str(oid) for oid, _ in enumerate_dep(data, dep)}
            gone = {str(oid) for oid, _, _ in failed if str(oid) not in surviving}
        except RuntimeError as exc:
            log(f"  (verify-after-delete re-export failed, keeping failures as-is: {exc})")
    for oid, name, ok, out in deletes:
        if ok or str(oid) in gone:
            report.record(singular, name, True, "")
            if not ok:
                log(f"  {singular} {name}: delete response errored but object is "
                    f"gone -- counted as deleted (verified by re-enumeration).")
        else:
            report.record(singular, name, False, out[-300:])


def purge_dep_type(profile_name, dep: DepType, report: Report):
    data = export_json(profile_name, dep.export_type)
    items = enumerate_dep(data, dep)
    if not items:
        return

    if dep.key == "global_ip_ranges":
        payload = {"globalIpRangeRefInput": [
            {"by": "ID", "input": str(obj_id)} for obj_id, _ in items]}
        ok, out = run_catocli(profile_name, "mutation", "object",
                              "deleteGlobalIpRangeBulk", json.dumps(payload))
        for _, name in items:
            report.record("global_ip_range", name, ok, "" if ok else out[-300:])
    elif dep.key == "dhcp_relay_groups":
        # deleteRelayGroup takes a LIST of ids, but delete per-id (single-element
        # list) rather than one bulk call: a bulk delete is all-or-nothing, so one
        # bad/stale id aborts the whole batch and leaves every relay group behind.
        # Per-id also lets the network-config service return a committed-but-errored
        # response for a single group without failing the others;
        # verify-after-delete (re-export,
        # treat an id that's gone as a success) absorbs that cosmetic error the same
        # way the notification-service deletes above do.
        deletes = []
        for obj_id, name in items:
            payload = {"networkConfigDhcpDeleteRelayGroupInput": {
                "id": [str(obj_id)]}}
            ok, out = run_catocli(profile_name, "mutation", "networkConfig", "dhcp",
                                  "deleteRelayGroup", json.dumps(payload))
            deletes.append((obj_id, name, ok, out))
        _delete_and_verify(profile_name, dep, "dhcp_relay_group", deletes, report)
    elif dep.key == "groups":
        for obj_id, name in items:
            payload = {"groupRefInput": {"by": "ID", "input": str(obj_id)}}
            ok, out = run_catocli(profile_name, "mutation", "groups",
                                  "deleteGroup", json.dumps(payload))
            report.record("group", name, ok, "" if ok else out[-300:])
    elif dep.key == "custom_applications":
        for obj_id, name in items:
            payload = {"deleteCustomApplicationInput": {
                "customApplication": {"by": "ID", "input": str(obj_id)}}}
            ok, out = run_catocli(profile_name, "mutation", "customAppData",
                                  "deleteCustomApplication", json.dumps(payload))
            report.record("custom_application", name, ok, "" if ok else out[-300:])
    elif dep.key == "sites":
        # removeSite cascades the site's network ranges / interfaces / wifi /
        # static hosts. Groups (site-scoped members) are already purged (earlier tier).
        for obj_id, name in items:
            payload = {"siteId": str(obj_id)}
            ok, out = run_catocli(profile_name, "mutation", "site",
                                  "removeSite", json.dumps(payload))
            report.record("site", name, ok, "" if ok else out[-300:])
    elif dep.key == "subscription_groups":
        # notification deletes return an invalid response body after the mutation
        # commits -> verify-after-delete so a cosmetic response error isn't a
        # spurious failure that aborts the run.
        deletes = []
        for obj_id, name in items:
            payload = {"subscriptionGroupRefInput": {"by": "ID", "input": str(obj_id)}}
            ok, out = run_catocli(profile_name, "mutation", "notification",
                                  "deleteSubscriptionGroup", json.dumps(payload))
            deletes.append((obj_id, name, ok, out))
        _delete_and_verify(profile_name, dep, "subscription_group", deletes, report)
    elif dep.key == "mailing_lists":
        deletes = []
        for obj_id, name in items:
            if is_protected_dep(dep, obj_id):
                report.skip("mailing_list", name, "system list (not deletable)")
                continue
            payload = {"mailingListRefInput": {"by": "ID", "input": str(obj_id)}}
            ok, out = run_catocli(profile_name, "mutation", "notification",
                                  "deleteMailingList", json.dumps(payload))
            deletes.append((obj_id, name, ok, out))
        # deleteMailingList has the same invalid-response-after-commit behavior.
        _delete_and_verify(profile_name, dep, "mailing_list", deletes, report)
    elif dep.key == "webhooks":
        # notification `integration deleteWebhook` is in the same service as the
        # mailing-list/subscription-group deletes, so it can return an invalid
        # response body after the mutation commits -> verify-after-delete so a
        # cosmetic response error isn't a spurious failure that aborts the run.
        deletes = []
        for obj_id, name in items:
            payload = {"webhookIntegrationRefInput": {"by": "ID", "input": str(obj_id)}}
            ok, out = run_catocli(profile_name, "mutation", "notification", "integration",
                                  "deleteWebhook", json.dumps(payload))
            deletes.append((obj_id, name, ok, out))
        _delete_and_verify(profile_name, dep, "webhook", deletes, report)
    elif dep.key in ("fqdn_containers", "ip_range_containers"):
        singular = dep.key[:-1]  # fqdn_container / ip_range_container
        for obj_id, name in items:
            payload = {"deleteContainerInput": {
                "ref": {"by": "ID", "input": str(obj_id)}}}
            ok, out = run_catocli(profile_name, "mutation", "container",
                                  "delete", json.dumps(payload))
            report.record(singular, name, ok, "" if ok else out[-300:])
    elif dep.key == "private_apps":
        for obj_id, name in items:
            payload = {"deletePrivateApplicationInput": {
                "privateApplication": {"by": "ID", "input": str(obj_id)}}}
            ok, out = run_catocli(profile_name, "mutation", "privateApplication",
                                  "deletePrivateApplication", json.dumps(payload))
            report.record("private_app", name, ok, "" if ok else out[-300:])
    elif dep.key == "app_connectors":
        for obj_id, name in items:
            payload = {"removeZtnaAppConnectorInput": {
                "ztnaAppConnector": {"by": "ID", "input": str(obj_id)}}}
            ok, out = run_catocli(profile_name, "mutation", "ztnaAppConnector",
                                  "removeZtnaAppConnector", json.dumps(payload))
            report.record("app_connector", name, ok, "" if ok else out[-300:])
    elif dep.key == "allocated_ips":
        # removeAllocatedIp keys on the IP (not the id); read it from the raw
        # export rather than the (id, name) enumeration.
        for item in (data if isinstance(data, list) else []):
            ip = (item or {}).get("ip")
            name = (item or {}).get("name") or ip or "?"
            if not ip:
                report.record("allocated_ip", name, False, "export item missing ip")
                continue
            payload = {"popLocationRemoveAllocatedIpInput": {
                "ip": ip, "type": (item.get("allocationType") or "SYSTEM")}}
            ok, out = run_catocli(profile_name, "mutation", "popLocationMutations",
                                  "removeAllocatedIp", json.dumps(payload))
            report.record("allocated_ip", name, ok, "" if ok else out[-300:])
    else:
        raise KeyError(dep.key)


# ---------------------------------------------------------------------------
# dry-run inventory


def inventory(profile_name, item):
    """Read-only would-delete listing. Returns (lines, counts)."""
    data = export_json(profile_name, item.export_type)
    lines = []
    if isinstance(item, PolicyFamily):
        rules, fw_rules, sub_policies, sections = enumerate_policy(data, item)
        for _, name in fw_rules:
            lines.append(f"  would delete firewall_rule: {name}")
        for _, name in rules:
            lines.append(f"  would delete rule:          {name}")
        for _, name in sub_policies:
            lines.append(f"  would delete sub_policy:    {name}")
        for _, name in sections:
            if is_protected_section(name):
                lines.append(f"  would attempt section:      {name} (skip if the API refuses as a system default)")
            else:
                lines.append(f"  would delete section:       {name}")
        count = len(rules) + len(fw_rules) + len(sub_policies) + len(sections)
    else:
        items = enumerate_dep(data, item)
        singular = item.key[:-1] if item.key.endswith("s") else item.key
        count = 0
        for obj_id, name in items:
            if is_protected_dep(item, obj_id):
                lines.append(f"  would SKIP {singular}: {name} (system)")
            else:
                lines.append(f"  would delete {singular}: {name}")
                count += 1
    return lines, count


# ---------------------------------------------------------------------------
# entry points


def run_one(profile_name, key, execute):
    item = lookup(key)
    report = Report(key)
    if not execute:
        lines, count = inventory(profile_name, item)
        log(f"[dry-run] {key}: {count} object(s)")
        for line in lines:
            log(line)
        return report
    if isinstance(item, PolicyFamily):
        purge_policy_family(profile_name, item, report)
    else:
        purge_dep_type(profile_name, item, report)
    log(report.summary_line())
    for entry in report.failed:
        log(f"  FAILED {entry['kind']} {entry['name']}: {entry['detail']}")
    return report


def main_single(key):
    """argparse main shared by every thin wrapper script."""
    import argparse
    parser = argparse.ArgumentParser(
        description=f"Purge all {key} objects from a TARGET tenant "
                    f"(dry-run by default; refuses any protected source tenant).")
    parser.add_argument("--profile", required=True,
                        help="catocli profile of the TARGET tenant (shell CATO_PROFILE ignored)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True,
                       help="print the would-delete inventory (default)")
    group.add_argument("--execute", action="store_true",
                       help="actually delete (interactive confirmation required)")
    parser.add_argument("--config-dir", default=os.path.join(REPO_ROOT, "config_data"),
                        help="config_data dir; enumeration snapshots land under "
                             "<config-dir>/purge_snapshots (default: ./config_data)")
    parser.add_argument("--json-report", default=None,
                        help="write the detailed result as JSON to this path")
    args = parser.parse_args()
    set_config_dir(args.config_dir)

    profile = guard_profile(args.profile)
    log(f"target: profile {args.profile!r} -> account {profile['account_id']}")
    if args.execute and not confirm_account(profile["account_id"]):
        return 2

    try:
        report = run_one(args.profile, key, args.execute)
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        return 1
    if args.json_report:
        with open(args.json_report, "w") as f:
            json.dump(report.as_dict(), f, indent=2)
    return report.exit_code
