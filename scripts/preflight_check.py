#!/usr/bin/env python3
"""Before-you-migrate dependency check for a Cato tenant-to-tenant migration.

Compares a SOURCE tenant (its catocli export plus live, read-only API reads)
with a TARGET tenant (live reads) and lists everything that has to be enabled,
licensed, created or decided BEFORE `bulk_apply.py` runs, so a migration does
not discover it one failed apply at a time.

What it checks
  access      both tenants reachable, not the same account, plan type
  export      the source export is complete (types present, static hosts on
              every site type, no stripped-ids surprises)
  licensing   public (allocated) IP capacity, ZTNA users, site licenses,
              add-on SKUs the source has and the target lacks
  pops        every PoP the source allocates IPs on (and rules pin) exists for
              the target (China and private PoPs need their own entitlement)
  identity    SCIM / LDAP / manual users and user groups the rules name:
              present in the target or not, how they get there, whether a SCIM
              integration looks active, and (with scim_url/scim_token in the
              target profile) whether the SCIM endpoint answers
  cma-only    objects the API can reference but not create or list: custom
              categories, device posture profiles, bandwidth priorities, user
              notification templates, DLP content/EDM profiles, RBI profiles
  features    RBI, Site Web Proxy, Static Range Translation, LAN Layer 7,
              the 169.254.169.254 range the API firewall blocks, Wi-Fi
              passwords, webhook endpoints, IPSec pre-shared keys
  integrity   source rules pointing at classic groups (invisible to the API)
              or at objects that no longer exist in the source
  target      factory/existing rules whose names clash with the source,
              existing sites/groups/IPs, policy on/off differences

Read-only by default. `--probe` additionally verifies the objects and features
the API cannot list by trying them inside a throwaway, named draft revision on
the TARGET (Internet Firewall, WAN Network, App Control, Site Web Proxy) and
discarding the revision straight away: nothing is published, and the live
policy never changes. It needs a target key with edit rights and is refused
for an account listed as a protected source.

Usage
  python3 scripts/preflight_check.py --source-profile <source> --target-profile <target>
  python3 scripts/preflight_check.py --source-profile <source> --target-profile <target> --probe
  python3 scripts/preflight_check.py ... --config-dir config_data --out-dir preflight --strict

Exit code: 2 = at least one BLOCKER, 1 = ACTION items only (with --strict),
0 = nothing that stops a migration. Reports land in --out-dir (gitignored:
tenant data): preflight_report.md and .json for engineers, and
preflight_report.html, a Cato-branded readiness report written for the
customer (add --pdf for a PDF copy and --customer "Name" for the cover).
"""
import argparse
import copy
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cato_profile import REPO_ROOT, get_profile, load_protected  # noqa: E402
from preflight_report import (_shown, build_runbook, guide, html_to_pdf, render_customer_html,  # noqa: E402
                              render_runbook_md, write_migrate_sh)

DEFAULT_ENDPOINT = "https://api.catonetworks.com/api/v1/graphql2"
SEVERITIES = ["BLOCKER", "ACTION", "WARN", "INFO", "OK"]
SEV_ICON = {"BLOCKER": "[X]", "ACTION": "[!]", "WARN": "[~]", "INFO": "[i]", "OK": "[ok]"}

# Bandwidth management priorities every tenant has out of the box.
DEFAULT_BANDWIDTH_PRIORITIES = {"10", "20", "30", "40", "255"}

# Reference kinds (keys inside rule JSON whose values are {name} refs).
IDENTITY_KINDS = {"usersGroup", "user"}
CMA_ONLY_KINDS = {  # kind: (singular, plural)
    "customCategory": ("custom category", "custom categories"),
    "device": ("device posture profile", "device posture profiles"),
    "bandwidthPriority": ("bandwidth management priority", "bandwidth management priorities"),
    "userNotification": ("user notification template", "user notification templates"),
    "contentProfile": ("DLP content profile", "DLP content profiles"),
    "edmProfile": ("DLP EDM profile", "DLP EDM profiles"),
    "rbiProfile": ("RBI profile", "RBI profiles"),
}
INTEGRITY_KINDS = {"group", "site", "backhaulingSite", "associatedSite", "siteNetworkSubnet",
                   "customApp", "globalIpRange", "host", "allocationIp", "externalIp", "popLocation"}
# The same object under another key in some policies (from the API schema's *RefInput fields):
# device posture in Always-On, TLS Inspection and Dynamic IP Allocation; global ranges in Terminal Server.
REF_ALIASES = {"devicePostureProfile": "device", "allowedHostIP": "globalIpRange", "excludeTraffic": "globalIpRange"}
# Referenced objects the migration neither creates nor can look up: flagged so someone checks them by hand.
UNCHECKED_KINDS = {"networkInterface": "network interface", "floatingSubnet": "floating subnet",
                   "valueSet": "value set"}
REF_KINDS = IDENTITY_KINDS | set(CMA_ONLY_KINDS) | INTEGRITY_KINDS | set(UNCHECKED_KINDS) | {"sanctionedAppsCategory"}


def _pl(n, one, many=None):
    """'1 rule' / '2 rules' (many defaults to one + 's')."""
    return f"{n} {one if n == 1 else (many or one + 's')}"

# GraphQL policy family names, in the order bulk_apply deploys them.
POLICY_FAMILIES = [
    "internetFirewall", "wanFirewall", "wanNetwork", "applicationControl", "tlsInspect",
    "socketBypass", "socketLan", "clientConnectivity", "privateAccess", "splitTunnel",
    "appTenantRestriction", "ztnaAlwaysOn", "dynamicIpAllocation", "remotePortFwd",
    "sdpStaticIpAllocation", "siteWebProxy", "antiMalwareFileHash", "terminalServer",
]

# The CMA role permission each policy's API needs (api.catonetworks.com/documentation, "Required Permissions").
POLICY_PERMISSION = {
    "internetFirewall": "Internet Firewall", "wanFirewall": "WAN Firewall", "wanNetwork": "Network Rules",
    "applicationControl": "Application Control", "tlsInspect": "TLS Inspection", "socketBypass": "Socket Bypass",
    "socketLan": "Socket LAN Network", "clientConnectivity": "Client Connectivity Policy",
    "privateAccess": "Private Access Policy", "splitTunnel": "SDP Split Tunnel", "appTenantRestriction": "Application Control",
    "ztnaAlwaysOn": "Always On Policy", "dynamicIpAllocation": "SDP IP Allocation Policy",
    "remotePortFwd": "Remote Port Forwarding", "sdpStaticIpAllocation": "SDP IP Allocation Policy",
    "siteWebProxy": "Site Web Proxy", "antiMalwareFileHash": "Anti Malware", "terminalServer": "User Awareness",
}
_DENIED = re.compile(r"access denied|insufficient privileges|permission", re.I)
# catocli export type -> policy family, for naming the permission a refused export type needs.
EXPORT_TYPE_FAMILY = {
    "if_rules": "internetFirewall", "wf_rules": "wanFirewall", "wnw_rules": "wanNetwork", "apc_rules": "applicationControl",
    "atr_rules": "appTenantRestriction", "tls_rules": "tlsInspect", "sb_rules": "socketBypass", "lan_fw_rules": "socketLan",
    "cc_rules": "clientConnectivity", "pa_rules": "privateAccess", "st_rules": "splitTunnel", "ao_rules": "ztnaAlwaysOn",
    "dia_rules": "dynamicIpAllocation", "ssia_rules": "sdpStaticIpAllocation", "rpf_rules": "remotePortFwd",
    "swp_rules": "siteWebProxy", "amfh_rules": "antiMalwareFileHash", "ts_rules": "terminalServer",
}

# Export files the bulk folders read (catocli export all + static hosts + webhooks).
EXPECTED_EXPORT_FILES = [
    "socket_sites.json", "vsocket_sites.json", "ipsec_sites.json", "groups.json",
    "global_ip_ranges.json", "allocated_ips.json", "static_hosts.json", "webhooks.json",
    "custom_applications.json",
]

SKU_FEATURES = {
    "CATO_CASB": "App Control (CASB) rules", "CATO_DLP": "DLP data rules",
    "CATO_RBI": "Remote Browser Isolation", "CATO_IP_ADD": "allocated (public) IPs",
    "CATO_ZTNA_USERS": "ZTNA / SDP users", "CATO_THREAT_PREVENTION": "threat prevention",
    "CATO_THREAT_PREVENTION_ADV": "advanced threat prevention", "CATO_EPP": "endpoint protection",
    "CATO_SITE": "site licenses", "CATO_PB": "pooled bandwidth", "CATO_IOT_OT": "IoT/OT device attributes",
    "CATO_AGTP": "agentic threat prevention", "CATO_AI_SEC_U": "AI security (users)",
    "CATO_AI_SEC_APP_U": "AI security (applications)", "CATO_ASTS_SEC_1_5K": "assets security",
    "CATO_DATALAKE": "data lake / event retention", "CATO_DEM": "digital experience monitoring",
    "CATO_SAAS_SECURITY_API": "SaaS security API (CASB API)", "CATO_XDR_PRO": "XDR Pro", "CATO_XOPS": "XOps",
    "CATO_ANTI_MALWARE_NG": "next-gen anti-malware", "CATO_IPS": "IPS", "CATO_MDR": "MDR",
}


class ApiError(Exception):
    pass


class Api:
    """Minimal GraphQL client on a catocli profile (token never printed)."""

    def __init__(self, profile):
        p = get_profile(profile)
        self.profile = profile
        self.account = str(p["account_id"])
        self.endpoint = p.get("endpoint") or DEFAULT_ENDPOINT
        self._token = p["cato_token"]
        self.scim_url = p.get("scim_url")
        self._scim_token = p.get("scim_token")

    def q(self, query, variables=None, allow_errors=False):
        body = json.dumps({"query": query, "variables": dict(variables or {}, a=self.account)}).encode()
        last = None
        for attempt in range(4):
            req = urllib.request.Request(self.endpoint, data=body, headers={
                "Content-Type": "application/json", "x-api-key": self._token,
                "User-Agent": "cato-bulk-config-management/preflight"})
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code not in (429, 500, 502, 503, 504):
                    raise ApiError(f"{self.profile}: {last}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = str(e)
            time.sleep(2 * (attempt + 1))
        else:
            raise ApiError(f"{self.profile}: request failed after retries ({last})")
        if data.get("errors") and not allow_errors:
            raise ApiError(f"{self.profile}: {data['errors'][0].get('message', data['errors'])}")
        return data

    def scim_get(self, path):
        req = urllib.request.Request(self.scim_url.rstrip("/") + path, headers={
            "Authorization": f"Bearer {self._scim_token}", "Accept": "application/scim+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")


# --------------------------------------------------------------------------
# Live tenant state
# --------------------------------------------------------------------------
Q_ACCOUNT = """query q($a:ID!){
  accountManagement(accountId:$a){ account{ id name type tenancy plan status } }
  licensing(accountId:$a){ licensingInfo{
    licenses{ sku status plan ... on SiteLicense { total site{ name } }
              ... on PooledBandwidthLicense { total allocatedBandwidth sites{ site{ name } allocatedBandwidth } }
              ... on ZtnaUsersLicense { total } ... on PublicIpsLicense { total } }
    globalLicenseAllocations{ publicIps{ total allocated available } ztnaUsers{ total allocated available } } } }
  popLocations(accountId:$a){ allocatedIpList(input:{}){ allocatedIp{ ip description popLocation{ id name } } } }
}"""


def _paged(api, query, path, limit=500, key_items="items", key_total=("pageInfo", "total")):
    out, start = [], 0
    while True:
        d = api.q(query, {"limit": limit, "from": start})["data"]
        node = d
        for p in path:
            node = node[p]
        items = node.get(key_items) or []
        out.extend(items)
        total = node
        for k in key_total:
            total = (total or {}).get(k)
        start += limit
        if not items or start >= (total or 0):
            return out


def load_tenant(api, with_policies=True):
    t = {"profile": api.profile, "account_id": api.account}
    d = api.q(Q_ACCOUNT)["data"]
    t["account"] = (d.get("accountManagement") or {}).get("account") or {}
    li = ((d.get("licensing") or {}).get("licensingInfo") or {})
    t["licenses"] = li.get("licenses") or []
    t["allocations"] = li.get("globalLicenseAllocations") or {}
    t["allocated_ips"] = ((d.get("popLocations") or {}).get("allocatedIpList") or {}).get("allocatedIp") or []

    locs = api.q('query q($a:ID!){ entityLookup(accountID:$a, type: location, limit: 5000){ items{ entity{ id name } } } }')
    t["locations"] = {i["entity"]["name"] for i in locs["data"]["entityLookup"]["items"]}
    sites = api.q('query q($a:ID!){ entityLookup(accountID:$a, type: site, limit: 5000){ items{ entity{ id name } } } }')
    t["sites"] = {i["entity"]["name"] for i in sites["data"]["entityLookup"]["items"]}
    try:  # names only; None when unreadable, so the 169.254.169.254 check falls back to "not there yet"
        gr = api.q('query q($a:ID!){ object(accountId:$a){ globalIpRangeList{ items{ name } } } }')
        t["global_ranges"] = {i["name"] for i in gr["data"]["object"]["globalIpRangeList"]["items"]}
    except Exception:  # noqa: BLE001
        t["global_ranges"] = None
    hosts = api.q('query q($a:ID!){ entityLookup(accountID:$a, type: host, limit: 1){ total } }')
    t["host_total"] = hosts["data"]["entityLookup"]["total"]

    t["users"] = _paged(api, """query q($a:ID!,$limit:Int!,$from:Int!){ user(accountId:$a){ userList(input:{paging:{limit:$limit,from:$from}}){
        items{ displayName firstName lastName email userImportType directoryId } pageInfo{ total } } } }""", ["user", "userList"])
    t["user_groups"] = _paged(api, """query q($a:ID!,$limit:Int!,$from:Int!){ user(accountId:$a){ usersGroupList(input:{paging:{limit:$limit,from:$from}}){
        items{ name importType directoryId membersCount } paging{ total } } } }""", ["user", "usersGroupList"], key_total=("paging", "total"))
    t["groups"] = {g["name"] for g in _paged(api, """query q($a:ID!,$limit:Int!,$from:Int!){ groups(accountId:$a){ groupList(input:{paging:{limit:$limit,from:$from}, sort:{}}){
        items{ name } paging{ total } } } }""", ["groups", "groupList"], limit=200, key_total=("paging", "total"))}

    t["policies"] = {}
    if with_policies:
        for fam in POLICY_FAMILIES:
            r = api.q("query q($a:ID!){ policy(accountId:$a){ %s{ policy{ enabled rules{ rule{ name } } } } } }" % fam, allow_errors=True)
            if r.get("errors") or not r.get("data"):
                t["policies"][fam] = {"error": (r.get("errors") or [{}])[0].get("message", "no data")}
                continue
            p = r["data"]["policy"][fam]["policy"]
            t["policies"][fam] = {"enabled": p.get("enabled"), "rules": [x["rule"]["name"] for x in p.get("rules") or []]}
    return t


# --------------------------------------------------------------------------
# Source export
# --------------------------------------------------------------------------
def _load(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _display_path(path):
    rel = os.path.relpath(os.path.abspath(path), REPO_ROOT)
    return path if rel.startswith("..") else rel


def load_export(config_dir):
    e = {"dir": _display_path(config_dir), "rules": [], "policy_enabled": {}, "missing_files": []}
    for f in EXPECTED_EXPORT_FILES:
        if not os.path.exists(os.path.join(config_dir, f)):
            e["missing_files"].append(f)
    policy_files = sorted(glob.glob(os.path.join(config_dir, "all_*_rules_and_sections.json")))
    if not policy_files:
        e["missing_files"].append("all_*_rules_and_sections.json")
    for f in policy_files:
        d = _load(f, {})
        pol = (d.get("data") or {}).get("policy") or {}
        if not pol:
            continue
        fam = next(iter(pol))
        p = pol[fam].get("policy") or {}
        e["policy_enabled"][fam] = p.get("enabled")
        for rw in p.get("rules") or []:
            r = rw.get("rule", rw)
            e["rules"].append({"family": fam, "name": r.get("name"), "raw": r})
        for nw in p.get("network_rules") or []:
            nr = nw.get("rule", nw)
            e["rules"].append({"family": fam, "name": nr.get("name"), "raw": nr})
            for fw in nw.get("firewall_rules") or []:
                fr = fw.get("rule", fw)
                e["rules"].append({"family": fam, "name": f'{nr.get("name")} :: {fr.get("name")}',
                                   "raw": fr, "lan_firewall": True, "network_rule": nr})
    e["sites"] = {}
    for kind in ("socket", "vsocket", "ipsec"):
        for s in (_load(os.path.join(config_dir, f"{kind}_sites.json"), {}) or {}).get("sites") or []:
            e["sites"][s["name"]] = dict(s, _kind=kind)
    e["global_ip_ranges"] = _load(os.path.join(config_dir, "global_ip_ranges.json"), []) or []
    e["allocated_ips"] = _load(os.path.join(config_dir, "allocated_ips.json"), []) or []
    e["static_hosts"] = _load(os.path.join(config_dir, "static_hosts.json"), []) or []
    e["webhooks"] = _load(os.path.join(config_dir, "webhooks.json"), []) or []
    e["groups"] = _load(os.path.join(config_dir, "groups.json"), []) or []
    e["custom_apps"] = _load(os.path.join(config_dir, "custom_applications.json"), []) or []
    e["export_report"] = _load(os.path.join(config_dir, "export_report.json"), None)  # from export_source.py
    return e


def collect_refs(rules):
    """kind -> name -> sorted list of 'family: rule' that reference it."""
    refs = defaultdict(lambda: defaultdict(set))

    def walk(o, label):
        if isinstance(o, dict):
            for k, v in o.items():
                kind = REF_ALIASES.get(k, k)
                if kind in REF_KINDS:
                    for x in (v if isinstance(v, list) else [v]):
                        if isinstance(x, dict) and x.get("name"):
                            refs[kind][x["name"]].add(label)
                walk(v, label)
        elif isinstance(o, list):
            for x in o:
                walk(x, label)

    for r in rules:
        walk(r["raw"], f'{r["family"]}: {r["name"]}')
    return {k: {n: sorted(v) for n, v in d.items()} for k, d in refs.items()}


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.findings = []
        # What the target still lacks, for the leave-out files (write_leave_out):
        self.leave_out = set()   # (rule JSON key, name) the scope script treats as missing
        self.skip_rules = set()  # rule names to leave out for a non-reference reason
        self.exclude = {"global_ip_ranges_exclude": set(), "allocated_ips_exclude": set(),
                        "ipsec_sites_exclude": set()}

    def skip(self, labels):
        """'family: name' labels -> rule names as scope_policies_to_target.py expects them."""
        for lab in labels:
            self.skip_rules.add(lab.split(": ", 1)[-1].replace(" :: ", " / "))

    def add(self, category, severity, key, ctx, title, detail="", items=None, fix="", rules=None):
        """key: stable finding id the customer report phrases; ctx: the numbers/names it needs."""
        self.findings.append({"category": category, "severity": severity, "key": key, "ctx": ctx or {}, "title": title,
                              "detail": detail, "items": items or [], "fix": fix,
                              "rules": sorted(set(rules or []))})

    def counts(self):
        c = Counter(f["severity"] for f in self.findings)
        return {s: c.get(s, 0) for s in SEVERITIES}


def _rules_for(refs, kind, names):
    out = set()
    for n in names:
        out.update(refs.get(kind, {}).get(n, []))
    return out


def _uses(refs, kind, name):
    rs = refs.get(kind, {}).get(name, [])
    return f"{len(rs)} rule(s)" + (f", e.g. {rs[0]}" if rs else "")


# --------------------------------------------------------------------------
# Probes (opt-in): verify non-listable objects inside a discarded draft revision
# --------------------------------------------------------------------------
def _ref(n):
    return {"by": "NAME", "input": n}


def _ifw_rule(name):
    return {
        "enabled": False, "name": name, "description": "preflight probe (discarded)",
        "source": {k: [] for k in ["ip", "host", "site", "subnet", "ipRange", "globalIpRange", "networkInterface",
                                   "siteNetworkSubnet", "floatingSubnet", "user", "usersGroup", "group", "systemGroup"]},
        "country": [], "device": [], "deviceOS": [],
        "deviceAttributes": {k: [] for k in ["category", "type", "model", "manufacturer", "os", "osVersion"]},
        "destination": dict({k: [] for k in ["application", "customApp", "appCategory", "sanctionedAppsCategory", "country",
                                             "domain", "fqdn", "ip", "subnet", "ipRange", "globalIpRange", "remoteAsn", "group"]},
                            containers={"fqdnContainer": [], "ipAddressRangeContainer": []}),
        "service": {"standard": [], "custom": []}, "action": "BLOCK",
        "tracking": {"event": {"enabled": False}, "alert": {"enabled": False, "frequency": "DAILY",
                                                             "subscriptionGroup": [], "webhook": [], "mailingList": []}},
        "schedule": {"activeOn": "ALWAYS"}, "activePeriod": {"useEffectiveFrom": False, "useExpiresAt": False},
        "exceptions": [], "actionConfig": {"userNotification": [], "rbiProfile": []}, "connectionsOriginList": [],
    }


def _wnw_rule(name, priority):
    net = {k: [] for k in ["user", "floatingSubnet", "globalIpRange", "group", "host", "ip", "ipRange",
                           "networkInterface", "subnet", "site", "siteNetworkSubnet", "systemGroup", "usersGroup"]}
    return {"enabled": False, "name": name, "description": "preflight probe (discarded)", "ruleType": "INTERNET",
            "routeType": "NONE", "source": net, "destination": copy.deepcopy(net),
            "application": {k: [] for k in ["application", "appCategory", "customApp", "customServiceIp", "customService",
                                            "domain", "fqdn", "service", "globalIpRange", "group"]},
            "configuration": {"activeTcpAcceleration": False, "packetLossMitigation": False, "preserveSourcePort": False,
                              "primaryTransport": {"transportType": "AUTOMATIC", "primaryInterfaceRole": "AUTOMATIC",
                                                   "secondaryInterfaceRole": "AUTOMATIC"},
                              "secondaryTransport": {"transportType": "NONE", "primaryInterfaceRole": "NONE",
                                                     "secondaryInterfaceRole": "NONE"},
                              "allocationIp": [], "popLocation": [], "backhaulingSite": []},
            "bandwidthPriority": _ref(priority), "exceptions": []}


def _apc_data_rule(name, content=(), edm=()):
    crit = {"attributes": {
        "complianceAttributes": {k: "ANY" for k in ["hippa", "isae3402", "iso27001", "pciDss", "soc1", "soc2", "soc3", "sox"]},
        "securityAttributes": {k: "ANY" for k in ["auditTrail", "encryptionAtRest", "httpSecurityHeaders", "mfa", "rbac",
                                                  "rememberPassword", "sso", "tlsEnforcement", "trustedCertificate"]}},
        "originCountry": [], "risk": []}
    body = {"application": {"application": _ref("Dropbox"), "applicationType": []}, "applicationActivitySatisfy": "ANY",
            "applicationActivity": [{"activity": {"by": "ID", "input": "content_transfer_action_upload"}, "operator": "IS"}],
            "applicationContext": {"applicationTenant": []}, "accessMethod": [],
            "source": {k: [] for k in ["country", "host", "site", "subnet", "ip", "ipRange", "globalIpRange", "networkInterface",
                                       "siteNetworkSubnet", "floatingSubnet", "user", "usersGroup", "group", "systemGroup"]},
            "device": [], "action": "BLOCK",
            "tracking": {"event": {"enabled": False}, "alert": {"enabled": False, "frequency": "DAILY",
                                                                 "subscriptionGroup": [], "webhook": [], "mailingList": []}},
            "schedule": {"activeOn": "ALWAYS"}, "severity": "LOW", "applicationCriteriaSatisfy": "ANY",
            "applicationCriteria": crit, "fileAttributeSatisfy": "ANY", "fileAttribute": [],
            "dlpProfile": {"contentProfile": [_ref(n) for n in content], "edmProfile": [_ref(n) for n in edm]}}
    return {"enabled": False, "name": name, "description": "preflight probe (discarded)", "ruleType": "DATA", "dataRule": body}


class Prober:
    """Runs addRule probes inside one named draft revision per policy, then discards it."""

    ADD_TYPES = {"internetFirewall": "InternetFirewallAddRuleInput", "wanNetwork": "WanNetworkAddRuleInput",
                 "applicationControl": "ApplicationControlAddRuleInput", "siteWebProxy": "SiteWebProxyAddRuleInput"}

    def __init__(self, api, log):
        self.api, self.log, self.revisions = api, log, []

    def run(self, family, cases):
        """cases: list of (key, rule_dict). Returns key -> (status, [errors]) or {'_error': msg}."""
        api, out = self.api, {}
        rv = api.q('mutation r($a:ID!){ policy(accountId:$a){ %s{ createPolicyRevision(input:{name:"preflight-probe", '
                   'description:"preflight_check.py probe, discarded immediately"}){ status errors{errorCode errorMessage} '
                   'policy{ revision{ id } } } } } }' % family, allow_errors=True)
        try:
            rid = rv["data"]["policy"][family]["createPolicyRevision"]["policy"]["revision"]["id"]
        except (KeyError, TypeError):
            msg = (rv.get("errors") or [{}])[0].get("message") or json.dumps(rv)[:300]
            return {"_error": f"could not open a draft revision on {family}: {msg}"}
        self.revisions.append((family, rid))
        q_add = ('mutation a($a:ID!,$r:ID!,$i:%s!){ policy(accountId:$a){ %s(input:{revision:{id:$r}}){ '
                 'addRule(input:$i){ status errors{errorCode errorMessage} } } } }') % (self.ADD_TYPES[family], family)
        try:
            for key, rule in cases:
                r = api.q(q_add, {"r": rid, "i": {"rule": rule, "at": {"position": "LAST_IN_POLICY"}}}, allow_errors=True)
                if r.get("errors"):
                    out[key] = ("GQL_ERROR", [{"errorCode": "graphql", "errorMessage": r["errors"][0].get("message", ""),
                                               "path": r["errors"][0].get("path")}])
                else:
                    a = r["data"]["policy"][family]["addRule"]
                    out[key] = (a["status"], a.get("errors") or [])
        finally:
            d = api.q('mutation d($a:ID!,$r:ID!){ policy(accountId:$a){ %s(input:{revision:{id:$r}}){ '
                      'discardPolicyRevision(input:{id:$r}){ status } } } }' % family, {"r": rid}, allow_errors=True)
            ok = ((((d.get("data") or {}).get("policy") or {}).get(family) or {}).get("discardPolicyRevision") or {}).get("status")
            self.log(f"  probe revision on {family} discarded: {ok or d.get('errors')}")
        return out

    @staticmethod
    def entity_state(result, name):
        """MISSING / PRESENT / ERROR for a probe that referenced `name`."""
        status, errors = result
        if status == "SUCCESS":
            return "PRESENT", ""
        for e in errors:
            if e.get("errorCode") == "ruleHasInvalidEntity":
                return ("MISSING", e.get("errorMessage", "")) if name in e.get("errorMessage", "") else ("ERROR", e.get("errorMessage", ""))
        if status == "GQL_ERROR":
            return "ERROR", errors[0].get("errorMessage", "")
        return "PRESENT", "; ".join(e.get("errorMessage", "") for e in errors)  # entity resolved, rule failed later checks


def run_probes(api, refs, export, log):
    """Returns {'entities': {(kind, name): (state, msg)}, 'rbi': state, 'swp': state, 'revisions': [...]}"""
    prober, res = Prober(api, log), {"entities": {}, "features": {}}

    # Internet Firewall: custom categories, device posture, user notifications, RBI profiles, RBI action.
    cases, index = [("control", _ifw_rule("preflight-probe control"))], {}
    for kind, path in [("customCategory", ("destination", "customCategory")), ("device", ("device",)),
                       ("userNotification", ("actionConfig", "userNotification")), ("rbiProfile", ("actionConfig", "rbiProfile"))]:
        for n in sorted(refs.get(kind, {})):
            rule = _ifw_rule(f"preflight-probe {kind} {n}"[:250])
            tgt = rule
            for p in path[:-1]:
                tgt = tgt[p]
            tgt[path[-1]] = [_ref(n)]
            if kind == "rbiProfile":
                rule["action"] = "RBI"
                rule["destination"]["appCategory"] = [_ref("Uncategorized")]
            key = f"{kind}:{n}"
            cases.append((key, rule))
            index[key] = (kind, n)
    rbi_rules = [r for r in export["rules"] if r["raw"].get("action") == "RBI"]
    if rbi_rules:
        rule = _ifw_rule("preflight-probe RBI action")
        rule["action"] = "RBI"
        rule["destination"]["appCategory"] = [_ref("Uncategorized")]
        cases.append(("feature:rbi", rule))
    out = prober.run("internetFirewall", cases)
    if "_error" in out:
        res["error"] = out["_error"]
        return res
    for key, (kind, n) in index.items():
        res["entities"][(kind, n)] = Prober.entity_state(out[key], n)
    if "feature:rbi" in out:
        status, errs = out["feature:rbi"]
        msg = "; ".join(e.get("errorMessage", "") for e in errs)
        res["features"]["rbi"] = ("DISABLED", msg) if "RBI is disabled" in msg else (("ENABLED", "") if status == "SUCCESS" else ("ERROR", msg))
    if out.get("control", ("", []))[0] != "SUCCESS":
        res["warning"] = f"Internet Firewall control probe failed: {out.get('control')}"

    # WAN Network: bandwidth priorities beyond the defaults.
    prios = sorted(n for n in refs.get("bandwidthPriority", {}) if n not in DEFAULT_BANDWIDTH_PRIORITIES)
    for n in sorted(refs.get("bandwidthPriority", {})):
        if n in DEFAULT_BANDWIDTH_PRIORITIES:
            res["entities"][("bandwidthPriority", n)] = ("PRESENT", "built in")
    if prios:
        out = prober.run("wanNetwork", [(f"bw:{n}", _wnw_rule(f"preflight-probe bw {n}", n)) for n in prios])
        for n in prios:
            res["entities"][("bandwidthPriority", n)] = (Prober.entity_state(out[f"bw:{n}"], n)
                                                       if "_error" not in out else ("ERROR", out["_error"]))

    # App Control: DLP content and EDM profiles.
    dlp = [("contentProfile", n) for n in sorted(refs.get("contentProfile", {}))] + \
          [("edmProfile", n) for n in sorted(refs.get("edmProfile", {}))]
    if dlp:
        cases = [(f"{k}:{n}", _apc_data_rule(f"preflight-probe {k} {n}"[:250],
                                             content=[n] if k == "contentProfile" else [],
                                             edm=[n] if k == "edmProfile" else [])) for k, n in dlp]
        out = prober.run("applicationControl", cases)
        for k, n in dlp:
            res["entities"][(k, n)] = Prober.entity_state(out[f"{k}:{n}"], n) if "_error" not in out else ("ERROR", out["_error"])

    # Site Web Proxy: can a proxy rule be created at all?
    if any(r["family"] == "siteWebProxy" for r in export["rules"]):
        rule = {"enabled": False, "name": "preflight-probe swp", "description": "preflight probe (discarded)",
                "fqdn": "preflight-probe.invalid", "port": 8080, "associatedSite": [], "shouldAssociateWithAllSites": True,
                "authenticationConfig": {"method": "NONE"}}
        out = prober.run("siteWebProxy", [("swp", rule)])
        if "_error" in out:
            res["features"]["swp"] = ("ERROR", out["_error"])
        else:
            status, errs = out["swp"]
            msg = "; ".join(f'{e.get("errorMessage", "")} (path {e.get("path")})' if e.get("path") else e.get("errorMessage", "")
                            for e in errs)
            res["features"]["swp"] = ("ENABLED", "") if status == "SUCCESS" else ("FAILS", msg)
    res["revisions"] = prober.revisions
    return res


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def check_permissions(rep, exp, src, tgt):
    """Policies a key can't read: the export carries on without them (export_source.py), and a target key that
    can't read a policy can't deploy it either. Also policies the source has that the export lacks (an export cut short)."""
    cat = "access"
    for side, t, key in (("source", src, "perm_source"), ("target", tgt, "perm_target")):
        denied = sorted(f for f, info in (t.get("policies") or {}).items()
                        if "error" in info and _DENIED.search(info["error"]))
        if denied:
            perms = sorted({POLICY_PERMISSION.get(f, f) for f in denied})
            who = "current (source)" if side == "source" else "new (target)"
            rep.add(cat, "BLOCKER", key, {"n": len(denied), "families": denied, "permissions": perms},
                    f"The {who} account's API key cannot read {_pl(len(denied), 'policy', 'policies')}",
                    items=[f"{f}: needs the '{POLICY_PERMISSION.get(f, f)}' permission" for f in denied],
                    fix=("Use a key from an admin whose role can view the whole account (the predefined Viewer or Editor "
                         "role), or add these permissions to the key's role in the CMA, then run scripts/export_source.py "
                         "again. It carries on past a refused policy (`catocli export all` on its own stops there), "
                         "but that policy's rules stay out of the export until the key can read it." if side == "source" else
                         "Use a key from an admin whose role can edit the whole account (the predefined Editor role), or "
                         "add these permissions to the key's role in the CMA. A key that can't read a policy can't "
                         "deploy it either."))
    # export_source.py's record of what the export was refused or failed on (it carries on past refusals).
    report = exp.get("export_report") or {}
    live_denied = {f for f, info in (src.get("policies") or {}).items() if "error" in info and _DENIED.search(info["error"])}
    refused = [r for r in report.get("refused") or [] if EXPORT_TYPE_FAMILY.get(r.get("type")) not in live_denied]
    if refused:
        def _need(t):
            perm = POLICY_PERMISSION.get(EXPORT_TYPE_FAMILY.get(t))
            return f"needs the '{perm}' permission" if perm else "refused for lack of permission"
        perms = sorted({POLICY_PERMISSION[EXPORT_TYPE_FAMILY[r["type"]]] for r in refused if r["type"] in EXPORT_TYPE_FAMILY})
        rep.add(cat, "BLOCKER", "export_refused", {"n": len(refused), "types": [r["type"] for r in refused],
                                                   "permissions": perms},
                f"The export was refused {_pl(len(refused), 'type')} for lack of permission, so "
                f"{'it is' if len(refused) == 1 else 'they are'} missing from it",
                items=[f"{r['type']}: {_need(r['type'])}" for r in refused],
                detail=(report.get("refused") or [{}])[0].get("error", "")[:200],
                fix="Add the permissions to the source key's admin role (or use a key from an admin whose role can "
                    "view the whole account), then run scripts/export_source.py again.")
    if report.get("failed"):
        rep.add("export", "BLOCKER", "export_failed", {"n": len(report["failed"])},
                f"{_pl(len(report['failed']), 'export step')} failed",
                items=[f"{f.get('type')}: {str(f.get('error', ''))[:120]}" for f in report["failed"]],
                fix="Fix the cause in the error, then run scripts/export_source.py again.")
    readable = {f: info for f, info in (src.get("policies") or {}).items() if "error" not in info}
    missing = sorted(f for f, info in readable.items() if info.get("rules") and f not in exp["policy_enabled"])
    if missing:
        rep.add("export", "BLOCKER", "export_policy_missing", {"n": len(missing)},
                f"{_pl(len(missing), 'policy', 'policies')} with rules in the source are missing from the export",
                items=[f"{f}: {_pl(len(readable[f]['rules']), 'rule')}" for f in missing],
                fix="Export again and check its summary says every type succeeded (it can stop part way). Without "
                    "these files the migration silently leaves the policies out.")
    if src.get("policies") and tgt.get("policies") and not any(
            k in ("perm_source", "perm_target", "export_refused") for k in (f["key"] for f in rep.findings)):
        rep.add(cat, "OK", "perm_ok", {}, "Both API keys can read every policy")


def check_access(rep, src, tgt):
    cat = "access"
    sa, ta = src["account"], tgt["account"]
    rep.add(cat, "INFO", "access_accounts", {}, f"Source: {sa.get('name')} ({src['account_id']}, {sa.get('plan')}); "
                         f"target: {ta.get('name')} ({tgt['account_id']}, {ta.get('plan')})")
    if src["account_id"] == tgt["account_id"]:
        rep.add(cat, "BLOCKER", "access_same_account", {}, "Source and target are the same account", fix="Point --target-profile at the new tenant.")
    if (ta.get("plan") or "").upper() == "TRIAL":
        rep.add(cat, "WARN", "access_trial", {}, "Target is a TRIAL account: it reports no license or allocation data, so capacity "
                             "checks below can only tell you what to ask for, not whether it is there",
                fix="Confirm the numbers in CMA > Account > License (or with your Cato contact) before you start.")


def check_export(rep, exp, src):
    cat = "export"
    if exp["missing_files"]:
        rep.add(cat, "BLOCKER", "export_incomplete", {"n": len(exp["missing_files"])}, f"Source export in {exp['dir']} is incomplete", items=exp["missing_files"],
                fix="Run scripts/export_source.py again and check its summary (catocli can exit 0 after a connection "
                    "reset). It runs `catocli export all`, `catocli export static_hosts --site-type all` and "
                    "`scripts/export_webhooks.py`.")
    else:
        # partial: a policy the key may not read is missing however complete the rest is (check_permissions says which)
        partial = bool((exp.get("export_report") or {}).get("refused")) or any(
            "error" in info and _DENIED.search(info["error"]) for info in (src.get("policies") or {}).values())
        rep.add(cat, "OK", "export_ok", {"rules": len(exp["rules"]), "policies": len(exp["policy_enabled"]),
                                         "partial": partial}, f"Source export in {exp['dir']} has every file the bulk folders read "
                           f"({len(exp['rules'])} rules across {len(exp['policy_enabled'])} policies)")
    n_export, n_live = len(exp["static_hosts"]), src.get("host_total")
    if n_live is not None and n_export < n_live:
        rep.add(cat, "BLOCKER", "export_hosts_short", {"have": n_export, "need": n_live}, f"Static hosts: export has {n_export}, the source tenant has {n_live}",
                detail="`export all` only exports hosts on physical sockets by default.",
                fix="`catocli export static_hosts --site-type all -f json --output-directory <config dir>`")
    elif n_live is not None:
        rep.add(cat, "OK", "export_hosts_ok", {"n": n_live}, f"Static hosts: all {n_live} exported")
    if not exp["rules"]:
        rep.add(cat, "WARN", "export_no_rules", {}, "No policy rules found in the export")


def _site_license_use(licenses):
    """({site name or None: count of site licenses}, {site name: pooled Mbps}); None = unassigned."""
    ded, pool = defaultdict(int), {}
    for l in licenses:
        if l.get("status") not in (None, "ACTIVE"):
            continue
        if "site" in l and "allocatedBandwidth" not in l:
            ded[(l.get("site") or {}).get("name")] += 1
        for s in l.get("sites") or []:
            name = (s.get("site") or {}).get("name")
            if name:
                pool[name] = pool.get(name, 0) + (s.get("allocatedBandwidth") or 0)
    return dict(ded), pool


def check_licensing(rep, exp, src, tgt):
    cat = "licensing"
    need_ips = len(src["allocated_ips"]) or len(exp["allocated_ips"])
    t_pub = (tgt["allocations"] or {}).get("publicIps")
    already = len(tgt["allocated_ips"])
    if need_ips:
        if not t_pub:
            rep.add(cat, "ACTION", "lic_ips_unknown", {"need": need_ips}, f"Allocated (public) IPs: the source uses {need_ips}; the target reports no "
                                   f"public-IP license data ({already} already allocated there)",
                    fix=f"Make sure the target may allocate at least {need_ips} IPs before you deploy "
                        "0-dependencies. New and trial accounts often allow only a few, and every allocation past the "
                        "cap fails with 'You cannot allocate more IPs than allowed by your license'.")
        elif (t_pub.get("available") or 0) + already < need_ips:
            rep.add(cat, "BLOCKER", "lic_ips_short", {"need": need_ips, "total": t_pub.get("total")}, f"Allocated (public) IPs: the source uses {need_ips}, the target has "
                                    f"{t_pub.get('total')} licensed ({t_pub.get('available')} free)",
                    fix=f"Raise the target's public IP license to at least {need_ips}.")
        else:
            rep.add(cat, "OK", "lic_ips_ok", {"need": need_ips}, f"Allocated (public) IPs: need {need_ips}, target has {t_pub.get('available')} free "
                               f"of {t_pub.get('total')}")
    s_z = (src["allocations"] or {}).get("ztnaUsers") or {}
    t_z = (tgt["allocations"] or {}).get("ztnaUsers")
    if s_z.get("allocated"):
        if not t_z:
            rep.add(cat, "ACTION", "lic_ztna_unknown", {"need": s_z["allocated"]}, f"ZTNA users: {s_z['allocated']} allocated in the source; the target reports no "
                                   "ZTNA user license data", fix=f"License at least {s_z['allocated']} ZTNA users in the target.")
        elif (t_z.get("total") or 0) < s_z["allocated"]:
            rep.add(cat, "BLOCKER", "lic_ztna_short", {"need": s_z["allocated"], "total": t_z.get("total")}, f"ZTNA users: source uses {s_z['allocated']}, target is licensed for {t_z.get('total')}",
                    fix="Raise the target's ZTNA user license.")
        else:
            rep.add(cat, "OK", "lic_ztna_ok", {"need": s_z["allocated"]}, f"ZTNA users: need {s_z['allocated']}, target licensed for {t_z.get('total')}")
    # Sites: a site holds either its own site license or a share of pooled bandwidth. The export does not say
    # which, so read it from the source's licenses; a site with neither in the source needs none in the target.
    n_sites = len(exp["sites"])
    s_ded, s_pool = _site_license_use(src["licenses"])
    t_ded, t_pool = _site_license_use(tgt["licenses"])
    need_ded = sorted(n for n in exp["sites"] if n in s_ded)
    need_pool = {n: bw for n, bw in s_pool.items() if n in exp["sites"]}
    unlicensed = n_sites - len(need_ded) - len(need_pool)
    free_ded = t_ded.get(None, 0)
    free_pool = sum(max(0, (l.get("total") or 0) - (l.get("allocatedBandwidth") or 0))
                    for l in tgt["licenses"] if "allocatedBandwidth" in l)
    need_pool_bw = sum(need_pool.values())
    ctx = {"need": len(need_ded) + len(need_pool), "sites": n_sites, "ded": len(need_ded), "pool": len(need_pool),
           "pool_bw": need_pool_bw, "free_ded": free_ded, "free_pool": free_pool, "unlicensed": unlicensed}
    note = (" and ".join(x for x in (f"{_pl(len(need_ded), 'site')} with a site license" if need_ded else "",
                                     f"{_pl(len(need_pool), 'site')} on pooled bandwidth ({need_pool_bw} Mbps)" if need_pool else "")
                         if x) + " in the source" + (f"; {unlicensed} more have no license there either" if unlicensed else ""))
    if n_sites and ctx["need"]:
        if not tgt["licenses"]:
            rep.add(cat, "ACTION", "lic_sites_unknown", ctx, f"Sites: the target reports no site or pooled bandwidth licenses ({note})",
                    fix=f"Make sure the target has {_pl(len(need_ded), 'free site license')}"
                        + (f" and {need_pool_bw} Mbps of free pooled bandwidth" if need_pool else "") + ".")
        elif free_ded < len(need_ded) or free_pool < need_pool_bw:
            rep.add(cat, "BLOCKER", "lic_sites_short", ctx, f"Sites: the target has {_pl(free_ded, 'free site license')} and "
                                   f"{free_pool} Mbps of free pooled bandwidth ({note})",
                    fix="Add site licenses or pooled bandwidth to the target, or leave some sites out "
                        "(sites the target cannot license fail to create).")
        else:
            rep.add(cat, "OK", "lic_sites_ok", ctx, f"Sites: the target has {_pl(free_ded, 'free site license')} and "
                               f"{free_pool} Mbps of free pooled bandwidth ({note})")
    elif n_sites:
        rep.add(cat, "INFO", "lic_sites_info", ctx, f"Sites: none of the {n_sites} sites hold a license in the source")
    s_skus = {l["sku"] for l in src["licenses"] if l.get("status") in (None, "ACTIVE")}
    t_skus = {l["sku"] for l in tgt["licenses"] if l.get("status") in (None, "ACTIVE")}
    missing = sorted(s_skus - t_skus)
    if missing and t_skus:
        rep.add(cat, "WARN", "lic_skus_missing", {"n": len(missing)}, f"{len(missing)} license SKU(s) active in the source but not in the target",
                items=[f"{s} ({SKU_FEATURES.get(s, 'add-on')})" for s in missing],
                fix="Rules and settings that rely on these features may be rejected or behave differently.")
    elif missing:
        rep.add(cat, "ACTION", "lic_skus_unknown", {"n": len(missing)}, "Add-ons to match in the target (it reports no licenses to compare)",
                items=[f"{s} ({SKU_FEATURES.get(s, 'add-on')})" for s in missing])


def check_pops(rep, exp, src, tgt, refs):
    cat = "pops"
    needed = defaultdict(list)
    for ip in src["allocated_ips"]:
        needed[(ip.get("popLocation") or {}).get("name")].append(f"allocated IP {ip.get('ip')} ({ip.get('description') or ''})".strip())
    for n, rules in refs.get("popLocation", {}).items():
        needed[n].extend(rules)
    ip_by_name = {a.get("name"): (a.get("popLocation") or {}).get("name") for a in exp["allocated_ips"]}
    for s in exp["sites"].values():
        for side in ("primary", "secondary"):
            t = (s.get("ipsec") or {}).get(side) or {}
            pop = ip_by_name.get(t.get("public_cato_ip_name"))
            if pop:
                needed[pop].append(f"IPSec site {s['name']} ({side})")
    needed.pop(None, None)
    missing = {p: u for p, u in needed.items() if p not in tgt["locations"]}
    dep_rules = set()
    lost_ips = {a.get("name") for a in exp["allocated_ips"] if (a.get("popLocation") or {}).get("name") in missing}
    lost_sites = {s["name"] for s in exp["sites"].values()
                  if any(ip_by_name.get(((s.get("ipsec") or {}).get(side) or {}).get("public_cato_ip_name")) in missing
                         for side in ("primary", "secondary"))}
    for kind in ("allocationIp", "externalIp"):
        dep_rules |= _rules_for(refs, kind, lost_ips)
    for kind in ("site", "backhaulingSite", "associatedSite"):
        dep_rules |= _rules_for(refs, kind, lost_sites)
    for n, rules in refs.get("siteNetworkSubnet", {}).items():
        if n.split(" \\ ")[0] in lost_sites:
            dep_rules |= set(rules)
    for p in missing:
        dep_rules |= set(refs.get("popLocation", {}).get(p, []))
    if missing:
        lost_subnets = {n for n in refs.get("siteNetworkSubnet", {}) if n.split(" \\ ")[0] in lost_sites}
        rep.leave_out |= {(k, n) for n in lost_ips for k in ("allocationIp", "externalIp")}
        rep.leave_out |= {(k, n) for n in lost_sites for k in ("site", "backhaulingSite", "associatedSite")}
        rep.leave_out |= {("siteNetworkSubnet", n) for n in lost_subnets} | {("popLocation", p) for p in missing}
        rep.exclude["allocated_ips_exclude"] |= lost_ips
        rep.exclude["ipsec_sites_exclude"] |= lost_sites
        rep.add(cat, "BLOCKER", "pop_missing", {"n": len(missing), "names": sorted(missing)}, f"{len(missing)} PoP location(s) the source uses are not available to the target",
                items=[f"{p}: used by {len(u)} item{'s' if len(u) != 1 else ''}: {'; '.join(u)}" for p, u in sorted(missing.items())],
                fix="China and private PoPs need their own entitlement. Get it added, or leave these out: "
                    f"`allocated_ips_exclude = {json.dumps(sorted(lost_ips))}` in 0-dependencies and "
                    f"`ipsec_sites_exclude = {json.dumps(sorted(lost_sites))}` in 1-sites (the exclude-*.auto.tfvars "
                    "files written next to this report hold exactly these), plus scope_command.sh for the rules.",
                rules=dep_rules)
    rep.add(cat, "OK" if not missing else "INFO", "pop_summary", {"ok": len(needed) - len(missing), "n": len(needed)},
            f"{len(needed) - len(missing)} of {len(needed)} PoP locations used by the source are available to the target")


def _user_key(u):
    return (u.get("displayName") or f"{u.get('firstName', '')} {u.get('lastName', '')}").strip()


def check_identity(rep, exp, src, tgt, refs, target_api):
    cat = "identity"
    s_groups = {g["name"]: g for g in src["user_groups"]}
    t_groups = {g["name"]: g for g in tgt["user_groups"]}
    s_users = {}
    for u in src["users"]:
        s_users.setdefault(_user_key(u), u)
    t_user_names = {_user_key(u) for u in tgt["users"]}
    t_user_emails = {(u.get("email") or "").lower() for u in tgt["users"]}

    by_type, missing_rules, type_rules = defaultdict(list), set(), defaultdict(set)
    kinds = defaultdict(Counter)  # itype -> {"groups": n, "users": n}
    for name, rules in sorted(refs.get("usersGroup", {}).items()):
        if name in t_groups:
            continue
        itype = (s_groups.get(name) or {}).get("importType") or "NOT IN SOURCE"
        by_type[itype].append(f"user group '{name}' ({len(rules)} rule(s))")
        kinds[itype]["groups"] += 1
        rep.leave_out.add(("usersGroup", name))
        missing_rules.update(rules)
        type_rules[itype].update(rules)
    for name, rules in sorted(refs.get("user", {}).items()):
        su = s_users.get(name)
        if name in t_user_names or (su and (su.get("email") or "").lower() in t_user_emails):
            continue
        itype = (su or {}).get("userImportType") or "NOT IN SOURCE"
        by_type[itype].append(f"user '{name}' ({len(rules)} rule(s))")
        kinds[itype]["users"] += 1
        rep.leave_out.add(("user", name))
        missing_rules.update(rules)
        type_rules[itype].update(rules)
    fixes = {
        "SCIM": "Set up a SCIM app in the target (CMA > Access > Directory Services > SCIM) and provision from the IdP, "
                "or push from the source with `scripts/scim_sync.py export` then `push --target-profile <p>` "
                "(the target profile needs scim_url and scim_token). Groups must exist before the policies apply.",
        "LDAP": "Connect the target to the same directory (Directory Services > LDAP/AD) and sync these users/groups.",
        "MANUAL": "Create the user groups in the target CMA with the same names and members: the public API has no "
                  "mutation for user groups (`groups.createGroup` makes Advanced Groups, a different object). Manual "
                  "users can be created in the CMA or with `user.createUser` (Beta), which sets name, email and "
                  "department but not group membership. A SCIM push can also create the groups, but they then "
                  "belong to SCIM rather than MANUAL.",
        "DYNAMIC": "Turn on dynamic user groups (department) in the target, in the CMA or with "
                   "`user.updateDynamicUserGroupsConfiguration` (Beta), and make sure the attribute is populated.",
        "INTEGRATION": "Recreate the identity integration that provides these in the source.",
        "NOT IN SOURCE": "These names are in the export but not in the source tenant's user list: stale references. "
                         "Tidy the rules in the source, or leave them out of the run.",
    }
    for itype in sorted(by_type):
        rep.add(cat, "BLOCKER", "id_missing", {"type": itype, "n": len(by_type[itype]),
                                               "groups": kinds[itype]["groups"], "users": kinds[itype]["users"]},
                f"{_pl(len(by_type[itype]), itype + ' identity', itype + ' identities')} the rules name "
                f"{'is' if len(by_type[itype]) == 1 else 'are'} missing in the target",
                items=by_type[itype], fix=fixes.get(itype, ""), rules=type_rules[itype])
    total_refs = len(refs.get("usersGroup", {})) + len(refs.get("user", {}))
    if total_refs and not by_type:
        rep.add(cat, "OK", "id_ok", {"n": total_refs}, f"All {total_refs} users and user groups the rules name exist in the target")
    if missing_rules:
        rep.add(cat, "INFO", "id_rules", {"n": len(missing_rules)}, f"{len(missing_rules)} rules name at least one missing identity and will fail until it exists")

    s_types = Counter(u.get("userImportType") for u in src["users"])
    t_types = Counter(u.get("userImportType") for u in tgt["users"])
    s_dirs = {u.get("directoryId") for u in src["users"] if u.get("directoryId")}
    rep.add(cat, "INFO", "id_source_dir", {}, f"Source directory: {len(src['users'])} users ({dict(s_types)}), "
                         f"{len(src['user_groups'])} user groups, {len(s_dirs)} directory id(s)")
    rep.add(cat, "INFO", "id_target_dir", {}, f"Target directory: {len(tgt['users'])} users ({dict(t_types)}), {len(tgt['user_groups'])} user groups")
    if s_types.get("SCIM") and not t_types.get("SCIM"):
        rep.add(cat, "ACTION", "id_scim_inactive", {}, "The source provisions users over SCIM; the target has no SCIM-provisioned users yet",
                detail="Either no SCIM integration exists in the target or it has not synced. The API cannot read the "
                       "IdP/SCIM configuration itself, so this is inferred from the user list.",
                fix="Create the SCIM app in the target, point the IdP (Entra ID, Okta, ...) at it, assign the groups "
                    "the rules use and run a sync before the policies.")
    if s_types.get("LDAP") and not t_types.get("LDAP"):
        rep.add(cat, "ACTION", "id_ldap_inactive", {}, "The source imports users from LDAP/AD; the target has none",
                fix="Connect the target to the directory and sync before the policies.")

    if target_api.scim_url and target_api._scim_token:
        try:
            st_u, body_u = target_api.scim_get("/Users?count=1")
            st_g, body_g = target_api.scim_get("/Groups?count=1")
            rep.add(cat, "OK", "id_scim_endpoint_ok", {}, f"Target SCIM endpoint answers (Users {st_u}: {body_u.get('totalResults')} total, "
                               f"Groups {st_g}: {body_g.get('totalResults')} total)")
        except Exception as e:  # noqa: BLE001 - report any failure as a finding
            rep.add(cat, "BLOCKER", "id_scim_endpoint_fail", {}, "Target SCIM endpoint in the profile does not answer", detail=str(e)[:300],
                    fix="Check scim_url and scim_token in the target profile (Directory Services > SCIM in the CMA).")
    else:
        rep.add(cat, "INFO", "id_scim_not_tested", {}, "No scim_url/scim_token in the target profile, so the SCIM endpoint was not tested",
                fix="Add both to the profile in ~/.cato/credentials to validate the SCIM app (and to use scim_sync.py).")


def check_cma_only(rep, refs, probes):
    cat = "cma-only"
    for kind, (one, many) in CMA_ONLY_KINDS.items():
        names = refs.get(kind, {})
        if kind == "bandwidthPriority":
            names = {n: r for n, r in names.items() if n not in DEFAULT_BANDWIDTH_PRIORITIES}
        if not names:
            continue
        noun = lambda k: one if k == 1 else many  # noqa: E731
        if probes and "entities" in probes:
            states = {n: probes["entities"].get((kind, n), ("UNKNOWN", "")) for n in names}
            missing = sorted(n for n, (st, _) in states.items() if st == "MISSING")
            unknown = sorted(n for n, (st, _) in states.items() if st not in ("MISSING", "PRESENT"))
            if missing:
                rep.leave_out |= {(kind, n) for n in missing}
                rep.add(cat, "BLOCKER", "cma_missing", {"kind": kind, "n": len(missing), "one": one, "many": many}, f"{len(missing)} {noun(len(missing))} missing in the target (verified by probe)",
                        items=[f"{n}: {_uses(refs, kind, n)}" for n in missing], rules=_rules_for(refs, kind, missing),
                        fix="Create them in the target CMA with exactly the same names. The API cannot create or list them.")
            if unknown:
                rep.add(cat, "WARN", "cma_undecided", {"kind": kind, "n": len(unknown), "one": one, "many": many, "names": unknown},
                        f"{len(unknown)} {noun(len(unknown))} the probe could not decide",
                        items=[f"{n}: {states[n][1][:160]}" for n in unknown], rules=_rules_for(refs, kind, unknown))
            present = len(names) - len(missing) - len(unknown)
            if present:
                rep.add(cat, "OK", "cma_present", {"kind": kind, "n": present, "one": one, "many": many}, f"{present} {noun(present)} the rules use already {'exists' if present == 1 else 'exist'} in the target",
                        items=sorted(n for n, (st, _) in states.items() if st == "PRESENT"))
        else:
            rep.add(cat, "ACTION", "cma_unverified", {"kind": kind, "n": len(names), "one": one, "many": many}, f"{len(names)} {noun(len(names))} the rules use must exist in the target "
                                   "(the API cannot list them; rerun with --probe to verify)",
                    items=[f"{n}: {_uses(refs, kind, n)}" for n in sorted(names)], rules=_rules_for(refs, kind, names),
                    fix="Create them in the target CMA with exactly the same names before the policies.")


def _deps_state_has_ranges(folder=None):
    """True once 0-dependencies has deployed its global IP ranges (local state)."""
    path = os.path.join(folder or os.path.join(REPO_ROOT, "0-dependencies"), "terraform.tfstate")
    try:
        with open(path) as fh:
            return '"cato_global_ip_ranges"' in fh.read()
    except OSError:
        return False


def check_features(rep, exp, refs, probes, tgt=None, deps_deployed=None, lan_l7_confirmed=False):
    cat = "features"
    rbi_rules = [f'{r["family"]}: {r["name"]}' for r in exp["rules"] if r["raw"].get("action") == "RBI"]
    if rbi_rules:
        state = ((probes or {}).get("features") or {}).get("rbi")
        if state and state[0] == "DISABLED":
            rep.skip(rbi_rules)
            rep.add(cat, "BLOCKER", "feat_rbi_disabled", {"n": len(rbi_rules)}, "Remote Browser Isolation is disabled in the target (verified by probe)",
                    items=rbi_rules, rules=rbi_rules, fix="Enable RBI on the target (license/feature) before the Internet Firewall.")
        elif state and state[0] == "ENABLED":
            rep.add(cat, "OK", "feat_rbi_ok", {"n": len(rbi_rules)}, f"Remote Browser Isolation is enabled in the target ({len(rbi_rules)} rule(s) use it)")
        else:
            rep.add(cat, "ACTION", "feat_rbi_unverified", {"n": len(rbi_rules)}, f"{len(rbi_rules)} rule(s) use the RBI action: RBI must be enabled in the target",
                    items=rbi_rules, rules=rbi_rules, fix="Enable RBI, or rerun with --probe to verify.")
    swp_rules = [f'{r["family"]}: {r["name"]}' for r in exp["rules"] if r["family"] == "siteWebProxy"]
    if swp_rules:
        state = ((probes or {}).get("features") or {}).get("swp")
        if state and state[0] == "ENABLED":
            rep.add(cat, "OK", "feat_swp_ok", {}, "Site Web Proxy rules can be created in the target")
        elif state:
            rep.skip(swp_rules)
            rep.add(cat, "BLOCKER", "feat_swp_fail", {"n": len(swp_rules)}, "Site Web Proxy rules cannot be created in the target (verified by probe)",
                    detail=state[1][:300], items=swp_rules, rules=swp_rules,
                    fix="Ask Cato to enable Site Web Proxy for the target account. 'Unsupported field [addRule]' with "
                        "path ...rule.transportProtocol comes from the API itself, not from Terraform.")
        else:
            rep.add(cat, "ACTION", "feat_swp_unverified", {"n": len(swp_rules)}, f"{len(swp_rules)} Site Web Proxy rule(s): the feature must be enabled in the target",
                    items=swp_rules, rules=swp_rules, fix="Enable Site Web Proxy, or rerun with --probe to verify.")

    translated = []
    for s in exp["sites"].values():
        nr = s.get("native_range") or {}
        if nr.get("translated_subnet") and nr.get("subnet") and nr["translated_subnet"].strip() != nr["subnet"].strip():
            translated.append(f'{s["name"]}: native {nr["subnet"]} -> {nr["translated_subnet"]}')
        for lan in s.get("lan_interfaces") or []:
            for r in lan.get("network_ranges") or []:
                ts, sn = (r.get("translated_subnet") or "").strip(), (r.get("subnet") or "").strip()
                if ts and sn and ts != sn:
                    translated.append(f'{s["name"]} / {r.get("name")}: {sn} -> {ts}')
    if translated:
        rep.add(cat, "ACTION", "feat_srt", {"n": len(translated)}, f"{len(translated)} network range(s) use real Static Range Translation",
                items=translated, fix="Enable Static Range Translation for the target account in the CMA (Resources > "
                                      "System Settings) before the sites go in. The API reads and sets each range's "
                                      "translated subnet, and refuses one while the feature is off, but has no call "
                                      "for the account-level switch.")
    else:
        rep.add(cat, "OK", "feat_srt_ok", {}, "No real Static Range Translation in the source (identity translations are skipped "
                           "by the sites module)")

    l7, l7_rules = [], []
    for r in exp["rules"]:
        if not r.get("lan_firewall"):
            continue
        raw = r["raw"]
        app = raw.get("application") or {}
        svc = raw.get("service") or {}
        uses_l7 = any(app.get(k) for k in ("application", "customApp", "appCategory", "domain", "fqdn")) or svc.get("standard")
        if uses_l7:
            scope = r["network_rule"].get("site") or {}
            sites = sorted({x["name"] for x in scope.get("site") or []} | {f'group {x["name"]}' for x in scope.get("group") or []})
            l7.append(f'{r["name"]} (sites: {", ".join(sites) or "every site the network rule covers"})')
            l7_rules.append(f'{r["family"]}: {r["name"]}')
    if l7 and lan_l7_confirmed:
        sites = sorted({x.rsplit("(sites: ", 1)[-1].rstrip(")") for x in l7})
        rep.add(cat, "OK", "feat_l7_confirmed", {"n": len(l7), "sites": sites},
                f"LAN Layer 7 enforcement confirmed on for {', '.join(sites)} (--lan-l7-confirmed; the API cannot read it)",
                items=l7)
    elif l7:
        rep.skip(l7_rules)
        rep.add(cat, "ACTION", "feat_l7", {"n": len(l7), "sites": sorted({x.rsplit("(sites: ", 1)[-1].rstrip(")") for x in l7})}, f"{len(l7)} LAN firewall rule(s) match on applications or services (Layer 7)",
                items=l7, rules=l7_rules,
                fix="Turn on LAN Layer 7 enforcement for those sites in the target CMA after the sites exist, or the "
                    "LAN firewall cannot publish ('Can't use L7 with sites that don't have L7 enforcement enabled'). "
                    "The API cannot set it.")

    # Ranges holding 169.254.169.254 can only be made by hand. 0-dependencies' first deploy sends every
    # range it is given in one request (the API would block this one); after that it owns every range in
    # the account, so a range that exists must NOT stay in global_ip_ranges_exclude or it plans to delete it.
    waf = [g.get("name") for g in exp["global_ip_ranges"] if "169.254.169.254" in json.dumps(g)]
    if waf:
        have = (tgt or {}).get("global_ranges") or set()
        deployed = _deps_state_has_ranges() if deps_deployed is None else deps_deployed
        todo = [n for n in waf if n not in have]
        made = [n for n in waf if n in have]
        if todo:
            rep.leave_out |= {("globalIpRange", n) for n in todo}
            rep.exclude["global_ip_ranges_exclude"] |= set(todo)
            rep.add(cat, "BLOCKER", "feat_waf_range", {"n": len(todo), "names": todo}, "Global IP range(s) containing 169.254.169.254: the API's web firewall rejects any "
                                    "request that carries this address", items=todo, rules=_rules_for(refs, "globalIpRange", todo),
                    fix="Leave them out with `global_ip_ranges_exclude` (exclude-0-dependencies.auto.tfvars, written next to "
                        "this report, does that) and create them by hand in the target CMA AFTER 0-dependencies has been "
                        "deployed once. Then run the check again: it sees them, drops them from the exclusion file, and the "
                        "rules that use them come back in.")
        if made and deployed:
            rep.add(cat, "OK", "feat_waf_range_done", {"n": len(made)}, f"{len(made)} range(s) the API firewall blocks exist in the target "
                                "(made by hand) and stay in", items=made)
        elif made:
            rep.exclude["global_ip_ranges_exclude"] |= set(made)
            rep.add(cat, "ACTION", "feat_waf_range_early", {"n": len(made), "names": made}, f"{len(made)} range(s) the API firewall blocks were made by hand "
                                    "before 0-dependencies was ever deployed", items=made,
                    fix="They stay in `global_ip_ranges_exclude` for the first deploy of 0-dependencies, which sends every "
                        "range it is given in one request. Straight after that deploy, run the check again and copy the new "
                        "exclude-0-dependencies.auto.tfvars before 0-dependencies is deployed again: from then on it owns "
                        "every range in the account and would delete one it is told to leave out.")

    sanctioned = _rules_for(refs, "sanctionedAppsCategory", set(refs.get("sanctionedAppsCategory", {})))
    if sanctioned:
        rep.add(cat, "ACTION", "feat_sanctioned", {"n": len(sanctioned)}, f"{_pl(len(sanctioned), 'rule')} match on the Sanctioned Apps "
                                "category, whose contents are an account setting the migration does not copy",
                items=sorted(sanctioned), rules=sanctioned,
                fix="Mark the same applications as sanctioned in the target before the policies, or these rules deploy "
                    "cleanly but match different traffic. The API cannot read or set the list.")

    wifi = []
    for s in exp["sites"].values():
        for w in s.get("wifi_ssids") or []:
            if ((w.get("security") or {}).get("mode") or "OPEN") != "OPEN":
                wifi.append(f'{s["name"]}:{w.get("name")}')
    if wifi:
        rep.add(cat, "ACTION", "feat_wifi", {"n": len(wifi)}, f"{len(wifi)} Wi-Fi SSID(s) need their passwords (exports carry CHANGE_ME)",
                items=wifi, fix="Put them in `wifi_ssid_passkeys` in a gitignored 1-sites/*.auto.tfvars.")
    if exp["webhooks"]:
        rep.add(cat, "WARN", "feat_webhooks", {"n": len(exp["webhooks"])}, f"{len(exp['webhooks'])} webhook(s) copy across pointing at the source's real endpoints",
                items=[f'{w.get("name")}: {w.get("url", "")[:60]}' for w in exp["webhooks"]],
                fix="In a test tenant, disable or repoint them; set real secrets via `webhook_secrets`.")
    ipsec = [s["name"] for s in exp["sites"].values() if s["_kind"] == "ipsec"]
    if ipsec:
        rep.add(cat, "INFO", "feat_ipsec", {"n": len(ipsec)}, f"{len(ipsec)} IPSec site(s): tunnels and pre-shared keys are not exported", items=ipsec,
                fix="Add tunnels and PSKs after the sites exist.")


def check_integrity(rep, exp, src, refs):
    cat = "integrity"
    classic_all = {n: r for n, r in refs.get("group", {}).items() if n not in src["groups"]}
    converted = {g.get("name") for g in exp["groups"]}
    done = sorted(n for n in classic_all if n in converted)
    classic = {n: r for n, r in classic_all.items() if n not in converted}
    if done:
        rep.add(cat, "OK", "int_classic_done", {"n": len(done)}, f"{len(done)} classic group(s) the API cannot see are already in groups.json as Advanced Groups",
                items=done)
    if classic:
        rep.leave_out |= {("group", n) for n in classic}
        rep.add(cat, "ACTION", "int_classic", {"n": len(classic)}, f"{len(classic)} group(s) the rules use are invisible to the groups API (classic groups)",
                items=[f"{n}: {len(r)} rule(s)" for n, r in sorted(classic.items())], rules=_rules_for(refs, "group", classic),
                fix="Read their members in the source CMA and add them to config_data/groups.json as Advanced Groups "
                    "(keep empty ones empty: an empty group matches nothing; dropping it from a rule matches everything).")
    site_names = set(exp["sites"])
    stale = defaultdict(set)
    stale_refs = set()
    for kind in ("site", "backhaulingSite", "associatedSite"):
        for n, rules in refs.get(kind, {}).items():
            if n not in site_names:
                stale[f"site '{n}'"].update(rules)
                stale_refs.add((kind, n))
    for n, rules in refs.get("siteNetworkSubnet", {}).items():
        site = n.split(" \\ ")[0]
        if site not in site_names:
            stale[f"subnet '{n}'"].update(rules)
            stale_refs.add(("siteNetworkSubnet", n))
    ga = {g.get("name") for g in exp["global_ip_ranges"]}
    for n, rules in refs.get("globalIpRange", {}).items():
        if n not in ga:
            stale[f"global IP range '{n}'"].update(rules)
            stale_refs.add(("globalIpRange", n))
    ca = {c.get("name") for c in exp["custom_apps"]} if isinstance(exp["custom_apps"], list) else set()
    for n, rules in refs.get("customApp", {}).items():
        if ca and n not in ca:
            stale[f"custom app '{n}'"].update(rules)
            stale_refs.add(("customApp", n))
    an = {a.get("name") for a in exp["allocated_ips"]}
    for kind in ("allocationIp", "externalIp"):
        for n, rules in refs.get(kind, {}).items():
            if n not in an:
                stale[f"allocated IP '{n}'"].update(rules)
                stale_refs.add((kind, n))
    rep.leave_out |= stale_refs
    unchecked = {(k, n): r for k in UNCHECKED_KINDS for n, r in refs.get(k, {}).items()}
    if unchecked:
        rep.add(cat, "WARN", "int_unchecked", {"n": len(unchecked)}, f"{_pl(len(unchecked), 'object')} the rules name that this "
                               "check cannot verify and the migration does not create",
                items=[f"{UNCHECKED_KINDS[k]} '{n}': {_pl(len(r), 'rule')}, e.g. {r[0]}" for (k, n), r in sorted(unchecked.items())],
                rules=set().union(*map(set, unchecked.values())),
                fix="Check each exists in the target with exactly the same name before the policies (network interfaces "
                    "come with their sites; floating subnets and value sets are made in the CMA).")
    if stale:
        rep.add(cat, "WARN", "int_stale", {"n": len(stale)}, f"{len(stale)} reference(s) point at objects that are not in the source export",
                items=[f"{k}: {len(v)} rule(s), e.g. {sorted(v)[0]}" for k, v in sorted(stale.items())],
                rules=set().union(*stale.values()),
                fix="Usually leftovers in the source (deleted sites/ranges). Tidy them there, or leave the rules out.")
    else:
        rep.add(cat, "OK", "int_ok", {}, "Every site, range, custom app and allocated IP the rules name is in the export")


def check_target_state(rep, exp, tgt):
    cat = "target"
    src_names = defaultdict(set)
    for r in exp["rules"]:
        if not r.get("lan_firewall"):
            src_names[r["family"]].add(r["name"])
    clashes, existing = [], []
    for fam, info in tgt["policies"].items():
        if "error" in info:
            if not _DENIED.search(info["error"]):  # access denied is a perm_target BLOCKER (check_permissions)
                rep.add(cat, "WARN", "tgt_unreadable", {"family": fam}, f"Could not read the target's {fam} policy",
                        detail=info["error"][:200])
            continue
        names = set(info["rules"])
        if names:
            existing.append(f"{fam}: {len(names)} rule(s)")
        both = sorted(names & src_names.get(fam, set()))
        if both:
            clashes.append(f"{fam}: {len(both)} ({', '.join(both[:3])}{', ...' if len(both) > 3 else ''})")
    if clashes:
        rep.add(cat, "ACTION", "tgt_clash", {"n": sum(int(c.split(": ", 1)[1].split(" ")[0]) for c in clashes),
                                             "families": [c.split(": ", 1)[0] for c in clashes]}, "Rules already in the target share names with source rules "
                               "(factory/sample rules, or a previous run)",
                items=clashes, fix="New tenant: review, then clear them with `scripts/purge_all.py --profile <target> "
                                   "--only <families>` (dry run by default; --execute deletes). Re-run of an earlier "
                                   "migration: leave them, Terraform state already owns them.")
    if existing and not clashes:
        rep.add(cat, "WARN", "tgt_rules_existing", {}, "The target already has rules (no name clashes)", items=existing)
    elif existing:
        rep.add(cat, "INFO", "tgt_rules_info", {}, "Rules already in the target", items=existing)
    for label, have in (("sites", tgt["sites"]), ("groups", tgt["groups"])):
        both = sorted(have & (set(exp["sites"]) if label == "sites" else {g.get("name") for g in exp["groups"]}))
        if both:
            rep.add(cat, "WARN", "tgt_existing_named", {"label": label, "n": len(both)}, f"{len(both)} {label} with source names already exist in the target",
                    items=both, fix="A greenfield run expects them absent: import them (brownfield) or remove them first.")
        elif have:
            rep.add(cat, "INFO", "tgt_existing_info", {"label": label, "n": len(have)}, f"Target already has {len(have)} {label} (no name clashes)")
    if tgt["allocated_ips"]:
        rep.add(cat, "INFO", "tgt_allocated", {"n": len(tgt["allocated_ips"])}, f"Target already has {len(tgt['allocated_ips'])} allocated IP(s); they count against the cap")
    diffs = [f"{fam}: source {'on' if on else 'off'}, target {'on' if tgt['policies'][fam].get('enabled') else 'off'}"
             for fam, on in sorted(exp["policy_enabled"].items())
             if fam in tgt["policies"] and "error" not in tgt["policies"][fam] and tgt["policies"][fam].get("enabled") != on]
    if diffs:
        rep.add(cat, "INFO", "tgt_toggles", {"n": len(diffs)}, f"{len(diffs)} policy on/off switch(es) differ today (the policy folders set them to match)",
                items=diffs)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
CATEGORY_TITLES = [
    ("access", "Access and accounts"), ("export", "Source export"), ("licensing", "Licensing and capacity"),
    ("pops", "PoP locations"), ("identity", "Users, groups, SCIM and IdP"), ("cma-only", "Objects only the CMA can create"),
    ("features", "Features and account settings"), ("integrity", "Source integrity"), ("target", "Target state"),
]


def blocked_rule_count(rep, severities=("BLOCKER",)):
    return len({r for f in rep.findings if f["severity"] in severities for r in f["rules"]})


def rule_totals(exp):
    nested = sum(1 for r in exp["rules"] if r.get("lan_firewall"))
    return len(exp["rules"]), nested


def render_text(rep, exp, meta):
    lines = [f"Pre-migration dependency check: {meta['source']} -> {meta['target']}"
             f"{'  (with probes)' if meta['probe'] else ''}", ""]
    for cat, title in CATEGORY_TITLES:
        fs = sorted([f for f in rep.findings if f["category"] == cat], key=lambda f: SEVERITIES.index(f["severity"]))
        if not fs:
            continue
        lines.append(f"== {title}")
        for f in fs:
            lines.append(f"  {SEV_ICON[f['severity']]:5s} {f['severity']:7s} {f['title']}")
            if f["detail"]:
                lines.append(f"                {f['detail']}")
            for it in f["items"][:12]:
                lines.append(f"                - {it}")
            if len(f["items"]) > 12:
                lines.append(f"                - ... and {len(f['items']) - 12} more (see the report)")
            if f["fix"] and f["severity"] in ("BLOCKER", "ACTION", "WARN"):
                lines.append(f"                fix: {f['fix']}")
        lines.append("")
    c = rep.counts()
    lines.append("Summary: " + ", ".join(f"{c[s]} {s}" for s in SEVERITIES))
    total, nested = rule_totals(exp)
    lines.append(f"Rules that will fail until the BLOCKERs are fixed: {blocked_rule_count(rep)} of {total} "
                 f"(the {total} are {total - nested} policy rules plus {nested} nested LAN firewall "
                 f"{'rule' if nested == 1 else 'rules'})" if nested else
                 f"Rules that will fail until the BLOCKERs are fixed: {blocked_rule_count(rep)} of {total}")
    lines.append(f"Rules that also depend on an ACTION item: {blocked_rule_count(rep, ('BLOCKER', 'ACTION'))} of {total}")
    return "\n".join(lines)


def render_markdown(rep, exp, meta):
    c = rep.counts()
    out = [f"# Pre-migration dependency check", "",
           f"Source **{meta['source']}**, target **{meta['target']}**, run {meta['when']}"
           f"{' with probes' if meta['probe'] else ' (read-only)'}.", "",
           "| Blockers | Actions | Warnings | Info | OK | Rules blocked | Rules blocked or at risk |",
           "|---:|---:|---:|---:|---:|---:|---:|",
           f"| {c['BLOCKER']} | {c['ACTION']} | {c['WARN']} | {c['INFO']} | {c['OK']} | "
           f"{blocked_rule_count(rep)} of {rule_totals(exp)[0]} | {blocked_rule_count(rep, ('BLOCKER', 'ACTION'))} of {rule_totals(exp)[0]} |", "",
           "BLOCKER: the migration will fail until this is done. ACTION: do this before you migrate (or confirm it "
           "is done). WARN: review. INFO and OK: for the record.", ""]
    for cat, title in CATEGORY_TITLES:
        fs = sorted([f for f in rep.findings if f["category"] == cat], key=lambda f: SEVERITIES.index(f["severity"]))
        if not fs:
            continue
        out += [f"## {title}", ""]
        for f in fs:
            out.append(f"**{f['severity']}**: {f['title']}")
            if f["detail"]:
                out.append(f"\n{f['detail']}")
            if f["items"]:
                out.append("")
                out += [f"- {it}" for it in f["items"]]
            if f["fix"] and f["severity"] != "OK":
                out.append(f"\n*What to do:* {f['fix']}")
            out.append("")
    titles = {f["key"]: guide(f)["title"] for f in _shown(rep)}
    out += render_runbook_md(build_runbook(rep, meta), titles)
    if meta.get("revisions"):
        out += ["## Probe revisions", "", "Created and discarded on the target during this run:", ""]
        out += [f"- {fam}: {rid}" for fam, rid in meta["revisions"]]
    return "\n".join(out) + "\n"


def provider_mirror():
    """(pinned provider version, True if a local build of it sits in ~/.terraform.d/mirror for bulk_apply --mirror)."""
    try:
        with open(os.path.join(REPO_ROOT, "0-dependencies", "versions.tf")) as fh:
            m = re.search(r'version\s*=\s*"=?\s*([0-9][^"]*)"', fh.read())
    except OSError:
        m = None
    ver = m.group(1).strip() if m else None
    if not ver:
        return None, False
    pattern = os.path.expanduser(f"~/.terraform.d/mirror/registry.terraform.io/catonetworks/cato/{ver}/*/"
                                 f"terraform-provider-cato_v{ver}*")
    return ver, bool(glob.glob(pattern))


def run_info(args, src_api, tgt_api):
    """What the runbook in the reports needs to print exact commands for this pair of accounts."""
    def rel(p):
        p = os.path.abspath(p)
        r = os.path.relpath(p, REPO_ROOT)
        return r if not r.startswith(".." + os.sep + ".." + os.sep + "..") else p
    ver, mirror = provider_mirror()
    return {"source_profile": args.source_profile, "target_profile": args.target_profile,
            "source_account": str(src_api.account), "target_account": str(tgt_api.account),
            "config_dir": rel(args.config_dir), "target_config_dir": rel(os.path.abspath(args.config_dir).rstrip(os.sep) + "_target"),
            "out_dir": rel(args.out_dir), "probe": bool(args.probe), "customer": args.customer,
            "lan_l7_confirmed": bool(args.lan_l7_confirmed), "provider_version": ver, "mirror": mirror}


def write_leave_out(rep, config_dir, out_dir):
    """Write what it takes to leave out everything the target still lacks.

    scope_command.sh rebuilds <config_dir>_target from the export minus every rule
    that waits on something still missing (an identical copy when nothing is), and
    exclude-0-dependencies.auto.tfvars / exclude-1-sites.auto.tfvars hold the
    exclusion lists for those two folders. Rerun the check after fixing anything
    and the files are rewritten to match. Wi-Fi passwords are never written here:
    a placeholder would be accepted by 1-sites and go live as the real password.
    """
    import shlex
    config_dir = os.path.abspath(config_dir)
    out_dir = os.path.abspath(out_dir)
    target_dir = config_dir.rstrip(os.sep) + "_target"
    args = ["python3", os.path.join(REPO_ROOT, "scripts", "scope_policies_to_target.py"),
            "--config-dir", config_dir, "--out", target_dir,
            "--report", os.path.join(out_dir, "scope_report.json")]
    extra = [f"--missing {shlex.quote(f'{k}={n}')}" for k, n in sorted(rep.leave_out) if n] + \
            [f"--skip-rule {shlex.quote(n)}" for n in sorted(rep.skip_rules) if n]
    lines = ["#!/bin/sh",
             "# Written by preflight_check.py. Makes " + target_dir,
             "# from the export, leaving out every rule that waits on something the target still lacks",
             f"# ({len(rep.leave_out)} missing reference(s), {len(rep.skip_rules)} rule(s) left out by name).",
             "# Fix what you can, run the check again, and this file is rewritten to match.",
             " ".join(shlex.quote(a) for a in args) + (" \\" if extra else "")]
    lines += [f"  {a}" + (" \\" if i < len(extra) - 1 else "") for i, a in enumerate(extra)]
    with open(os.path.join(out_dir, "scope_command.sh"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    folders = {"0-dependencies": ("global_ip_ranges_exclude", "allocated_ips_exclude"),
               "1-sites": ("ipsec_sites_exclude",)}
    for folder, names in folders.items():
        body = [f"# Written by preflight_check.py: what {folder} leaves out on this target.",
                "# Copy it into the folder unchanged. Empty lists mean nothing is left out."]
        body += [f"{n} = {json.dumps(sorted(rep.exclude[n]))}" for n in names]
        with open(os.path.join(out_dir, f"exclude-{folder}.auto.tfvars"), "w") as fh:
            fh.write("\n".join(body) + "\n")
    return ["scope_command.sh", "exclude-0-dependencies.auto.tfvars", "exclude-1-sites.auto.tfvars"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--source-profile", required=True, help="catocli profile of the SOURCE tenant (read-only is enough)")
    ap.add_argument("--target-profile", required=True, help="catocli profile of the TARGET tenant")
    ap.add_argument("--config-dir", default=os.path.join(REPO_ROOT, "config_data"),
                    help="the source export (default: ./config_data)")
    ap.add_argument("--probe", action="store_true",
                    help="verify non-listable objects/features in a discarded draft revision on the target (needs edit rights)")
    ap.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "preflight"), help="where to write the reports")
    ap.add_argument("--customer", default=None,
                    help="customer name for the cover of the branded report (default: the target account name)")
    ap.add_argument("--pdf", action="store_true",
                    help="also print the branded report to preflight_report.pdf (needs a local Chrome, Chromium or Edge)")
    ap.add_argument("--lan-l7-confirmed", action="store_true",
                    help="LAN Layer 7 enforcement is switched on in the target for every site the check names "
                         "(the API cannot read it): stop leaving the LAN firewall rules that need it out")
    ap.add_argument("--strict", action="store_true", help="exit 1 when ACTION items remain (CI use)")
    ap.add_argument("--quiet", action="store_true", help="print only the summary")
    args = ap.parse_args()

    log = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    src_api, tgt_api = Api(args.source_profile), Api(args.target_profile)
    if args.probe:
        protected_accounts, protected_profiles = load_protected()
        if (tgt_api.account in protected_accounts or args.target_profile in protected_profiles
                or tgt_api.account == src_api.account):
            print(f"REFUSED: --probe against account {tgt_api.account}, which is a protected source "
                  "(or the source itself).", file=sys.stderr)
            return 2

    log(f"reading source export from {args.config_dir}")
    exp = load_export(args.config_dir)
    log(f"reading source tenant {src_api.account} ({args.source_profile})")
    src = load_tenant(src_api)  # read-only; tells us which policies the source key can't read
    log(f"reading target tenant {tgt_api.account} ({args.target_profile})")
    tgt = load_tenant(tgt_api)
    refs = collect_refs(exp["rules"])

    probes = None
    if args.probe:
        log("probing the target inside discarded draft revisions")
        probes = run_probes(tgt_api, refs, exp, log)

    rep = Report()
    check_access(rep, src, tgt)
    check_permissions(rep, exp, src, tgt)
    check_export(rep, exp, src)
    check_licensing(rep, exp, src, tgt)
    check_pops(rep, exp, src, tgt, refs)
    check_identity(rep, exp, src, tgt, refs, tgt_api)
    check_cma_only(rep, refs, probes)
    check_features(rep, exp, refs, probes, tgt, lan_l7_confirmed=args.lan_l7_confirmed)
    check_integrity(rep, exp, src, refs)
    check_target_state(rep, exp, tgt)
    if probes and probes.get("error"):
        rep.add("access", "WARN", "probe_failed", {}, "Probes could not run", detail=probes["error"][:300],
                fix="--probe needs a target key with edit rights on the policies.")
    if probes and probes.get("warning"):
        rep.add("access", "WARN", "probe_baseline", {}, "A probe baseline failed, so probe results may be unreliable", detail=probes["warning"][:300])

    meta = {"source": f"{src['account'].get('name')} ({src_api.account})",
            "target_name": tgt["account"].get("name"),
            "target": f"{tgt['account'].get('name')} ({tgt_api.account})", "probe": args.probe,
            "when": time.strftime("%Y-%m-%d %H:%M %Z"), "revisions": (probes or {}).get("revisions", []),
            "run": run_info(args, src_api, tgt_api)}
    text = render_text(rep, exp, meta)
    if args.quiet:
        print("\n".join(text.splitlines()[-2:]))
    else:
        print(text)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "preflight_report.md"), "w") as fh:
        fh.write(render_markdown(rep, exp, meta))
    with open(os.path.join(args.out_dir, "preflight_report.json"), "w") as fh:
        json.dump({"meta": meta, "counts": rep.counts(), "rules_total": len(exp["rules"]),
                   "rules_blocked": blocked_rule_count(rep),
                   "rules_blocked_or_at_risk": blocked_rule_count(rep, ("BLOCKER", "ACTION")), "findings": rep.findings}, fh, indent=1, default=list)
    html_path = os.path.join(args.out_dir, "preflight_report.html")
    with open(html_path, "w") as fh:
        fh.write(render_customer_html(rep, exp, meta, customer=args.customer))
    written = ["preflight_report.md", "preflight_report.json", "preflight_report.html"]
    written += write_leave_out(rep, args.config_dir, args.out_dir)
    written += [x for x in [write_migrate_sh(build_runbook(rep, meta), REPO_ROOT, args.out_dir, meta)] if x]
    if args.pdf:
        err = html_to_pdf(html_path, os.path.join(args.out_dir, "preflight_report.pdf"))
        if err:
            log(f"PDF not written: {err}")
        else:
            written.append("preflight_report.pdf")
    log(f"reports written to {args.out_dir}: {', '.join(written)}")
    c = rep.counts()
    if c["BLOCKER"]:
        return 2
    if args.strict and c["ACTION"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
