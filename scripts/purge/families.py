"""Declarative registry of every purgeable type.

One PolicyFamily per rules-and-sections policy (18) plus one DepType per
dependency object type (4). The catocli payload shapes are uniform across
families (verified against catocli models/ and parser READMEs):

  removeRule:    {"<family>PolicyMutationInput": {},
                  "<family>RemoveRuleInput": {"id": "<rule-id>"}}
  removeSection: {"<family>PolicyMutationInput": {},
                  "policyRemoveSectionInput": {"id": "<section-id>"}}
  publish:       {"<family>PolicyMutationInput": {},
                  "policyPublishRevisionInput": {...}}

The lone irregular family is socketLan (LAN firewall): its export nests
firewall rules under network rules (`network_rules[].firewall_rules[]`),
and firewall-rule removal uses the nested subcommand
`mutation policy socketLan firewall removeRule` with
`socketLanFirewallRemoveRuleInput`. engine.py special-cases it via
`nested_firewall=True`. socketLan ALSO carries sub-policies
(`policy.sub_policies[]`), whose scope rules can reference dependency objects
(e.g. global IP ranges); these are enumerated for nested_firewall families and
removed via `mutation policy socketLan removeSubPolicy` with
`socketLanRemoveSubPolicyInput` -- without this the sub-policy (and anything its
scope references) is never purged.

Export JSON shape (all policy families): rules under
`data.policy.<family>.policy.rules[]` as `{"audit", "rule": {...},
"properties"}` wrappers (socketLan: `network_rules`), sections flat under
`...policy.sections[]` as `{"section_index","section_name","section_id"}`.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyFamily:
    key: str           # wrapper-script / --only name
    export_type: str   # catocli export <type>
    family: str        # GraphQL family segment (also the export JSON key)
    nested_firewall: bool = False  # socketLan only

    @property
    def mutation_path(self):
        return ["mutation", "policy", self.family]

    @property
    def wrapper_key(self):
        return f"{self.family}PolicyMutationInput"

    @property
    def remove_rule_key(self):
        return f"{self.family}RemoveRuleInput"

    @property
    def remove_sub_policy_key(self):
        # internetFirewallRemoveSubPolicyInput / wanFirewallRemoveSubPolicyInput /
        # socketLanRemoveSubPolicyInput -- the per-family removeSubPolicy input.
        return f"{self.family}RemoveSubPolicyInput"


@dataclass(frozen=True)
class DepType:
    key: str
    export_type: str
    # engine.py dispatches on key for the (non-uniform) delete payloads.
    # list_key: when the export nests its array under a top-level key instead of
    # being a flat list (sites -> {"sites":[...]}); None = flat list / {"data":[...]}.
    list_key: str = None


POLICY_FAMILIES = [
    PolicyFamily("internet_firewall", "if_rules", "internetFirewall"),
    PolicyFamily("wan_firewall", "wf_rules", "wanFirewall"),
    PolicyFamily("wan_network", "wnw_rules", "wanNetwork"),
    PolicyFamily("tls_inspection", "tls_rules", "tlsInspect"),
    PolicyFamily("lan_firewall", "lan_fw_rules", "socketLan", nested_firewall=True),
    PolicyFamily("application_control", "apc_rules", "applicationControl"),
    PolicyFamily("remote_port_forward", "rpf_rules", "remotePortFwd"),
    PolicyFamily("socket_bypass", "sb_rules", "socketBypass"),
    PolicyFamily("split_tunnel", "st_rules", "splitTunnel"),
    PolicyFamily("terminal_server", "ts_rules", "terminalServer"),
    PolicyFamily("always_on", "ao_rules", "ztnaAlwaysOn"),
    PolicyFamily("client_connectivity", "cc_rules", "clientConnectivity"),
    PolicyFamily("dynamic_ip_allocation", "dia_rules", "dynamicIpAllocation"),
    PolicyFamily("private_access", "pa_rules", "privateAccess"),
    PolicyFamily("app_tenant_restriction", "atr_rules", "appTenantRestriction"),
    PolicyFamily("anti_malware_file_hash", "amfh_rules", "antiMalwareFileHash"),
    PolicyFamily("sdp_static_ip_allocation", "ssia_rules", "sdpStaticIpAllocation"),
    PolicyFamily("site_web_proxy", "swp_rules", "siteWebProxy"),
]

# Teardown is the EXACT REVERSE of the current create order. The authoritative
# create order is bulk_apply.py FOLDER_SEQUENCE:
#   0-dependencies -> 1-sites -> 2-app-connectors -> 3-groups -> 4-policies/*
# (0-scim-users is a non-Terraform manual gate and isn't torn down here.) So the
# purge tiers are its strict reverse, folder for folder:
#   4-policies (18 families)      Tier 1  -- reference everything, go first
#   3-groups                      Tier 2  -- site-scoped members
#   2-app-connectors              Tier 3  -- app connectors + private apps
#   1-sites                       Tier 4  -- cascade-removes ranges/if/wifi/hosts
#   0-dependencies                Tier 5  -- account-level objects, created first
# Each folder's own create order is reversed within its tier below. This mirror
# MUST stay in lock-step with FOLDER_SEQUENCE: if a folder moves or a resource
# type changes folders, move its DepType to the matching tier here too.

# Tier 2: groups (3-groups). Site-scoped members -> deleted before sites, and
# 3-groups is created after 2-app-connectors -> deleted before app connectors.
GROUP_TYPES = [
    DepType("groups", "groups"),
]

# Tier 3: app connectors + private apps (2-app-connectors). Created after
# 1-sites, so in reverse they are deleted BEFORE sites (not lumped with the
# account-level deps). Intra-folder: private apps reference connector groups
# (created implicitly by connectors), so apps delete first, then connectors --
# the reverse of app_connectors-before-private_apps on create.
APP_CONNECTOR_TYPES = [
    DepType("private_apps", "private_apps"),
    DepType("app_connectors", "app_connectors", list_key="connectors"),
]

# Tier 4: sites (1-sites; cascade-removes ranges/interfaces/wifi/static hosts).
# Export nests the array under "sites".
SITE_TYPES = [
    DepType("sites", "socket_sites", list_key="sites"),
]

# Tier 5: account-level dependency objects (everything 0-dependencies/ creates,
# the first folder applied -> the last tier torn down). Intra-folder reverse:
# the notification chain subscription_groups -> {mailing_lists, webhooks}
# (integration[]) means subscription_groups (the referrers) delete first, then
# their children. allocated IPs live here (0-dependencies), NOT with the app
# connectors. Global IP ranges go LAST: Dynamic IP Allocation rules (among other
# policy references) must be removed and published before their ranges can go.
# deleteGlobalIpRangeBulk is atomic, so one surviving reference blocks all ranges.
DEP_TYPES = [
    DepType("allocated_ips", "allocated_ips"),
    DepType("subscription_groups", "subscription_groups"),
    DepType("mailing_lists", "mailing_lists"),
    # webhooks: children of subscription_groups (referenced by integration[]),
    # so they delete after subscription_groups. `catocli export webhooks` returns
    # a flat id+name list (the stock minimal query -- NOT the richer auth-selecting
    # query that errors on the undeployed CustomHeadersAuth type), which the engine
    # enumerates like any other flat dep.
    DepType("webhooks", "webhooks"),
    DepType("custom_applications", "custom_apps"),
    DepType("fqdn_containers", "fqdn_containers"),
    DepType("ip_range_containers", "ip_range_containers"),
    DepType("dhcp_relay_groups", "dhcp_relay_groups"),
    DepType("global_ip_ranges", "global_ip_ranges"),
]

# Everything-but-policies, in teardown order (exact reverse of FOLDER_SEQUENCE):
# groups (3) -> app connectors (2) -> sites (1) -> account deps (0).
NON_POLICY_TYPES = GROUP_TYPES + APP_CONNECTOR_TYPES + SITE_TYPES + DEP_TYPES

ALL_KEYS = [f.key for f in POLICY_FAMILIES] + [d.key for d in NON_POLICY_TYPES]


def lookup(key):
    for item in POLICY_FAMILIES + NON_POLICY_TYPES:
        if item.key == key:
            return item
    raise KeyError(f"unknown purge type {key!r} (known: {', '.join(ALL_KEYS)})")
