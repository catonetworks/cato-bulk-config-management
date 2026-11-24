#!/usr/bin/env python3
"""Lint config_data/ for internal consistency and brownfield-import readiness.

Stdlib-only, no network calls. Intended to run both locally (`python3
scripts/validate_config_data.py`) and in CI (see .github/workflows/terraform-validate.yml).

Checks performed (see CHECKS below for the authoritative list) fall into three
severities:
  - ERROR:   structural problems that would break `terraform plan/apply` or a
             future `catocli *_to_tf` import. Exit code becomes 1.
  - WARNING: things that are frequently legitimate in this account (refs to
             built-in/catalog objects, unmanaged sites, non-deterministic
             ordering) but worth a human's attention. Never affect exit code.
  - INFO:    known, expected conditions reported once per file for visibility
             (e.g. rules in the implicit default section). Never affect exit
             code or the PASS/PASS-with-warnings verdict.

Sentinel convention: `section_name: ""` in rules_in_sections[] (and a rule's
section.name) means "the policy's implicit default section" -- a section the
API never lists in policy.sections[]. The empty string (NOT null) is the
canonical sentinel because the sub-policy composite state key
"<sub_policy>::<section>" must render identically in the Python importer
(f-string) and in HCL interpolation; null renders as "None" in Python but ""
in HCL, silently breaking brownfield import address matching. "Default
Section" appears in pre-2026-11 exports (a CLI-synthesized string, removed
since) and is accepted as a legacy equivalent.

Usage:
    python3 scripts/validate_config_data.py [--config-data DIR]

Exit status: 0 if no errors (warnings allowed), 1 if any error was recorded.
"""
import argparse
import csv
import ipaddress
import json
import os
import sys
from collections import Counter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_DATA = os.path.join(REPO_ROOT, "config_data")

# (filename, top-level shape) -- shape is "list" for the flat dependency/static
# arrays, or "policy" for the data.policy.<family>.policy{} rule files, or
# "sites" for socket_sites.json's {"sites": [...]} wrapper.
POLICY_FILES = [
    ("all_ifw_rules_and_sections.json", "internetFirewall"),
    ("all_wf_rules_and_sections.json", "wanFirewall"),
    ("all_wnw_rules_and_sections.json", "wanNetwork"),
    ("all_tls_rules_and_sections.json", "tlsInspect"),
]

# 14 new 2-* policy families (2026-11 bulk coverage expansion). Unlike
# POLICY_FILES above, these are OPTIONAL -- a family's file is only required
# (full structural check, errors affect exit code) once it actually exists in
# config_data/; if it hasn't been exported yet this only produces a WARNING.
# `opts` carries the two known shape deviations from the ifw/wf/wnw/tls norm:
#   - socketLan (LAN Firewall) names its rule list "network_rules", not "rules"
#   - privateAccess orders rules via "rules_in_policy" (no section_name), not
#     "rules_in_sections"
NEW_POLICY_FILES = [
    ("all_lan_fw_rules_and_sections.json", "socketLan", {"rules_key": "network_rules"}),
    ("all_apc_rules_and_sections.json", "applicationControl", {}),
    ("all_rpf_rules_and_sections.json", "remotePortFwd", {}),
    ("all_sb_rules_and_sections.json", "socketBypass", {}),
    ("all_ssia_rules_and_sections.json", "sdpStaticIpAllocation", {}),
    ("all_st_rules_and_sections.json", "splitTunnel", {}),
    ("all_swp_rules_and_sections.json", "siteWebProxy", {}),
    ("all_ts_rules_and_sections.json", "terminalServer", {}),
    ("all_amfh_rules_and_sections.json", "antiMalwareFileHash", {}),
    ("all_ao_rules_and_sections.json", "ztnaAlwaysOn", {}),
    ("all_atr_rules_and_sections.json", "appTenantRestriction", {}),
    ("all_cc_rules_and_sections.json", "clientConnectivity", {}),
    ("all_dia_rules_and_sections.json", "dynamicIpAllocation", {}),
    (
        "all_pa_rules_and_sections.json",
        "privateAccess",
        {"order_key": "rules_in_policy", "order_has_section": False},
    ),
]

DEPENDENCY_LIST_FILES = [
    "groups.json",
    "global_ip_ranges.json",
    "custom_applications.json",
    "fqdn_containers.json",
    "ip_range_containers.json",
    "mailing_lists.json",
    "subscription_groups.json",
]

# New 1-dependencies/ array-of-{id,name,...} type (2026-11). Optional like
# NEW_POLICY_FILES above -- required once it exists, warning until then.
DHCP_RELAY_GROUPS_FILE = "dhcp_relay_groups.json"

# ZTNA / private-access infrastructure exports (2026-11-10): allocated PoP IPs,
# private applications, and app connectors (+groups). Optional-until-present,
# same treatment as DHCP_RELAY_GROUPS_FILE. allocated_ips.json additionally
# enforces (popLocation.name, description) uniqueness -- that pair is the
# cross-tenant identity scripts/remap_allocated_ips.py joins on, so a duplicate
# would make the policy-ref remap ambiguous.
ALLOCATED_IPS_FILE = "allocated_ips.json"
PRIVATE_APPLICATIONS_FILE = "private_applications.json"
APP_CONNECTORS_FILE = "app_connectors.json"

# webhooks.json: exported via `catocli export webhooks` (auto-included in
# `export all`). Now Terraform-managed by the `cato_webhook` resource in
# 1-dependencies/bulk_webhooks.tf (deploys before subscription groups). The
# export is credential-free by design (no auth/body), so creation also needs a
# `webhook_secrets` tfvars entry per webhook -- webhooks without one are skipped
# (surfaced via the webhooks_skipped_no_secret output), mirroring the
# mailing-list "All Admins" skip. Optional-until-present, same treatment as
# DHCP_RELAY_GROUPS_FILE got before dhcp_relay_groups.json existed. Once
# present, subscription_groups.json's integration[] names are cross-checked
# against it (see SUBSCRIPTION_GROUPS_FILE below).
WEBHOOKS_FILE = "webhooks.json"
SUBSCRIPTION_GROUPS_FILE = "subscription_groups.json"

# SCIM replication dataset (3-scim-users/). Optional: when both CSVs are
# present, policy rule usersGroup/user name references are cross-checked
# against them (warn on a reference that resolves to neither -- e.g. a
# renamed/deleted SCIM group or user, "rule hygiene" in operator terms);
# when absent, a single INFO line records that the cross-check was skipped
# rather than silently saying nothing.
SCIM_USERS_FILE = "scim_users.csv"
SCIM_GROUPS_FILE = "scim_groups.csv"

STATIC_HOSTS_FILE = "static_hosts.json"
SOCKET_SITES_FILE = "socket_sites.json"

# Per-site-type PLACEHOLDER exports (catocli export vsocket_sites /
# ipsec_sites / interconnect_sites). Optional-until-present -- an account
# may have no cloud/IPSec/interconnect sites. 0-sites provisions these as
# placeholder shells (site + native range only); their names widen the
# 2-groups managed-site set so whole-SITE group members resolve. Same
# {"sites":[...]} wrapper as socket_sites.json but NOT required, so they're
# validated separately from check_socket_sites (which errors on absence).
PLACEHOLDER_SITE_FILES = [
    "vsocket_sites.json",
    "ipsec_sites.json",
    "interconnect_sites.json",
]

EXPECTED_FILES = (
    [f for f, _ in POLICY_FILES]
    + DEPENDENCY_LIST_FILES
    + [STATIC_HOSTS_FILE, SOCKET_SITES_FILE]
)

# Rule-JSON keys that reference other managed object types by name. Mapped to
# the dependency-list name-set they should resolve against. Refs that don't
# resolve are WARNINGS, not errors -- they may legitimately point at built-in
# or catalog objects (e.g. system groups) that this repo doesn't manage.
CROSS_REF_KEYS = {
    "group": "groups.json",
    "customApp": "custom_applications.json",
    "globalIpRange": "global_ip_ranges.json",
}

# Rule tracking.alert reference keys -> the dependency name-set they must resolve
# against. A miss usually means the export captured a SYNTHETIC/placeholder name
# the API emits for a system object in the policy-query context (e.g. the system
# "All Admins" mailing list rendered as "MailingList-<id>"), which never matches
# the real object name -- it breaks name-based replication AND causes a post-apply
# "Provider produced inconsistent result after apply" set-correlation error on the
# alert's mailing_list/subscription_group/webhook set. webhooks.json is
# optional-until-present, so its name-set is only cross-checked when loaded.
ALERT_REF_KEYS = {
    "mailingList": "mailing_lists.json",
    "subscriptionGroup": "subscription_groups.json",
    "webhook": WEBHOOKS_FILE,
}


class Report:
    def __init__(self, greenfield=False):
        self.errors = []
        self.warnings = []
        self.infos = []
        # Greenfield mode: the dataset is a `catocli export all --strip-ids`
        # capture headed for an EMPTY target, so migration.tf stays parked and
        # no import block will ever read these ids. Missing import ids are then
        # expected (they are stripped on purpose), not an error.
        self.greenfield = greenfield
        self.missing_import_ids = 0

    def missing_import_id(self, msg):
        """A missing id that only a migration.tf import block would need.

        ERROR for brownfield adoption (the import block cannot be built without
        it); counted and summarised as one INFO line in greenfield mode."""
        if self.greenfield:
            self.missing_import_ids += 1
        else:
            self.errors.append(msg)

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def info(self, msg):
        self.infos.append(msg)

    def ok(self):
        return not self.errors

    def render(self):
        if self.greenfield and self.missing_import_ids:
            self.infos.insert(0,
                f"greenfield mode: {self.missing_import_ids} object(s) have no source-tenant id "
                f"(expected for a --strip-ids export; only migration.tf import blocks need ids, "
                f"and those stay parked for a greenfield apply)")
        lines = []
        lines.append("=" * 72)
        lines.append("config_data validation report")
        lines.append("=" * 72)
        if not self.errors and not self.warnings and not self.infos:
            lines.append("PASS -- no errors, no warnings.")
        else:
            lines.append(f"{len(self.errors)} error(s), {len(self.warnings)} warning(s), {len(self.infos)} info")
            lines.append("")
            if self.errors:
                lines.append("ERRORS:")
                for e in self.errors:
                    lines.append(f"  [ERROR] {e}")
                lines.append("")
            if self.warnings:
                lines.append("WARNINGS:")
                for w in self.warnings:
                    lines.append(f"  [WARN]  {w}")
                lines.append("")
            if self.infos:
                lines.append("INFO:")
                for n in self.infos:
                    lines.append(f"  [INFO]  {n}")
        lines.append("=" * 72)
        lines.append("RESULT: " + ("FAIL" if self.errors else "PASS (with warnings)" if self.warnings else "PASS"))
        lines.append("=" * 72)
        return "\n".join(lines)


def load_json(report, directory, filename):
    """Load + parse a JSON file. Records an error and returns None on failure."""
    path = os.path.join(directory, filename)
    if not os.path.isfile(path):
        report.error(f"{filename}: file not found at {path}")
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        report.error(f"{filename}: invalid JSON ({e})")
        return None


def check_file_presence(report, directory):
    for filename in EXPECTED_FILES:
        if not os.path.isfile(os.path.join(directory, filename)):
            report.error(f"{filename}: expected export file is missing from {directory}")


def collect_refs(obj, key, acc):
    """Recursively collect `name` values from every list found under `key`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and "name" in item:
                        acc.add(item["name"])
            collect_refs(v, key, acc)
    elif isinstance(obj, list):
        for item in obj:
            collect_refs(item, key, acc)


def check_policy_file(
    report,
    directory,
    filename,
    family_key,
    dep_name_sets,
    rules_key="rules",
    order_key="rules_in_sections",
    order_has_section=True,
    scim_name_sets=None,
):
    """Validate one policy rules/sections file.

    `rules_key`/`order_key`/`order_has_section` accommodate the two known
    naming deviations among the 14 new 2026-11 families -- see
    NEW_POLICY_FILES's comment. Defaults match the original 4 families
    (ifw/wf/wnw/tls) unchanged.
    """
    data = load_json(report, directory, filename)
    if data is None:
        return

    try:
        policy = data["data"]["policy"][family_key]["policy"]
    except (KeyError, TypeError):
        report.error(f"{filename}: missing data.policy.{family_key}.policy -- unexpected top-level shape")
        return

    rules = policy.get(rules_key)
    sections = policy.get("sections")
    rules_in_sections = policy.get(order_key)

    if not isinstance(rules, list):
        report.error(f"{filename}: policy.{rules_key} is missing or not a list")
        rules = []
    if not isinstance(sections, list):
        report.error(f"{filename}: policy.sections is missing or not a list")
        sections = []
    if not isinstance(rules_in_sections, list):
        report.error(f"{filename}: policy.{order_key} is missing or not a list")
        rules_in_sections = []

    # Required keys per rule / section.
    rule_names = []
    for i, entry in enumerate(rules):
        rule = entry.get("rule") if isinstance(entry, dict) else None
        if not isinstance(rule, dict):
            report.error(f"{filename}: rules[{i}] has no 'rule' object")
            continue
        rid, rname = rule.get("id"), rule.get("name")
        if not rid:
            report.error(f"{filename}: rules[{i}].rule missing 'id' (rule name={rname!r})")
        if not rname:
            report.error(f"{filename}: rules[{i}].rule missing 'name' (rule id={rid!r})")
        else:
            rule_names.append(rname)

    section_names = []
    for i, sec in enumerate(sections):
        sname, sid = sec.get("section_name"), sec.get("section_id")
        if not sname:
            report.error(f"{filename}: sections[{i}] missing 'section_name'")
        if not sid:
            report.error(f"{filename}: sections[{i}] missing 'section_id' (section_name={sname!r})")
        else:
            section_names.append(sname)

    # Uniqueness: rule names, section names (within this file).
    for label, names in (("rule", rule_names), ("section", section_names)):
        dupes = [n for n, c in Counter(names).items() if c > 1]
        for n in dupes:
            report.error(f"{filename}: duplicate {label} name {n!r}")

    rule_name_set = set(rule_names)
    section_name_set = set(section_names)
    # Sub-policies (IFW/WF/LAN FW): rules_in_sections can contain sub-policy
    # ORDERING rows (entry_type == "sub_policy") that position a whole sub-policy
    # within a section. Those rows carry `sub_policy_name`, NOT `rule_name`, and
    # must be validated against policy.sub_policies[] rather than policy.rules.
    sub_policy_name_set = {
        sp.get("sub_policy_name")
        for sp in (policy.get("sub_policies") or [])
        if isinstance(sp, dict) and sp.get("sub_policy_name")
    }

    # rules_in_sections resolution. Rule-name misses are real breakage (a rule
    # ordering entry pointing at nothing). The implicit-default-section
    # sentinel (`section_name: ""`; also None, and "Default Section" from
    # pre-2026-11 exports -- a string the CLI used to synthesize, never an API
    # name) is a known, expected condition: rules at the policy root with no
    # explicit section. It is counted and reported once per file as INFO. Any
    # OTHER unresolved section name still warns per row (stale/odd export) --
    # e.g. IFW's "Default Outbound Internet" system section, which the API
    # never returns in policy.sections[].
    implicit_default_rows = 0
    for i, entry in enumerate(rules_in_sections):
        if entry.get("entry_type") == "sub_policy":
            # Sub-policy ordering row: references a sub-policy, not a rule.
            spname = entry.get("sub_policy_name")
            if spname not in sub_policy_name_set:
                report.error(
                    f"{filename}: {order_key}[{i}] references sub_policy_name {spname!r} "
                    f"which does not exist in policy.sub_policies"
                )
        else:
            rname = entry.get("rule_name")
            if rname not in rule_name_set:
                report.error(
                    f"{filename}: {order_key}[{i}] references rule_name {rname!r} "
                    f"which does not exist in policy.{rules_key}"
                )
        if order_has_section:
            sname = entry.get("section_name")
            if sname in ("", None, "Default Section"):
                if sname not in section_name_set:
                    implicit_default_rows += 1
            elif sname not in section_name_set:
                report.warn(
                    f"{filename}: {order_key}[{i}] references section_name {sname!r} "
                    f"which does not exist in policy.sections -- likely an implicit/system "
                    f"section (e.g. a tenant's default section) that the export never lists "
                    f"explicitly; verify against the live tenant if unexpected"
                )
    if implicit_default_rows:
        report.info(
            f"{filename}: {implicit_default_rows} rule(s) in the implicit default section "
            f"(no explicit section in policy.sections) -- expected for policies without "
            f"custom sections"
        )

    # Cross-file referential integrity: group/customApp/globalIpRange name
    # refs inside rules should resolve against the matching dependency file.
    for ref_key, dep_filename in CROSS_REF_KEYS.items():
        refs = set()
        collect_refs(rules, ref_key, refs)
        known = dep_name_sets.get(dep_filename, set())
        for ref in sorted(refs - known):
            report.warn(
                f"{filename}: rule references {ref_key} {ref!r} not found in {dep_filename} "
                f"-- may be a built-in/catalog object this repo doesn't manage"
            )

    # Alert-reference integrity: tracking.alert.{mailingList,subscriptionGroup,
    # webhook} name refs should resolve against the matching dependency file. A
    # miss is a WARNING -- most often a system/placeholder name the export
    # synthesized (e.g. the system "All Admins" list as "MailingList-<id>") that
    # won't match the live object and would fail apply with a set-correlation
    # error. Only cross-check dependency files whose name-set is loaded
    # (webhooks.json is optional-until-present).
    for ref_key, dep_filename in ALERT_REF_KEYS.items():
        if dep_filename not in dep_name_sets:
            continue
        refs = set()
        collect_refs(rules, ref_key, refs)
        for ref in sorted(refs - dep_name_sets[dep_filename]):
            report.warn(
                f"{filename}: alert references {ref_key} {ref!r} not found in {dep_filename} "
                f"-- likely a system/placeholder name (e.g. a synthetic 'MailingList-<id>') "
                f"that won't match the live object; fix the export/config or the apply will "
                f"fail with a 'does not correlate' set error on the alert"
            )

    # SCIM cross-ref: usersGroup/user name refs against scim_groups.csv /
    # scim_users.csv (only when the dataset is present -- see main()).
    if scim_name_sets is not None:
        group_refs = set()
        collect_refs(rules, "usersGroup", group_refs)
        for ref in sorted(group_refs - scim_name_sets["groups"]):
            report.warn(
                f"{filename}: rule references usersGroup {ref!r} not found in "
                f"{SCIM_GROUPS_FILE} -- may be a renamed/deleted SCIM group (rule hygiene) "
                f"or a built-in group outside SCIM sync"
            )
        user_refs = set()
        collect_refs(rules, "user", user_refs)
        for ref in sorted(user_refs - scim_name_sets["users"]):
            report.warn(
                f"{filename}: rule references user {ref!r} not found in {SCIM_USERS_FILE} "
                f"-- may be a renamed/deleted SCIM user (rule hygiene) or a portal admin "
                f"account outside SCIM sync"
            )


def check_dependency_list(report, directory, filename, required_keys, id_keys=None, name_key="name"):
    """Validate one flat top-level-array dependency file. Returns the set of names found."""
    data = load_json(report, directory, filename)
    names = set()
    if data is None:
        return names
    if not isinstance(data, list):
        report.error(f"{filename}: expected a top-level JSON array, got {type(data).__name__}")
        return names

    seen_names = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            report.error(f"{filename}: item[{i}] is not an object")
            continue
        for key in required_keys:
            # An empty members list is legitimate: the API accepts a group with
            # no members (it matches nothing), and source tenants do have them.
            # Only a missing/non-list members key is structurally broken.
            if key == "members" and isinstance(item.get(key), list):
                continue
            # A custom application with an empty criteria list is real (the API and CMA accept it), but the
            # Terraform provider refuses it, as it does a criterion that is only "any protocol".
            if key == "criteria" and isinstance(item.get(key), list):
                why = custom_app_problem(item)
                if why:
                    report.error(f"{filename}: item[{i}] (name={item.get(name_key)!r}): {why}, which the Cato "
                                 "Terraform provider refuses. Create it by hand in the target CMA (preflight_check.py "
                                 "leaves it out of the <config>_target copy, with the rules that use it), or fix "
                                 "it in the source and export again.")
                continue
            if not item.get(key):
                report.error(f"{filename}: item[{i}] (name={item.get(name_key)!r}) missing required key {key!r}")
        if id_keys:
            for key in id_keys:
                if not item.get(key):
                    report.missing_import_id(f"{filename}: item[{i}] (name={item.get(name_key)!r}) missing id field {key!r} "
                                             f"needed for a migration.tf import block")
        nm = item.get(name_key)
        if nm:
            seen_names.append(nm)
            names.add(nm)

    dupes = [n for n, c in Counter(seen_names).items() if c > 1]
    for n in dupes:
        report.error(f"{filename}: duplicate name {n!r}")

    if seen_names and seen_names != sorted(seen_names):
        report.warn(f"{filename}: entries are not sorted by name -- export is non-deterministic across runs")

    return names


def _normalize_ip_range(value):
    """Normalize a global-IP-range value the way createGlobalIpRangeBulk does:
    a bare host ('192.0.2.6') is equivalent to its /32 ('192.0.2.6/32'),
    and any CIDR collapses to its network form. Returns a canonical string for
    equality, or the raw trimmed value if it isn't a plain host/CIDR (e.g. a
    'from-to' range -- left as-is, compared literally)."""
    if value is None:
        return None
    s = str(value).strip()
    try:
        if "/" in s:
            return str(ipaddress.ip_network(s, strict=False))
        if "-" not in s:
            # bare host -> /32 (or /128 for v6); this is the collision the bulk
            # create rejects: '192.0.2.6' == '192.0.2.6/32'
            return str(ipaddress.ip_network(s + "/32", strict=False)) if ":" not in s \
                else str(ipaddress.ip_network(s + "/128", strict=False))
    except ValueError:
        pass
    return s


def check_global_ip_range_dupes(report, directory, filename="global_ip_ranges.json"):
    """createGlobalIpRangeBulk creates the whole cato_global_ip_ranges singleton
    in ONE mutation and rejects it wholesale with 'Duplicate IP Range' if any two
    entries resolve to the same effective range -- even when their NAMES differ
    (so check_dependency_list's unique-name check doesn't catch it). A bare host
    and its /32 collide. Flag it here as an ERROR so it surfaces at validate time
    instead of failing a live apply."""
    data = load_json(report, directory, filename)
    if not isinstance(data, list):
        return
    by_range = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        raw = item.get("ipRange")
        canon = _normalize_ip_range(raw)
        if canon is None:
            continue
        by_range.setdefault(canon, []).append((item.get("name"), raw))
    for canon, entries in by_range.items():
        if len(entries) > 1:
            label = ", ".join(f"{n!r} ({r})" for n, r in entries)
            report.error(
                f"{filename}: {len(entries)} entries resolve to the same IP range "
                f"{canon} -- {label}; createGlobalIpRangeBulk rejects the whole "
                f"singleton with 'Duplicate IP Range'. Drop or re-IP all but one."
            )


def load_scim_name_sets(report, directory):
    """Load the SCIM replication dataset (3-scim-users/'s committed CSVs,
    mirrored at config_data/scim_users.csv + scim_groups.csv) into name sets
    usable for rule cross-referencing. Returns None if either CSV is absent
    (caller emits the single INFO line) -- this is advisory data, not a
    structural requirement of config_data/, so a missing file is never an
    error here.

    `users` matches both the SCIM email and the "given_name family_name"
    full name a rule's `user` field carries (e.g. "the operator Anderson").
    `groups` matches scim_groups.csv's display_name, which is what a rule's
    `usersGroup` field names.
    """
    users_path = os.path.join(directory, SCIM_USERS_FILE)
    groups_path = os.path.join(directory, SCIM_GROUPS_FILE)
    if not (os.path.isfile(users_path) and os.path.isfile(groups_path)):
        return None

    users = set()
    try:
        with open(users_path, newline="") as f:
            for row in csv.DictReader(f):
                full_name = f"{(row.get('given_name') or '').strip()} {(row.get('family_name') or '').strip()}".strip()
                if full_name:
                    users.add(full_name)
                email = (row.get("email") or "").strip()
                if email:
                    users.add(email)
    except (OSError, csv.Error) as e:
        report.warn(f"{SCIM_USERS_FILE}: could not read ({e}) -- skipping SCIM user cross-check")
        return None

    groups = set()
    try:
        with open(groups_path, newline="") as f:
            for row in csv.DictReader(f):
                display_name = (row.get("display_name") or "").strip()
                if display_name:
                    groups.add(display_name)
    except (OSError, csv.Error) as e:
        report.warn(f"{SCIM_GROUPS_FILE}: could not read ({e}) -- skipping SCIM group cross-check")
        return None

    return {"users": users, "groups": groups}


def check_allocated_ip_identity(report, directory):
    """Enforce (popLocation.name, description) uniqueness in allocated_ips.json.

    That pair is the cross-tenant identity of an allocation (Cato assigns the
    IP, so the display name differs per tenant); scripts/remap_allocated_ips.py
    joins source->target allocations on it. A duplicate pair makes the policy
    allocationIp-ref remap ambiguous."""
    data = load_json(report, directory, ALLOCATED_IPS_FILE)
    if not isinstance(data, list):
        return
    pairs = []
    for item in data:
        if not isinstance(item, dict):
            continue
        pop = (item.get("popLocation") or {}).get("name") or ""
        desc = item.get("description") or ""
        pairs.append((pop, desc))
    for pair, count in Counter(pairs).items():
        if count > 1:
            report.error(
                f"{ALLOCATED_IPS_FILE}: {count} allocations share popLocation+description "
                f"{pair!r} -- the pair is the cross-tenant remap join key and must be unique; "
                f"set distinct descriptions in CMA and re-export"
            )


def check_app_connectors(report, directory):
    """Validate app_connectors.json ({connectors: [...], groups: [...]}) when present.

    Not a flat array like the other dependency exports, so it gets its own
    optional-until-present structural check instead of check_dependency_list."""
    if not os.path.isfile(os.path.join(directory, APP_CONNECTORS_FILE)):
        report.warn(
            f"{APP_CONNECTORS_FILE}: optional new dependency export not present yet -- "
            f"skipping structural checks"
        )
        return
    data = load_json(report, directory, APP_CONNECTORS_FILE)
    if data is None:
        return
    if not isinstance(data, dict) or not isinstance(data.get("connectors"), list):
        report.error(f"{APP_CONNECTORS_FILE}: expected an object with a connectors[] array")
        return
    names = []
    for i, item in enumerate(data["connectors"]):
        if not isinstance(item, dict):
            report.error(f"{APP_CONNECTORS_FILE}: connectors[{i}] is not an object")
            continue
        for key in ("name", "groupName", "type"):
            if not item.get(key):
                report.error(f"{APP_CONNECTORS_FILE}: connectors[{i}] "
                             f"(name={item.get('name')!r}) missing required key {key!r}")
        if not item.get("id"):
            report.missing_import_id(f"{APP_CONNECTORS_FILE}: connectors[{i}] (name={item.get('name')!r}) "
                                     f"missing id field needed for a migration.tf import block")
        if item.get("name"):
            names.append(item["name"])
    for n, c in Counter(names).items():
        if c > 1:
            report.error(f"{APP_CONNECTORS_FILE}: duplicate connector name {n!r} ({c}x)")
    groups = data.get("groups")
    if groups is not None and not isinstance(groups, list):
        report.error(f"{APP_CONNECTORS_FILE}: groups must be an array of names")


def check_webhooks_and_subscription_groups(report, directory):
    """webhooks.json: optional-until-present, same treatment
    DHCP_RELAY_GROUPS_FILE got before dhcp_relay_groups.json existed. Now
    Terraform-managed (cato_webhook, 1-dependencies/bulk_webhooks.tf) with
    secrets supplied via the webhook_secrets tfvars. Once present, cross-check
    subscription_groups.json's integration[] names against it."""
    webhooks_path = os.path.join(directory, WEBHOOKS_FILE)
    if not os.path.isfile(webhooks_path):
        report.warn(
            f"{WEBHOOKS_FILE}: optional export not present -- skipping webhook "
            f"structural checks and the subscription_groups integration cross-check"
        )
        return

    webhook_names = check_dependency_list(report, directory, WEBHOOKS_FILE, required_keys=["name"], id_keys=["id"])

    sub_groups_path = os.path.join(directory, SUBSCRIPTION_GROUPS_FILE)
    if not os.path.isfile(sub_groups_path):
        return  # already reported as missing by check_file_presence
    data = load_json(report, directory, SUBSCRIPTION_GROUPS_FILE)
    if not isinstance(data, list):
        return
    integration_refs = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        for integration in entry.get("integration") or []:
            if isinstance(integration, dict) and integration.get("name"):
                integration_refs.add(integration["name"])
    for ref in sorted(integration_refs - webhook_names):
        report.warn(
            f"{SUBSCRIPTION_GROUPS_FILE}: integration {ref!r} not found in {WEBHOOKS_FILE} "
            f"-- the webhook must exist in the target tenant (managed by cato_webhook in "
            f"1-dependencies/bulk_webhooks.tf, which needs a webhook_secrets tfvars entry) "
            f"before this subscription group's integration ref can resolve"
        )


def check_static_hosts(report, directory, site_names):
    data = load_json(report, directory, STATIC_HOSTS_FILE)
    if data is None:
        return
    if not isinstance(data, list):
        report.error(f"{STATIC_HOSTS_FILE}: expected a top-level JSON array, got {type(data).__name__}")
        return

    pairs = []
    unmanaged_sites = set()
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            report.error(f"{STATIC_HOSTS_FILE}: item[{i}] is not an object")
            continue
        for key in ("name", "site_name", "ip"):
            if not item.get(key):
                report.error(f"{STATIC_HOSTS_FILE}: item[{i}] missing required key {key!r}")
        for key in ("id", "site_id"):
            if not item.get(key):
                report.missing_import_id(
                    f"{STATIC_HOSTS_FILE}: item[{i}] (name={item.get('name')!r}) missing id field {key!r} "
                    f"needed for a migration.tf import block"
                )
        site_name, name = item.get("site_name"), item.get("name")
        if site_name and name:
            pairs.append((site_name, name))
        if site_name and site_name not in site_names:
            unmanaged_sites.add(site_name)

    dupes = [p for p, c in Counter(pairs).items() if c > 1]
    for p in dupes:
        report.error(f"{STATIC_HOSTS_FILE}: duplicate (site_name, name) pair {p!r}")

    for sn in sorted(unmanaged_sites):
        report.warn(
            f"{STATIC_HOSTS_FILE}: site_name {sn!r} not present in {SOCKET_SITES_FILE} -- "
            f"legitimate if that site is unmanaged by this repo's 0-sites/ Terraform"
        )


def check_socket_sites(report, directory):
    data = load_json(report, directory, SOCKET_SITES_FILE)
    names = set()
    if data is None:
        return names
    if not isinstance(data, dict) or not isinstance(data.get("sites"), list):
        report.error(f"{SOCKET_SITES_FILE}: expected top-level {{'sites': [...]}} shape")
        return names

    sites = data["sites"]
    seen_names = []
    for i, site in enumerate(sites):
        if not isinstance(site, dict):
            report.error(f"{SOCKET_SITES_FILE}: sites[{i}] is not an object")
            continue
        sname = site.get("name")
        if not site.get("id"):
            report.missing_import_id(f"{SOCKET_SITES_FILE}: sites[{i}] (name={sname!r}) missing 'id' "
                                     f"needed for a migration.tf import block")
        if not sname:
            report.error(f"{SOCKET_SITES_FILE}: sites[{i}] missing 'name'")
        else:
            seen_names.append(sname)
            names.add(sname)

        for wi, wifi in enumerate(site.get("wifi_ssids") or []):
            if not isinstance(wifi, dict) or not wifi.get("id"):
                report.missing_import_id(
                    f"{SOCKET_SITES_FILE}: sites[{i}] (name={sname!r}) wifi_ssids[{wi}] missing 'id' "
                    f"needed for a migration.tf import block"
                )

    dupes = [n for n, c in Counter(seen_names).items() if c > 1]
    for n in dupes:
        report.error(f"{SOCKET_SITES_FILE}: duplicate site name {n!r}")

    return names


def check_placeholder_sites(report, directory):
    """vsocket_sites.json / ipsec_sites.json / interconnect_sites.json --
    optional-until-present (same treatment DHCP_RELAY_GROUPS_FILE got before it
    existed). Each uses the {"sites":[...]} wrapper; entries need a 'name'. No
    'id' is required (placeholders are created greenfield, not imported). Returns
    the union of placeholder site names for cross-file use."""
    names = set()
    for filename in PLACEHOLDER_SITE_FILES:
        data = load_json(report, directory, filename)
        if data is None:
            report.info(f"{filename}: optional per-site-type export not present yet -- skipped")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("sites"), list):
            report.error(f"{filename}: expected top-level {{'sites': [...]}} shape")
            continue
        seen = []
        for i, site in enumerate(data["sites"]):
            if not isinstance(site, dict):
                report.error(f"{filename}: sites[{i}] is not an object")
                continue
            sname = site.get("name")
            if not sname:
                report.error(f"{filename}: sites[{i}] missing 'name'")
            else:
                seen.append(sname)
                names.add(sname)
        for n in [n for n, c in Counter(seen).items() if c > 1]:
            report.error(f"{filename}: duplicate site name {n!r}")
    return names


def _empty_criterion(c):
    """A custom application criterion that is only "any protocol": no port, port range or destination."""
    c = c or {}
    d = c.get("destination") or {}
    ip = d.get("destinationIp") or {}
    return (c.get("protocol") in (None, "ANY") and not c.get("port") and not c.get("portRange")
            and not d.get("domain") and not d.get("fqdn") and not ip.get("ip") and not ip.get("subnet")
            and not ip.get("ipRange"))


def custom_app_problem(app):
    """Why the Cato Terraform provider refuses a custom application as exported, or None.

    The provider needs at least one criterion, and none that is only "any protocol". The API and the CMA accept
    both, so accounts do have them, and the export shows them exactly as the account holds them.
    """
    crit = app.get("criteria")
    if not isinstance(crit, list) or not crit:
        return "it has no criteria"
    if any(_empty_criterion(c) for c in crit):
        return "one of its criteria is only 'any protocol', with no port or destination"
    return None


# Required-key / id-key config per dependency file, matching the JSON shapes
# documented in CLAUDE.md's "Dependency JSONs" line.
DEPENDENCY_SPECS = {
    "groups.json": {"required_keys": ["name", "members"], "id_keys": ["id"]},
    # Global IP Ranges are a singleton cato_global_ip_ranges resource -- no
    # per-range id is needed for a migration import block, so none is checked.
    "global_ip_ranges.json": {"required_keys": ["name", "ipRange"], "id_keys": None},
    "custom_applications.json": {"required_keys": ["name", "criteria"], "id_keys": ["id"]},
    "fqdn_containers.json": {"required_keys": ["name", "mode", "values"], "id_keys": ["id"]},
    "ip_range_containers.json": {"required_keys": ["name", "mode", "values"], "id_keys": ["id"]},
    "mailing_lists.json": {"required_keys": ["name"], "id_keys": ["id"]},
    "subscription_groups.json": {"required_keys": ["name"], "id_keys": ["id"]},
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-data", "--config-dir", dest="config_data",
                        default=DEFAULT_CONFIG_DATA, help="path to config_data/ directory")
    parser.add_argument("--greenfield", action="store_true",
                        help="dataset is a --strip-ids export for an EMPTY target tenant: "
                             "missing source-tenant ids (only needed by migration.tf import "
                             "blocks, which stay parked for greenfield) are reported as one "
                             "INFO line instead of an ERROR per object")
    args = parser.parse_args()
    directory = args.config_data

    report = Report(greenfield=args.greenfield)

    if not os.path.isdir(directory):
        report.error(f"config_data directory not found: {directory}")
        print(report.render())
        return 1

    check_file_presence(report, directory)

    dep_name_sets = {}
    for filename in DEPENDENCY_LIST_FILES:
        spec = DEPENDENCY_SPECS[filename]
        names = check_dependency_list(
            report, directory, filename, spec["required_keys"], id_keys=spec["id_keys"]
        )
        dep_name_sets[filename] = names

    # Webhook names for the alert-reference cross-check (ALERT_REF_KEYS). Optional
    # until webhooks.json is exported; the full structural webhook check still runs
    # later via check_webhooks_and_subscription_groups. Loaded quietly (isfile
    # guard) so a missing file isn't a presence error here.
    if os.path.isfile(os.path.join(directory, WEBHOOKS_FILE)):
        wh = load_json(report, directory, WEBHOOKS_FILE)
        if isinstance(wh, list):
            dep_name_sets[WEBHOOKS_FILE] = {
                w.get("name") for w in wh if isinstance(w, dict) and w.get("name")
            }

    # global_ip_ranges is a singleton bulk-create -- also reject same-IP dupes
    # (distinct names, same effective range) that unique-name checks miss.
    check_global_ip_range_dupes(report, directory)

    site_names = check_socket_sites(report, directory)
    placeholder_site_names = check_placeholder_sites(report, directory)
    check_static_hosts(report, directory, site_names)

    # SCIM replication dataset -- optional. When both CSVs are present, every
    # policy file below gets its usersGroup/user refs cross-checked against
    # them; when absent, one INFO line records the skip (not silence).
    scim_name_sets = load_scim_name_sets(report, directory)
    if scim_name_sets is None:
        report.info(
            f"SCIM cross-checks skipped: {SCIM_USERS_FILE}/{SCIM_GROUPS_FILE} not present "
            f"in {directory} -- run scripts/scim_sync.py export to generate them"
        )

    for filename, family_key in POLICY_FILES:
        check_policy_file(report, directory, filename, family_key, dep_name_sets, scim_name_sets=scim_name_sets)

    # 14 new 2-* policy families -- optional. A file only gets the full
    # structural check (errors, affects exit code) once it actually exists in
    # config_data/; otherwise it's a warning, not a failure, since several of
    # these families are still pending a module/provider release.
    for filename, family_key, opts in NEW_POLICY_FILES:
        if os.path.isfile(os.path.join(directory, filename)):
            check_policy_file(report, directory, filename, family_key, dep_name_sets, scim_name_sets=scim_name_sets, **opts)
        else:
            report.warn(
                f"{filename}: optional new-family export not present yet "
                f"(pending module/provider release) -- skipping structural checks"
            )

    # New 1-dependencies/ dependency-array type -- same optional treatment.
    if os.path.isfile(os.path.join(directory, DHCP_RELAY_GROUPS_FILE)):
        check_dependency_list(report, directory, DHCP_RELAY_GROUPS_FILE, required_keys=["name"], id_keys=["id"])
    else:
        report.warn(
            f"{DHCP_RELAY_GROUPS_FILE}: optional new dependency export not present yet -- "
            f"skipping structural checks"
        )

    # ZTNA / private-access infrastructure exports -- same optional treatment.
    if os.path.isfile(os.path.join(directory, ALLOCATED_IPS_FILE)):
        check_dependency_list(report, directory, ALLOCATED_IPS_FILE,
                              required_keys=["name", "ip", "popLocation"], id_keys=["id"])
        check_allocated_ip_identity(report, directory)
    else:
        report.warn(
            f"{ALLOCATED_IPS_FILE}: optional new dependency export not present yet -- "
            f"skipping structural checks"
        )
    if os.path.isfile(os.path.join(directory, PRIVATE_APPLICATIONS_FILE)):
        check_dependency_list(report, directory, PRIVATE_APPLICATIONS_FILE,
                              required_keys=["name", "internalAppAddress"], id_keys=["id"])
    else:
        report.warn(
            f"{PRIVATE_APPLICATIONS_FILE}: optional new dependency export not present yet -- "
            f"skipping structural checks"
        )
    check_app_connectors(report, directory)

    # webhooks.json -- optional until the catocli export type ships; once
    # present, cross-checks subscription_groups.json's integration[] names.
    check_webhooks_and_subscription_groups(report, directory)

    print(report.render())
    return 1 if not report.ok() else 0


if __name__ == "__main__":
    sys.exit(main())
