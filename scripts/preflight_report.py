"""Cato-branded customer readiness report for preflight_check.py.

Turns the check's findings into a self-contained HTML document (fonts and the
official logo embedded, prints cleanly to A4/Letter) written for the customer:
plain language, grouped by who acts, with why each item matters and what to do.
The technical fixes for the migration engineers go in an appendix.

Optional PDF output uses a local Chrome/Chromium/Edge in headless mode.
"""
import base64
import hashlib
import html
import os
import re
import shutil
import subprocess
import time

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_assets")
LOGO_SHA256 = {
    "cato_logo_rgb_color_art_101.svg": "63d48dabae6e7ba66f5d6db207c853974b8e4716c60c5f17d68253c5322578b5",
    "cato_logo_rgb_white_art_101.svg": "bb0ba25208097457fcec91ac9672458b547443505d7a0b5f60d5dd47010134eb",
}

# Customer-facing labels (calm, plain; Coral and Sunflower carry meaning only).
LABELS = {
    "BLOCKER": ("Required before migration", "req"),
    "ACTION": ("To do or confirm", "todo"),
    "WARN": ("Review", "review"),
    "OK": ("Ready", "ok"),
    "INFO": ("Note", "note"),
}

# Who acts. Outline icons: 24 px grid, 2 px round stroke.
_I = 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
OWNERS = [
    ("cato_team", "Your Cato account team", "Licenses, PoP entitlements and add-on services",
     f'<svg viewBox="0 0 24 24" {_I}><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M3 13h18"/></svg>'),
    ("cato_support", "Cato Support", "Features that need to be enabled on the account",
     f'<svg viewBox="0 0 24 24" {_I}><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M5.6 5.6l3.6 3.6M14.8 14.8l3.6 3.6M18.4 5.6l-3.6 3.6M9.2 14.8l-3.6 3.6"/></svg>'),
    ("identity", "Your identity team", "Users and groups from your identity provider",
     f'<svg viewBox="0 0 24 24" {_I}><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 20a6.5 6.5 0 0 0-3-5.5"/></svg>'),
    ("cma_admin", "Your Cato administrator", "Settings and objects in the Cato Management Application (CMA)",
     f'<svg viewBox="0 0 24 24" {_I}><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg>'),
    ("migration", "Your migration team", "Preparation the migration engineers handle with you",
     f'<svg viewBox="0 0 24 24" {_I}><path d="M4 7h13l-3-3M20 17H7l3 3"/></svg>'),
]
ICON_CHECK = f'<svg viewBox="0 0 24 24" {_I}><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'

CMA_WHY = {
    "customCategory": "Custom categories group applications and domains for your policies. They can only be created in the CMA.",
    "device": "Device posture profiles define the device checks your access policies require. They can only be created in the CMA.",
    "bandwidthPriority": "Bandwidth priorities shape traffic in your WAN Network rules. Priorities beyond the built-in ones are created in the CMA.",
    "userNotification": "User notification templates are the messages people see when a rule blocks or prompts them.",
    "contentProfile": "Data Loss Prevention (DLP) content profiles define the data your Cato DLP rules look for.",
    "edmProfile": "Exact Data Match (EDM) profiles hold the data fingerprints your Cato DLP rules match against.",
    "rbiProfile": "Remote Browser Isolation (RBI) profiles control how isolated browsing behaves.",
}
SAME_NAMES = "In the CMA, create each item listed here with exactly the same name and settings as in your current account."
CHECK_NAMES = ("In the CMA, check that each item listed here exists in the new account with exactly the same name, "
               "and create any that don't, with the same settings as in your current account.")


def _g(owner, title, why, steps=(), items=None):
    g = {"owner": owner, "title": title, "why": why, "steps": list(steps)}
    if items is not None:
        g["items"] = list(items)  # customer view of the list, when the engineering one says too much
    return g


def _pl(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _who(c, n):
    """'2 user groups', '1 user', '1 user group and 7 users' from an id_missing finding's counts."""
    ng, nu = c.get("groups"), c.get("users")
    if ng is None and nu is None:
        return _pl(n, "user or group", "users and groups")
    return " and ".join(x for x in (_pl(ng, "user group") if ng else "", _pl(nu, "user") if nu else "") if x)


def guide(f):
    """Customer wording for a finding: owner, title, why it matters, what to do."""
    k, c, n = f["key"], f["ctx"], f["ctx"].get("n")
    rules = len(f["rules"])
    affects = (f" {rules} of your policy rules {'depends' if rules == 1 else 'depend'} on this." if rules else "")
    if k == "access_trial":
        return _g("cato_team", "Confirm license quantities for the new account",
                  "The new account is a trial, and trial accounts don't report license details. The quantities in this "
                  "section are what your current configuration uses.",
                  ["Share the license items in this section with your Cato account team and confirm each one is in place "
                   "before the migration date."])
    if k in ("perm_source", "perm_target"):
        perms = c.get("permissions") or []
        names = ", ".join(FAMILY_NAMES.get(x, x) for x in c.get("families") or [])
        if k == "perm_source":
            return _g("cma_admin", f"Let the current account's API key read {_pl(n, 'more policy', 'more policies')}",
                      f"The key can't read {names}, so the export carried on without "
                      + ("it" if n == 1 else "them") + " and " + ("its" if n == 1 else "their")
                      + " rules can't be migrated yet.",
                      ["Create the key as an admin whose role can view the whole account (the predefined Viewer or "
                       "Editor role), or add " + ("this permission" if len(perms) == 1 else "these permissions")
                       + " to its role: " + ", ".join(perms) + ".",
                       "Then export the current account again."])
        return _g("cma_admin", f"Let the new account's API key manage {_pl(n, 'more policy', 'more policies')}",
                  f"The key can't read {names}, so it can't migrate " + ("it" if n == 1 else "them") + " either.",
                  ["Create the key as an admin whose role can edit the whole account (the predefined Editor role), or "
                   "add " + ("this permission" if len(perms) == 1 else "these permissions") + " to its role: "
                   + ", ".join(perms) + "."])
    if k == "export_refused":
        perms = c.get("permissions") or []
        types = ", ".join(c.get("types") or [])
        return _g("cma_admin", f"Let the current account's API key export {_pl(n, 'more item type')}",
                  f"The export was refused {types} for lack of permission, so "
                  + ("it is" if n == 1 else "they are") + " missing from the migration.",
                  (["Add " + ("this permission" if len(perms) == 1 else "these permissions") + " to the key's admin "
                    "role: " + ", ".join(perms) + ". Or create the key as an admin whose role can view the whole "
                    "account (the predefined Viewer or Editor role)."] if perms else
                   ["Create the key as an admin whose role can view the whole account (the predefined Viewer or "
                    "Editor role)."]) + ["Then export the current account again."])
    if k == "export_failed":
        return _g("migration", f"Export {_pl(n, 'item type')} again",
                  "Part of the export failed for a reason other than permissions, so the migration would miss it.",
                  ["Fix the cause shown in the technical notes, then export the current account again."])
    if k == "export_policy_missing":
        return _g("migration", f"Export {_pl(n, 'missing policy', 'missing policies')} again",
                  "These policies have rules in your current account but aren't in the export, so the migration would "
                  "leave them out.",
                  ["Export the current account again and check every type succeeded."])
    if k == "access_same_account":
        return _g("migration", "Choose a different target account", "The source and target are the same account.")
    if k in ("export_incomplete", "export_hosts_short", "export_no_rules"):
        why = {"export_incomplete": "Some configuration files are missing from the export, so parts of your configuration would not be copied.",
               "export_hosts_short": f"Only {c.get('have')} of your {c.get('need')} static hosts were exported.",
               "export_no_rules": "The export has no policy rules."}[k]
        return _g("migration", "Complete the configuration export", why,
                  ["Rerun the export, confirm it finishes for every object type, then run this check again."])
    if k in ("lic_ips_unknown", "lic_ips_short"):
        extra = f" The new account is licensed for {c.get('total')}." if k == "lic_ips_short" else ""
        return _g("cato_team", f"License {c['need']} public IP addresses",
                  f"Your current account uses {c['need']} dedicated public IP addresses at Cato PoPs (Points of Presence). "
                  f"Each one needs a license in the new account, or the addresses and the rules that use them can't be "
                  f"created.{extra}",
                  [f"Ask your Cato account team to license at least {c['need']} public IP addresses for the new account."])
    if k in ("lic_ztna_unknown", "lic_ztna_short"):
        extra = f" The new account is licensed for {c.get('total')}." if k == "lic_ztna_short" else ""
        return _g("cato_team", f"License {c['need']} remote users",
                  f"{c['need']} people connect remotely with the Cato Client today. Zero Trust Network Access (ZTNA) "
                  f"user licenses let them connect to the new account.{extra}",
                  [f"Ask your Cato account team to license at least {c['need']} ZTNA users."])
    if k in ("lic_sites_unknown", "lic_sites_short", "lic_sites_none"):
        ded, pool, bw = c.get("ded", c.get("need", 0)), c.get("pool", 0), c.get("pool_bw", 0)
        uses = " and ".join(x for x in (
            f"{_pl(ded, 'site')} {'has its' if ded == 1 else 'have their'} own site license" if ded else "",
            f"{_pl(pool, 'site')} {'shares' if pool == 1 else 'share'} {bw} Mbps of pooled bandwidth" if pool else "") if x)
        have = (f" The new account has {_pl(c.get('free_ded', 0), 'free site license')} and {c.get('free_pool', 0)} Mbps "
                "of free pooled bandwidth." if k == "lic_sites_short" else "")
        return _g("cato_team", f"License {_pl(c['need'], 'site')}",
                  f"In your current account, {uses}. The new account needs the same, or those sites can't be "
                  f"created.{have}",
                  [f"Ask your Cato account team for {_pl(ded, 'site license')}"
                   + (f" and {bw} Mbps of pooled bandwidth" if pool else "") + " in the new account."])
    if k in ("lic_skus_unknown", "lic_skus_missing"):
        return _g("cato_team", f"Match {_pl(n, 'add-on service')}",
                  ("This service is" if n == 1 else "These services are") + " active in your current account. Policies "
                  "that rely on " + ("it" if n == 1 else "them") + " behave differently, or can't be created, without "
                  + ("it." if n == 1 else "them."),
                  ["Review the list with your Cato account team and add the services you want to keep using."])
    if k == "pop_missing":
        return _g("cato_team", f"Make {_pl(n, 'PoP location')} available, or leave {'its' if n == 1 else 'their'} items out",
                  f"Some dedicated IP addresses and IPSec tunnels use Cato PoPs ({', '.join(c.get('names', []))}) that "
                  "need a separate entitlement, such as PoPs in China." + affects,
                  ["Ask your Cato account team to add the entitlement for these PoPs.",
                   "Or agree with your migration team to leave these items out of the migration for now."])
    if k == "id_missing":
        t = c.get("type")
        if t == "MANUAL":
            ng = c.get("groups", n if "users" not in c else 0)
            nu = c.get("users", 0)
            what = " and ".join(x for x in (f"{ng} user group{'s' if ng != 1 else ''}" if ng else "",
                                            f"{nu} user{'s' if nu != 1 else ''}" if nu else "") if x)
            why = ("These were created by hand in your current account. The Cato API can't create user groups or "
                   "set group membership, so the groups need to be created in the Cato Management Application (CMA) "
                   "with exactly the same names and members." if ng else
                   "These users were created by hand in your current account, and your policy rules name them.")
            if nu:
                why += (" Users can be created in the CMA, or by your migration team through the Cato API."
                        if ng else " They can be created in the CMA, or by your migration team through the Cato API.")
            steps = []
            if nu:
                steps.append("Create each user listed here with the same name and email address, in the CMA or with "
                             "your migration team.")
            if ng:
                steps += ["In the CMA, create each group listed here with exactly the same name.",
                          "Add the same members to each group as in your current account."]
            return _g("cma_admin", f"Create {what}" + (" in the CMA" if ng and not nu else ""), why + affects, steps)
        if t == "SCIM":
            return _g("identity", f"Provision {_who(c, n)} through SCIM",
                      "Your policy rules refer to these users and groups by name. Your current account receives them from "
                      "your identity provider through SCIM (System for Cross-domain Identity Management), so the new "
                      "account needs the same." + affects,
                      ["Create the SCIM integration in the new account, and point your identity provider (for example "
                       "Microsoft Entra ID or Okta) at it.",
                       "Assign the groups and users listed here, and run a sync.",
                       "Confirm they appear in the CMA before the migration starts."])
        if t == "LDAP":
            return _g("identity", f"Sync {_who(c, n)} from your directory",
                      "Your current account imports these from your directory (LDAP or Active Directory)." + affects,
                      ["Connect the new account to the same directory.",
                       "Sync the users and groups listed here, and confirm they appear in the CMA."])
        if t == "DYNAMIC":
            return _g("cma_admin", f"Turn on {_pl(n, 'dynamic user group')}",
                      ("This group is" if n == 1 else "These groups are") + " built automatically from user attributes "
                      "such as department." + affects,
                      ["Turn on dynamic user groups for the department attribute, in the CMA or with your migration "
                       "team through the Cato API.",
                       "Confirm the department attribute is filled in for the users these groups should hold."])
        if t == "INTEGRATION":
            return _g("identity", f"Reconnect the integration that provides {_who(c, n)}",
                      "An identity integration in your current account provides these." + affects,
                      ["Set up the same integration in the new account and confirm these appear in the CMA."])
        return _g("migration", f"Review {_who(c, n)} that no longer {'exists' if n == 1 else 'exist'}",
                  "Your policy rules name " + ("this" if n == 1 else "these") + ", but "
                  + ("it isn't" if n == 1 else "they aren't") + " in your current account any more." + affects,
                  ["Decide with your migration team whether to tidy these rules before the migration."])
    if k == "id_scim_inactive":
        return _g("identity", "Connect your identity provider to the new account",
                  "Your current account receives users from an identity provider through SCIM. The new account has no "
                  "provisioned users yet.",
                  ["Create the SCIM integration in the new account.",
                   "Point your identity provider (for example Microsoft Entra ID or Okta) at it.",
                   "Assign the user groups your policies use, and run a sync."])
    if k == "id_ldap_inactive":
        return _g("identity", "Connect your directory to the new account",
                  "Your current account imports users from LDAP or Active Directory. The new account has none yet.",
                  ["Connect the new account to the directory and run a sync."])
    if k == "id_scim_endpoint_fail":
        return _g("identity", "Check the SCIM connection details",
                  "The SCIM address or token for the new account didn't work.",
                  ["Confirm the SCIM URL and token in the new account's SCIM integration and share them with your "
                   "migration team."])
    if k in ("cma_missing", "cma_unverified"):
        what = f"{n} {c['one'] if n == 1 else c['many']}"
        title = (f"Create {what} in the CMA" if k == "cma_missing"
                 else f"Confirm {what} {'exists' if n == 1 else 'exist'} in the CMA")
        steps = [SAME_NAMES] if k == "cma_missing" else [CHECK_NAMES]
        return _g("cma_admin", title, CMA_WHY.get(c.get("kind"), "") + affects, steps)
    if k == "custom_app_unbuildable":
        it = "it" if n == 1 else "them"
        return _g("cma_admin", f"Create {_pl(n, 'custom application')} by hand in the CMA",
                  "Terraform can't create " + ("this custom application" if n == 1 else "these custom applications")
                  + " as " + ("it's" if n == 1 else "they're") + " defined in your current account. Each needs at "
                  "least one criterion, and none that is only \"any protocol\" with no port or destination. The CMA "
                  "accepts both, so the migration copies the rest and leaves " + it + " for you." + affects,
                  ["In the new account's CMA, create each one listed here with the same name and criteria. Or, in "
                   "the current account, add a criterion or delete the \"any protocol\" row, then export again.",
                   "Until then, the migration leaves " + it + " out, along with the rules that use " + it + "."])
    if k == "cma_undecided":
        what = f"{n} {c['one'] if n == 1 else c['many']}"
        return _g("cma_admin", f"Check {what} in the CMA",
                  CMA_WHY.get(c.get("kind"), "") + " The check couldn't tell whether "
                  + ("it exists" if n == 1 else "they exist") + " in the new account." + affects,
                  [CHECK_NAMES], items=c.get("names") or [x.split(": ", 1)[0] for x in f["items"]])
    if k == "feat_sanctioned":
        return _g("cma_admin", "Match your sanctioned applications",
                  f"{_pl(n, 'policy rule')} {'matches' if n == 1 else 'match'} on your Sanctioned Apps list. The list "
                  "itself is an account setting that isn't copied, so without it these rules would match different "
                  "traffic in the new account.",
                  ["In the CMA, mark the same applications as sanctioned in the new account as in your current account, "
                   "before the policies are migrated."])
    if k == "int_unchecked":
        return _g("migration", f"Confirm {_pl(n, 'object')} your rules name",
                  "Some rules name network interfaces, floating subnets or value sets. The check can't look these up, "
                  "and the migration doesn't create them." + affects,
                  ["Check each one listed here exists in the new account with exactly the same name before the "
                   "policies are migrated."])
    if k in ("feat_rbi_disabled", "feat_rbi_unverified"):
        return _g("cato_team", "Enable Cato RBI (Remote Browser Isolation)",
                  (f"{n} of your policy rules isolates" if n == 1 else f"{n} of your policy rules isolate")
                  + " risky browsing with Cato RBI"
                  + (", which isn't enabled in the new account." if k == "feat_rbi_disabled" else "."),
                  ["Ask your Cato account team to enable Cato RBI for the new account."])
    if k in ("feat_swp_fail", "feat_swp_unverified"):
        return _g("cato_support", "Enable Site Web Proxy for the new account",
                  f"{n} Site Web Proxy rule{'s' if n != 1 else ''} can't be created because the new account doesn't "
                  "accept Site Web Proxy rules yet.",
                  ["Open a case with Cato Support to enable Site Web Proxy for the new account, and mention the error "
                   "\u201cUnsupported field [addRule]\u201d."])
    if k == "feat_srt":
        return _g("cma_admin", "Turn on Static Range Translation",
                  (f"{n} of your network ranges translates addresses" if n == 1 else
                   f"{n} of your network ranges translate addresses")
                  + ". The new account accepts these translations only once Static Range Translation is turned on, "
                  "and that account setting can only be changed in the CMA.",
                  ["In the CMA, turn on Static Range Translation for the new account before the sites are created."])
    if k == "feat_l7":
        sites = ", ".join(c.get("sites") or [])
        return _g("cma_admin", f"Turn on LAN Layer 7 enforcement for {sites}",
                  (f"{n} LAN firewall rule matches" if n == 1 else f"{n} LAN firewall rules match")
                  + " on applications or services, which needs Layer 7 "
                  "enforcement on the site. It's a per-site setting that can only be changed in the CMA.",
                  ["After the sites exist in the new account, turn on LAN Layer 7 enforcement for the sites listed."])
    if k == "feat_waf_range":
        return _g("cma_admin", f"Create {_pl(n, 'IP range')} in the CMA",
                  ("This range includes" if n == 1 else "These ranges include") + " 169.254.169.254, an address the "
                  "Cato API doesn't accept for security reasons, so " + ("it needs" if n == 1 else "they need")
                  + " to be created by hand." + affects,
                  ["Wait until your migration team tells you the shared objects (the first part of the migration) are in "
                   "place. Creating it before then makes that step fail.",
                   "Then, in the CMA, create the range listed here with the same name and value."])
    if k == "feat_waf_range_early":
        return _g("migration", f"Keep {n} hand-made IP range{'s' if n != 1 else ''} out of the first deploy",
                  "This range was created by hand before the migration's first step. It includes 169.254.169.254, which the "
                  "Cato API doesn't accept, so the first step has to leave it out and then take it over straight after.",
                  ["Deploy the shared objects with the range left out.",
                   "Run the check again straight away, and use its new exclusion file before the shared objects are "
                   "deployed again."])
    if k == "feat_wifi":
        return _g("migration", f"Provide passwords for {n} Wi-Fi network{'s' if n != 1 else ''}",
                  "Wi-Fi passwords aren't included when configuration is exported.",
                  ["Share the passwords with your migration team securely, or plan to set them after the migration."])
    if k == "feat_webhooks":
        return _g("migration", f"Decide where {n} webhook{'s' if n != 1 else ''} should send events",
                  "Webhooks copy across pointing at the same destinations as today, such as your ticketing or chat tools.",
                  ["Confirm each destination with your migration team, and have the credentials ready."])
    if k == "feat_ipsec":
        return _g("migration", f"Prepare IPSec tunnel details for {n} site{'s' if n != 1 else ''}",
                  "Tunnel settings and pre-shared keys aren't included in the export.",
                  ["Gather the tunnel details and keys for the sites listed, ready for after the sites are created."])
    if k == "int_classic":
        return _g("migration", f"Recreate {n} legacy group{'s' if n != 1 else ''}",
                  "These groups use an older format that the Cato API can't read, so their members need to be recreated "
                  "as groups in the new account." + affects,
                  ["Your migration team will copy each group's members from your current account."])
    if k == "int_stale":
        return _g("migration", f"Review {n} reference{'s' if n != 1 else ''} to deleted objects",
                  "Some rules still point at sites or ranges that no longer exist in your current account." + affects,
                  ["Decide with your migration team whether to tidy these rules before the migration."])
    if k == "tgt_clash":
        return _g("migration", f"Clear {n} sample rule{'s' if n != 1 else ''} from the new account",
                  "New accounts include sample rules. Some share names with your rules, which would create duplicates.",
                  ["Your migration team will review the sample rules and remove them before the migration."])
    if k == "tgt_existing_named":
        return _g("migration", f"Review {_pl(n, (c.get('label') or 'items').rstrip('s'))} that already "
                               f"{'exists' if n == 1 else 'exist'} in the new account",
                  "A fresh migration expects " + ("this" if n == 1 else "these") + " to be absent.",
                  ["Decide with your migration team whether to adopt or remove them."])
    if k in ("probe_failed", "probe_baseline", "tgt_unreadable", "tgt_rules_existing"):
        return _g("migration", f["title"], f["detail"] or "", [])
    return _g("migration", f["title"], f["detail"] or "", [])


# OK findings, phrased for the "already in place" list.
def ready_text(f):
    k, c = f["key"], f["ctx"]
    return {
        "export_ok": (f"Apart from what the API key wasn't allowed to read, your configuration export is complete "
                      f"({c.get('rules')} policy rules across {c.get('policies')} policies)." if c.get("partial") else
                      f"Your configuration export is complete ({c.get('rules')} policy rules across {c.get('policies')} policies)."),
        "export_hosts_ok": f"All {c.get('n')} static hosts were exported.",
        "lic_ips_ok": f"The new account has enough public IP address licenses ({c.get('need')} needed).",
        "lic_ztna_ok": f"The new account has enough ZTNA user licenses ({c.get('need')} needed).",
        "pop_summary": f"{c.get('ok')} of {c.get('n')} Cato PoPs your configuration uses are available to the new account.",
        "id_ok": f"All {c.get('n')} users and user groups your rules name exist in the new account.",
        "id_scim_endpoint_ok": "The new account's SCIM integration answers.",
        "cma_present": f"{c.get('n')} {c.get('one') if c.get('n') == 1 else c.get('many')} your rules use already "
                       f"{'exists' if c.get('n') == 1 else 'exist'} in the new account.",
        "lic_sites_ok": f"The new account has enough site licenses and pooled bandwidth for your {_pl(c.get('need', 0), 'licensed site')}.",
        "feat_rbi_ok": "Cato RBI is enabled in the new account.",
        "perm_ok": "Both API keys can read every policy.",
        "feat_swp_ok": "Site Web Proxy rules can be created in the new account.",
        "feat_srt_ok": "Your network ranges don't need Static Range Translation.",
        "feat_waf_range_done": "The IP ranges that had to be created by hand are in place.",
        "feat_l7_confirmed": f"LAN Layer 7 enforcement is on for {', '.join(c.get('sites') or [])}, as confirmed by your team.",
        "int_classic_done": f"{c.get('n')} legacy groups have already been recreated for the migration.",
        "int_ok": "Every site, range, application and IP address your rules name is in the export.",
        "custom_app_by_hand_present": f"{_pl(c.get('n', 0), 'custom application')} Terraform can't create "
                                      f"{'has' if c.get('n') == 1 else 'have'} already been created in the new account.",
    }.get(k, f["title"])


# --------------------------------------------------------------------------
def _asset(name, binary=True):
    path = os.path.join(ASSETS, name)
    with open(path, "rb") as fh:
        data = fh.read()
    return data if binary else data.decode()


def _logo(name):
    try:
        data = _asset(name)
    except OSError:
        return ""
    if hashlib.sha256(data).hexdigest() != LOGO_SHA256.get(name):
        return ""  # never show modified artwork
    # As an image, not inline SVG: the kit's files share class names (.st0 ...), and two
    # inline copies would restyle each other.
    return f'<img class="cato-logo" alt="Cato Networks" src="data:image/svg+xml;base64,{base64.b64encode(data).decode()}">'


def _font_face():
    out = []
    for fam, weight, fn, fmt in [("Archivo", "100 900", "archivo-latin-wght-normal.woff2", "woff2"),
                                 ("Inter", "400", "Inter-Regular.woff", "woff"), ("Inter", "700", "Inter-Bold.woff", "woff")]:
        try:
            b64 = base64.b64encode(_asset(fn)).decode()
        except OSError:
            continue
        out.append(f"@font-face{{font-family:'{fam}';font-weight:{weight};font-style:normal;font-display:swap;"
                   f"src:url(data:font/{fmt};base64,{b64}) format('{fmt}');}}")
    return "\n".join(out)


def _ring(pct):
    """Flat 2D open ring as a readiness gauge (Cato Green on Pale Grey)."""
    r, w = 70, 18
    circ = 2 * 3.14159265 * r
    dash = max(0.0, min(1.0, pct)) * circ
    return (f'<svg viewBox="0 0 180 180" class="ring" role="img" aria-label="{round(pct * 100)} percent ready">'
            f'<circle cx="90" cy="90" r="{r}" fill="none" stroke="#F0F0F0" stroke-width="{w}"/>'
            f'<circle cx="90" cy="90" r="{r}" fill="none" stroke="#128461" stroke-width="{w}" '
            f'stroke-dasharray="{dash:.1f} {circ:.1f}" transform="rotate(-90 90 90)"/>'
            f'<text x="90" y="88" text-anchor="middle" class="ring-num">{round(pct * 100)}%</text>'
            f'<text x="90" y="112" text-anchor="middle" class="ring-sub">ready now</text></svg>')


# Print sizes for code: Chrome-printed PDFs split "_" onto its own line when copied at some sizes
# (9.6px and 8pt do; 9 to 13px in half-pixel steps do not). Check with a PDF text extract after changing them.
CSS = """
:root{--green:#128461;--moss:#006F53;--forest:#003E34;--pine:#002C28;--hl:#00FFCC;--coral:#FF3749;--sun:#FFC820;
--pale:#F0F0F0;--light:#C8C8C8;--grey:#969696;--mid:#646464;--dark:#464646;--black:#000}
*{box-sizing:border-box}
html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{margin:0;background:#fff;color:#1b1b1b;font:15px/1.6 Inter,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.page{max-width:920px;margin:0 auto;padding:0 40px 48px}
h1,h2{font-family:Archivo,Inter,sans-serif;font-weight:500;letter-spacing:-.01em;margin:0}
h2{font-size:26px;color:var(--pine);margin:44px 0 6px}
h3{font-size:16px;font-weight:700;margin:0 0 4px}
p{margin:0 0 10px}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--moss)}
.hero{background:linear-gradient(125deg,#128461,#00392F);color:#fff;padding:34px 40px 40px;border-radius:0 0 20px 20px;
max-width:1000px;margin:0 auto}
.hero .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:46px}
.hero .logo img{height:34px;width:auto;display:block}
.hero .eyebrow{color:var(--hl)}
.hero h1{font-size:40px;line-height:1.15;margin:10px 0 14px;max-width:720px}
.hero .meta{display:flex;flex-wrap:wrap;gap:6px 28px;font-size:14px;color:#dff5ec}
.hero .meta b{color:#fff;font-weight:700}
.pill{display:inline-block;padding:6px 14px;border-radius:999px;font-weight:700;font-size:13px;background:#fff;color:var(--forest)}
.summary{display:grid;grid-template-columns:200px 1fr;gap:32px;align-items:center;margin:36px 0 8px}
.ring{width:190px;height:190px}.ring-num{font:500 40px Archivo,Inter,sans-serif;fill:var(--pine)}
.ring-sub{font:600 12px Inter,sans-serif;fill:var(--mid);letter-spacing:.08em;text-transform:uppercase}
.lead{font-size:18px;line-height:1.55;color:#222;margin-bottom:16px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.stat{border:1px solid #e3e3e3;border-left:5px solid var(--grey);border-radius:10px;padding:10px 12px}
.stat .num{font:500 28px Archivo,Inter,sans-serif;color:var(--pine);line-height:1.1}
.stat .lbl{font-size:12.5px;color:var(--dark)}
.stat.req{border-left-color:var(--coral)}.stat.todo{border-left-color:var(--sun)}.stat.ok{border-left-color:var(--green)}
.owner{margin-top:30px}
.owner-head{display:flex;gap:14px;align-items:center;padding-bottom:10px;border-bottom:2px solid var(--green);margin-bottom:14px;break-after:avoid}
.owner-head .ico{width:42px;height:42px;border-radius:50%;background:var(--green);color:#fff;display:flex;align-items:center;justify-content:center;flex:none}
.owner-head .ico svg{width:22px;height:22px}
.owner-head .count{margin-left:auto;font-size:13px;color:var(--mid);white-space:nowrap}
.owner-head h3{font-family:Archivo,Inter,sans-serif;font-weight:500;font-size:20px;color:var(--pine);margin:0}
.owner-head .sub{font-size:13px;color:var(--mid)}
.item{display:grid;grid-template-columns:22px 1fr;gap:12px;padding:14px 16px;border:1px solid #e6e6e6;border-radius:12px;margin-bottom:10px;break-inside:avoid}
.box{width:18px;height:18px;border:2px solid var(--dark);border-radius:4px;margin-top:3px}
.no{display:inline-block;min-width:22px;height:22px;line-height:22px;text-align:center;border-radius:11px;background:var(--forest);color:#fff;font-size:12px;font-weight:700;margin-right:8px;padding:0 6px}
.rules-by-policy{columns:2;column-gap:28px;font-size:12.5px;margin-top:10px}
.rules-by-policy .pol{break-inside:avoid;margin-bottom:10px}
.rules-by-policy .pol b{display:block;color:var(--pine);font-size:13px;margin-bottom:2px}
.rules-by-policy .r{display:flex;justify-content:space-between;gap:10px;padding:1px 0;border-bottom:1px dotted #ddd}
.rules-by-policy .r span:last-child{color:var(--mid);white-space:nowrap}
.chip{display:inline-block;font-size:11.5px;font-weight:700;letter-spacing:.04em;padding:2px 9px;border-radius:999px;margin-bottom:6px}
.chip.req{background:#ffe3e6;color:#8a0f1a}.chip.todo{background:#fff2c6;color:#5c4500}.chip.review{background:var(--pale);color:var(--dark)}
.item .why{color:#333}
.item ol{margin:6px 0 4px 18px;padding:0}.item li{margin:2px 0}
.what{font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--moss);margin-top:8px}
.names{margin:8px 0 0;padding:8px 12px;background:var(--pale);border-radius:8px;font-size:13px;color:#2a2a2a;columns:2;column-gap:26px}
.names div{break-inside:avoid;padding:1px 0;overflow-wrap:anywhere}
.more{font-size:12.5px;color:var(--mid);margin-top:4px}
.affects{font-size:12.5px;color:var(--mid);margin-top:6px}
.ready{display:grid;grid-template-columns:1fr 1fr;gap:8px 24px;margin-top:12px}
.ready div{display:flex;gap:10px;align-items:flex-start;font-size:14px}
.ready svg{width:20px;height:20px;color:var(--green);flex:none;margin-top:2px}
.note{background:var(--pale);border-radius:12px;padding:16px 20px;margin-top:12px}
.note li{margin:4px 0}
.appendix{break-before:page}
.appendix h2{margin-top:28px}
.app-item{border-top:1px solid #e3e3e3;padding:10px 0;break-inside:avoid}
.app-item .t{font-weight:700}
.app-rules{font-size:12.5px;columns:2;column-gap:26px;color:#333;margin-top:4px}
.app-rules div{overflow-wrap:anywhere;break-inside:avoid}
.code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;color:#333;overflow-wrap:anywhere}
html{scroll-behavior:smooth}
[id]{scroll-margin-top:18px}
.page a{color:var(--moss)}
.toc{margin:26px 0 6px;padding:16px 22px 14px;border:1px solid #e3e3e3;border-radius:12px;break-inside:avoid}
.toc>ol{columns:2;column-gap:34px;margin:8px 0 0;padding-left:20px}
.toc li{break-inside:avoid;margin:3px 0}
.toc ol ol{margin:2px 0 8px;padding-left:14px;list-style:none;font-size:13px}
.toc a{color:var(--forest);text-decoration:none;font-weight:600}.toc ol ol a{font-weight:400}
.toc a:hover{text-decoration:underline}
.tc{color:var(--mid);font-size:12px;margin-left:3px;white-space:nowrap}
.toc-side{display:none}
.totop{position:fixed;right:18px;bottom:18px;background:var(--forest);color:#fff!important;padding:8px 16px;border-radius:999px;
font-size:13px;font-weight:700;text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.18)}
@media screen and (min-width:1500px){.toc-side{display:block;position:fixed;top:24px;left:max(16px,calc(50% - 770px));
width:230px;max-height:calc(100vh - 48px);overflow:auto;font-size:13px;line-height:1.35}
.toc-side .eyebrow{margin:0 0 8px 10px}.toc-side ol{list-style:none;padding:0;margin:0}
.toc-side ol ol{padding-left:12px;margin:1px 0 6px}
.toc-side a{display:block;padding:4px 10px;border-left:2px solid #e3e3e3;color:var(--dark);text-decoration:none}
.toc-side a:hover{color:var(--pine);background:#f4f8f6}
.toc-side a.on{border-left-color:var(--green);color:var(--pine);font-weight:700;background:#eef7f3}
.toc-side .tc{display:none}.totop{display:none}}
.runbook{break-before:page;margin-top:8px}
.runbook h2{margin-top:28px}
.phase{font-family:Archivo,Inter,sans-serif;font-weight:500;font-size:19px;color:var(--pine);margin:26px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--green);break-after:avoid}
.step{display:grid;grid-template-columns:30px 1fr;gap:12px;margin:0 0 16px;break-inside:avoid}
.sn{width:28px;height:28px;border-radius:50%;background:var(--green);color:#fff;font-weight:700;font-size:13px;display:flex;align-items:center;justify-content:center}
.sb h4{font-size:15px;margin:3px 0 4px;color:#1b1b1b}
.sb p{margin:0 0 8px}
.before{display:inline-block;font-size:12.5px;background:#fff2c6;color:#5c4500;border-radius:6px;padding:3px 9px;margin:0 0 8px}
.cmd,.file{position:relative;background:var(--pine);border-radius:8px;margin:0 0 6px}
.cmd pre,.file pre{margin:0;padding:10px 74px 10px 14px;color:#e8fff7;font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre;overflow-x:auto}
.file{background:#f4f7f6;border:1px solid #dfe7e4}.file pre{color:#1b1b1b;padding-right:14px}
.flabel{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--moss);padding:8px 14px 0}
.cmd button{position:absolute;top:7px;right:8px;font:600 12px Inter,sans-serif;color:var(--pine);background:var(--hl);border:0;border-radius:6px;padding:4px 10px;cursor:pointer}
.snote{font-size:13px;color:var(--mid);margin-top:4px}
.runs{margin:2px 0 6px;padding:6px 12px 8px;border-left:3px solid #dfe7e4;background:#fafcfb}
.runs pre{margin:0;color:#3a3a3a;font:11.5px/1.45 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.runs .flabel{padding:0 0 3px}
.sb code{font:12.5px ui-monospace,Menlo,Consolas,monospace;background:var(--pale);padding:1px 5px;border-radius:4px}
footer{margin-top:48px;padding-top:16px;border-top:1px solid #e3e3e3;display:flex;gap:18px;align-items:center;font-size:12px;color:var(--mid)}
footer img{height:20px;width:auto;flex:none}
@page{size:A4;margin:14mm 12mm}
@media print{body{font-size:12.5px;line-height:1.5}.hero{border-radius:0;padding:26px 30px 30px}.hero .top{margin-bottom:28px}
.hero h1{font-size:32px}.page{padding:0 4px 20px}h2{font-size:22px;margin-top:24px}.lead{font-size:15px}
.summary{margin-top:22px;grid-template-columns:170px 1fr;gap:24px}.ring{width:160px;height:160px}
.owner{margin-top:20px}.owner-head{margin-bottom:10px}.item{padding:10px 12px;margin-bottom:8px}
.item ol{margin-top:2px}.names{font-size:11.5px;padding:6px 10px}.rules-by-policy{font-size:11px}
.app-item{padding:7px 0}.code{font-size:11px}.cmd button{display:none}.cmd pre,.file pre{font-size:10px;padding-right:12px;white-space:pre-wrap}
.step{margin-bottom:10px}.phase{font-size:16px;margin-top:18px}
.toc-side,.totop{display:none!important}.toc{margin-top:14px;padding:10px 16px}.toc li{margin:1px 0}
a{text-decoration:none}}
"""


def _esc(s):
    return html.escape(str(s), quote=True)


FAMILY_NAMES = {
    "internetFirewall": "Internet Firewall", "wanFirewall": "WAN Firewall", "wanNetwork": "WAN Network",
    "applicationControl": "Application Control", "tlsInspect": "TLS Inspection", "socketBypass": "Socket Bypass",
    "socketLan": "LAN Firewall", "clientConnectivity": "Client Connectivity", "privateAccess": "Private Access",
    "splitTunnel": "Split Tunnel", "appTenantRestriction": "App Tenant Restriction", "ztnaAlwaysOn": "Always-On",
    "dynamicIpAllocation": "Dynamic IP Allocation", "remotePortFwd": "Remote Port Forwarding",
    "sdpStaticIpAllocation": "Static IP Allocation", "siteWebProxy": "Site Web Proxy",
    "antiMalwareFileHash": "Anti-Malware File Hash", "terminalServer": "Terminal Server",
}
_FAM_RE = re.compile(r"\b(" + "|".join(FAMILY_NAMES) + r"): ")


def _friendly(text, key=None):
    """Customer-facing form of an engineering list entry."""
    t = _FAM_RE.sub(lambda m: FAMILY_NAMES[m.group(1)] + ": ", str(text))
    t = re.sub(r"\b1 rule\(s\)", "1 rule", t)
    t = t.replace("rule(s)", "rules").replace(" :: ", " / ")
    if key in ("lic_skus_unknown", "lic_skus_missing"):
        m = re.match(r"^(\S+) \((.+)\)$", t)
        if m:
            t = f"{m.group(2)[0].upper()}{m.group(2)[1:]} ({m.group(1)})"
    return t


# --------------------------------------------------------------------------
# Part 2: the runbook. Built from the findings and the run details (meta["run"]),
# so every command names this pair of accounts, profiles and folders.

PURGE_KEYS = {
    "internetFirewall": "internet_firewall", "wanFirewall": "wan_firewall", "wanNetwork": "wan_network",
    "tlsInspect": "tls_inspection", "socketLan": "lan_firewall", "applicationControl": "application_control",
    "remotePortFwd": "remote_port_forward", "socketBypass": "socket_bypass", "splitTunnel": "split_tunnel",
    "terminalServer": "terminal_server", "ztnaAlwaysOn": "always_on", "clientConnectivity": "client_connectivity",
    "dynamicIpAllocation": "dynamic_ip_allocation", "privateAccess": "private_access",
    "appTenantRestriction": "app_tenant_restriction", "antiMalwareFileHash": "anti_malware_file_hash",
    "sdpStaticIpAllocation": "sdp_static_ip_allocation", "siteWebProxy": "site_web_proxy",
}
# Target export in parts: one long run can be cut off part way and still exit 0. Each part's --types list is
# one shell word, so it is kept short enough to print on one line (a wrapped word breaks when copied from a PDF).
EXPORT_TYPES = ("allocated_ips,amfh_rules,ao_rules,apc_rules,atr_rules,cc_rules,custom_apps,dhcp_relay_groups,dia_rules,"
                "fqdn_containers,global_ip_ranges,groups,if_rules,interconnect_sites,ip_range_containers,ipsec_sites,"
                "lan_fw_rules,mailing_lists,pa_rules,private_apps,app_connectors,rpf_rules,sb_rules,socket_sites,ssia_rules,"
                "st_rules,subscription_groups,swp_rules,tls_rules,ts_rules,vsocket_sites,wf_rules,wnw_rules").split(",")


def _type_parts(limit=72):
    parts, cur = [], []
    for t in EXPORT_TYPES:
        if cur and len(",".join(cur + [t])) > limit:
            parts.append(",".join(cur))
            cur = []
        cur.append(t)
    return parts + [",".join(cur)]
# Which action plan items belong before which deploy step.
BEFORE_FOUNDATIONS = ("access_same_account", "perm_source", "perm_target", "export_refused", "export_failed",
                      "export_policy_missing", "export_incomplete", "export_hosts_short", "lic_ips_short", "lic_ips_unknown",
                      "lic_sites_short", "lic_sites_unknown", "lic_sites_none", "pop_missing", "feat_srt", "feat_wifi",
                      "feat_webhooks", "int_classic", "tgt_existing_named", "access_trial")
BEFORE_POLICIES = ("id_missing", "id_scim_inactive", "id_ldap_inactive", "id_scim_endpoint_fail", "cma_missing",
                   "custom_app_unbuildable",
                   "cma_unverified", "cma_undecided", "feat_rbi_disabled", "feat_rbi_unverified", "feat_swp_fail",
                   "feat_swp_unverified", "feat_sanctioned", "tgt_clash", "lic_ztna_short", "lic_ztna_unknown",
                   "lic_skus_missing", "lic_skus_unknown", "int_stale", "int_unchecked")


def _q(s):
    s = str(s)
    return s if re.fullmatch(r"[A-Za-z0-9_./:=@%+-]+", s) else "'" + s.replace("'", "'\"'\"'") + "'"


def build_runbook(rep, meta):
    """Phases of steps for Part 2. Empty without meta['run'].

    A step: {id, n, title, text, cmds, runs, sub, script, before (finding keys), file, note}.
    `cmds` is what the reader pastes: short lines (at most CMD_LINE_MAX) that print whole, chained
    with "&&" when there are several, so they paste the same whether a PDF viewer keeps line breaks
    or turns them into spaces. A step that runs long commands gets a subcommand `sub` in the
    generated migrate.sh (`script` = its body); `cmds` is then `sh <out>/migrate.sh <sub>` and
    `runs` lists the full commands for reference.
    """
    run = meta.get("run")
    if not run:
        return []
    sp, tp, sa, ta = run["source_profile"], run["target_profile"], run["source_account"], run["target_account"]
    cfg, tcfg, out = run["config_dir"], run["target_config_dir"], run["out_dir"]
    mir = " --mirror" if run.get("mirror") else ""
    fs = {}
    for f in rep.findings:
        fs.setdefault(f["key"], []).append(f)

    def has(*keys):
        return any(k in fs for k in keys)

    def first(key):
        return fs[key][0] if key in fs else None

    common = ((f" --config-dir {_q(cfg)}" if cfg != "config_data" else "")
              + (f" --out-dir {_q(out)}" if out != "preflight" else ""))
    check_first = (f"python3 scripts/preflight_check.py --source-profile {_q(sp)} --target-profile {_q(tp)}"
                   + (" --probe" if run.get("probe") else "") + common)
    check = (check_first + (" --lan-l7-confirmed" if run.get("lan_l7_confirmed") else "") + " --pdf"
             + (f" --customer {_q(run['customer'])}" if run.get("customer") else ""))
    check_l7 = check if run.get("lan_l7_confirmed") else check.replace(" --pdf", " --lan-l7-confirmed --pdf")
    deploy_all = f"python3 scripts/bulk_apply.py --profile {_q(tp)} --mode deploy{mir} --config-dir {_q(tcfg)} --keep-going"
    vexp = f"{out}/target_export"
    script_path = f"{out}/migrate.sh"
    leave_out = [("run", f"cp {_q(out + '/exclude-0-dependencies.auto.tfvars')} 0-dependencies/"),
                 ("run", f"cp {_q(out + '/exclude-1-sites.auto.tfvars')} 1-sites/"),
                 ("run", f"sh {_q(out + '/scope_command.sh')}")]

    def step(sid, title, text, cmds=(), before=(), file=None, note=None, sub=None, script=None):
        s = {"id": sid, "title": title, "text": text, "before": [k for k in before if k in fs], "file": file,
             "note": note, "sub": sub, "script": script or [], "cmds": list(cmds), "runs": []}
        if sub:
            s["cmds"] = [f"sh {_q(script_path)} {sub}"]
            s["runs"] = [c for kind, c in s["script"] if kind in ("run", "run_any")]
        return s

    exported = has("export_ok") and not has("export_incomplete", "export_hosts_short")
    done_here = "Already done on the machine that made this report: skip this step there."
    setup = [
        step("tools", "Get the tools and the code",
             "You need Terraform 1.7 or newer, Python 3 and git. This downloads the migration repo into a new "
             "cato-bulk-config-management folder and installs the Cato CLI inside it. Every command in this part "
             "runs from that folder. In a new terminal window, go back into the folder (cd "
             "cato-bulk-config-management) and run the last line again first, or the Cato CLI isn't found.",
             TOOLS_CMDS,
             note=done_here + ((f" It serves Terraform provider {run.get('provider_version')} from a local build in "
                                "~/.terraform.d/mirror, so the deploy steps pass --mirror. Build the provider the same "
                                "way on any other machine, or drop --mirror from migrate.sh once those provider fixes "
                                "are released.") if run.get("mirror") else "")),
        step("keys", "Save the two API keys as profiles",
             "Replace CURRENT_KEY with the current account's view-only key and NEW_KEY with the new account's key "
             "that can edit, keeping the single quotes. Each key is tested before it's saved: if catocli prints "
             "ERROR, that key wasn't saved, so fix it and run its line again on its own. The last two commands "
             f"show which account each profile points at: {sa} for {sp} and {ta} for {tp}.",
             [f"catocli configure set --profile {_q(sp)} --cato-token 'CURRENT_KEY' --account-id {_q(sa)}",
              f"catocli configure set --profile {_q(tp)} --cato-token 'NEW_KEY' --account-id {_q(ta)}",
              f"python3 scripts/cato_profile.py {_q(sp)}", f"python3 scripts/cato_profile.py {_q(tp)}"],
             note=done_here),
        step("export", "Export the current account",
             "Reads every setting the migration copies into files, and only reads. If the key is refused a type "
             "for lack of permission, it carries on with the rest and the check lists what was refused.",
             [f"python3 scripts/export_source.py --profile {_q(sp)}" + (f" --config-dir {_q(cfg)}" if cfg != "config_data" else "")],
             before=("export_incomplete", "export_hosts_short"),
             note=("Already done for this report. Run it again if the current account changes, then run the check "
                   "again.") if exported else None),
    ]
    setup += [
        step("firstcheck", "Run the check on this machine",
             f"Writes this report and {script_path}, which runs every step from here on. Already done on the "
             "machine that made this report.", [check_first]),
    ]
    prepare = [
        step("guard", "Protect the current account",
             "Stops every script in the repo from changing the current account.", sub="guard",
             script=[("run", f"printf '%s\\n' '{{' '\"protected_accounts\": [\"{sa}\"],' "
                             f"'\"protected_profiles\": [\"{sp}\"]' '}}' > cbcm.config.json")]),
        step("recheck", "Work through the action plan, then run the check again",
             "Each time an item is done, run the check again. It refreshes this report and rewrites the files "
             "{step:leaveout} uses, and migrate.sh itself.", sub="check", script=[("run_any", check)]),
    ]
    if has("int_classic"):
        names = [x.split(": ", 1)[0] for x in first("int_classic")["items"]]
        prepare.append(step("classic", "Add the legacy groups to the export",
                            f"Open each of these in the current account's CMA (Resources, Groups) and note its members: "
                            f"{', '.join(names)}. Add each one to {cfg}/groups.json as an Advanced Group with the same name "
                            "and members, keeping empty groups empty. Exporting again overwrites this file, so add them "
                            "again after any new export. Then check the file still reads.",
                            before=("int_classic",), sub="check-groups",
                            script=[("run", f"python3 -m json.tool {_q(cfg + '/groups.json')} > /dev/null && "
                                            "echo 'groups.json is fine'")]))
    if has("id_missing", "id_scim_inactive"):
        prepare.append(step("scim", "Copy users and groups (test accounts only)",
                            "For a real migration, your identity team provisions users and groups into the new account "
                            "(the identity items above). For a test account you can copy them instead. Everything copied "
                            "arrives as SCIM. Create the new account's SCIM integration first, then replace SCIM_URL "
                            "and SCIM_TOKEN with its URL and token, keeping the single quotes: the first command adds "
                            f"them to the {tp} profile. The second shows what it would copy and asks before copying. "
                            f"Afterwards, delete {cfg}/scim_users.csv and scim_groups.csv: they hold people's names "
                            "and email addresses.",
                            [f"catocli configure set --profile {_q(tp)} --scim-url 'SCIM_URL' --scim-token 'SCIM_TOKEN'",
                             f"sh {_q(script_path)} copy-users"],
                            script=[("run", f"python3 scripts/scim_sync.py export --source-profile {_q(sp)}"),
                                    ("run", f"python3 scripts/scim_sync.py push --target-profile {_q(tp)}"),
                                    ("confirm", f"Copy these users and groups into account {ta} for real?"),
                                    ("run", f"python3 scripts/scim_sync.py push --target-profile {_q(tp)} --execute")]))
        prepare[-1]["runs"] = [c for k, c in prepare[-1]["script"] if k == "run"]
        prepare[-1]["sub"] = "copy-users"
    if has("tgt_clash"):
        fams = [PURGE_KEYS.get(x, x) for x in first("tgt_clash")["ctx"].get("families") or []]
        if fams:
            only = ",".join(fams)
            prepare.append(step("purge", "Clear the sample rules from the new account",
                                "Only on a brand-new account: this deletes every rule in these policies. It lists what "
                                f"it would delete and asks first; then the delete itself asks you to type {ta}.",
                                before=("tgt_clash",), sub="clear-samples",
                                script=[("run", f"python3 scripts/purge_all.py --profile {_q(tp)} --only {only}"),
                                        ("confirm", f"Delete these rules from account {ta}?"),
                                        ("run", f"python3 scripts/purge_all.py --profile {_q(tp)} --only {only} --execute")]))
    prepare.append(step("leaveout", "Leave out what isn't ready yet",
                        "Copies the two exclusion files this check wrote into their folders, then writes "
                        f"{tcfg}: the export without the rules that wait on anything still missing (Appendix A lists "
                        "them). With nothing missing it's an identical copy, so always run this step, and run it again "
                        "after every check.", sub="leave-out", script=leave_out))
    if has("feat_wifi"):
        keys = first("feat_wifi")["items"]
        prepare.append(step("wifi", "Add the Wi-Fi passwords",
                            "Open a new file and type each network's real password between the quotes. Terraform uses "
                            "exactly what's there as the live password, so never leave a placeholder in it. Save with "
                            "Ctrl+O, then Return, then leave with Ctrl+X.",
                            ["nano 1-sites/wifi.auto.tfvars"],
                            file="wifi_ssid_passkeys = {\n" + "\n".join(f'  "{k}" = ""' for k in keys) + "\n}",
                            before=("feat_wifi",)))
    if has("feat_webhooks"):
        names = [x.split(": ", 1)[0] for x in first("feat_webhooks")["items"]]
        prepare.append(step("webhooks", "Add the webhook secrets",
                            "Webhooks copy across without their secrets. For each one that signs in, add one line using "
                            "the shape that matches how it signs in; webhooks that don't sign in need nothing. Without an "
                            "entry, a webhook that signs in is skipped.",
                            ["nano 0-dependencies/secrets.auto.tfvars"],
                            file="# Webhooks in this export: " + ", ".join(names) + "\n"
                                 "webhook_secrets = {\n"
                                 '  "WEBHOOK_NAME" = { bearer = { bearer_token = "" } }\n'
                                 '  # or { basic = { username = "", password = "" } }\n'
                                 '  # or { custom = { name = "x-api-key", value = "" } }\n}',
                            before=("feat_webhooks",)))
    deploy = [
        step("foundations", "Build the shared objects, sites, app connectors and groups",
             f"Builds the first four folders in order. It asks you to type {ta} to confirm, then ends with a table: every "
             "line should say applied. The sites show Disconnected until their Sockets are connected, which is expected.",
             before=BEFORE_FOUNDATIONS, sub="foundations",
             script=[("run", f"python3 scripts/bulk_apply.py --profile {_q(tp)} --mode deploy{mir} --config-dir {_q(tcfg)} "
                             "--folders 0-dependencies,1-sites,2-app-connectors,3-groups")]),
        step("policies", "Add the policies",
             f"Deploys every policy folder in order and asks for {ta} again. Rules that wait on a missing item stay out "
             "until {step:after} brings them back.", before=BEFORE_POLICIES, sub="policies",
             script=[("run", f"python3 scripts/bulk_apply.py --profile {_q(tp)} --mode deploy{mir} --config-dir {_q(tcfg)} "
                             "--keep-going --start-at 4-policies/always-on")]),
    ]
    verify = [
        step("replan", "Confirm Terraform has nothing left to do",
             "Changes nothing. Every line should say ok, with 0 to add, change or delete.", sub="replan",
             script=[("run_any", f"python3 scripts/bulk_apply.py --profile {_q(tp)} --mode plan{mir} --config-dir {_q(tcfg)} "
                                 "--keep-going")]),
        step("export_target", "Export the new account",
             "In parts, because one long export can be cut off part way. Check the last lines of each part say every "
             "type succeeded.", sub="export-new",
             script=[("run", f"CATO_PROFILE={_q(tp)} catocli export all --no-account-suffix --output-directory {_q(vexp)} "
                             f"--types {h}") for h in _type_parts()]
             + [("run", f"CATO_PROFILE={_q(tp)} catocli export static_hosts --site-type all -f json "
                        f"--output-directory {_q(vexp)}")]),
        step("compare", "Check rule order and content against the source",
             "A plan can't see either. The first should end with ALL ORDERING MATCHES. In the second, the only "
             "differences to expect are PoP address names (allocationIp), which every account gets its own of.",
             sub="compare", script=[("run_any", f"python3 scripts/order_check.py {_q(tcfg)} {_q(vexp)}"),
                                    ("run_any", f"python3 scripts/fidelity_check.py {_q(tcfg)} {_q(vexp)}")]),
    ]
    bring_back = [("run_any", check)] + leave_out + [("run", deploy_all)]
    finish = []
    if has("feat_waf_range", "feat_waf_range_early"):
        names = (first("feat_waf_range") or first("feat_waf_range_early"))["ctx"].get("names") or []
        finish.append(step("waf", "Create the blocked IP ranges by hand",
                           f"Now that the shared objects exist, create {', '.join(names)} in the new account's CMA "
                           "(Resources, IP Ranges) with the same values as in the current account. Then bring them in: "
                           "this runs the check, leaves out what's still missing and deploys. Don't deploy any other "
                           "way first: the shared objects folder now owns every IP range in the account and would "
                           "delete one the old exclusion file still leaves out.",
                           before=("feat_waf_range", "feat_waf_range_early"), sub="bring-back", script=bring_back))
    if has("feat_l7"):
        sites = ", ".join(first("feat_l7")["ctx"].get("sites") or [])
        finish.append(step("l7", "Turn on LAN Layer 7 and add its rules",
                           f"In the new account's CMA, turn on LAN Layer 7 enforcement for {sites}. The API can't see "
                           "that switch, so this runs the check with --lan-l7-confirmed (and the migrate.sh it writes "
                           "keeps it from then on), leaves out what's still missing and deploys.",
                           before=("feat_l7",), sub="bring-back-l7",
                           script=[("run_any", check_l7)] + leave_out + [("run", deploy_all)]))
    finish.append(step("after", "Bring left-out rules back in",
                       "Whenever a missing item arrives in the new account, run this: it runs the check, leaves out "
                       "what's still missing and deploys. Folders with nothing new to do say so and change nothing.",
                       sub="bring-back", script=bring_back))
    if has("feat_ipsec"):
        finish.append(step("ipsec", "Add the IPSec tunnels",
                           "Tunnel settings and pre-shared keys never leave the current account. Add them in the new "
                           "account's CMA for: " + ", ".join(first("feat_ipsec")["items"]) + ".",
                           before=("feat_ipsec",)))
    finish.append(step("hardware", "Connect the Sockets and cloud sites",
                       "Terraform builds the site configuration, not the hardware. Plan moving each physical Socket and "
                       "redeploying each cloud vSocket with your Cato account team."))
    finish.append(step("trouble", "If a step fails",
                       "Read the error's last lines: they're Cato's own message and usually say what's missing. Fix the "
                       "cause, then carry on from the folder that failed, using its name from the summary table (the "
                       "folder below is an example). Don't change or delete what Terraform built by hand in the CMA, and "
                       "never delete a terraform.tfstate file.",
                       [f"sh {_q(script_path)} resume 4-policies/internet-firewall"]))
    finish[-1]["runs"] = [f"python3 scripts/bulk_apply.py --profile {_q(tp)} --mode deploy{mir} --config-dir {_q(tcfg)} "
                          "--keep-going --start-at FOLDER"]
    phases = [("Set up, once on each machine", setup), ("Prepare", prepare), ("Run the migration", deploy),
              ("Check the result", verify), ("Finish up", finish)]
    n, ids = 0, {}
    for _, steps in phases:
        for s in steps:
            n += 1
            s["n"] = n
            ids[s["id"]] = n
    for _, steps in phases:
        for s in steps:
            s["text"] = re.sub(r"\{step:(\w+)\}", lambda m: f"step {ids.get(m.group(1), '?')}", s["text"])
    return phases


# Step 1 of Part 2, the same lines as the getting started guide's first block: without the virtual environment
# and the PATH line, the next step's `catocli` isn't found.
TOOLS_CMDS = [
    "git clone https://github.com/catonetworks/cato-bulk-config-management.git",
    "cd cato-bulk-config-management",
    "python3 -m venv .venv && .venv/bin/pip install catocli",
    'export PATH="$PWD/.venv/bin:$PATH"',
]

CMD_LINE_MAX = 100  # longest command line Part 2 prints (10px code fits ~104 on an A4 line): wider would wrap in
# print, and a wrapped line breaks when copied


def chain(cmds, sep="&&"):
    """Commands one per line, every line but the last ending in "&&" (or ";"): pasted with line breaks the shell
    carries on after "&&", pasted by a PDF viewer that turns line breaks into spaces it is the same chain on one
    line. ";" runs every command whatever the one before returned: for exports, where one partly refused type
    must not stop the rest."""
    end = " &&" if sep == "&&" else ";"
    return "\n".join(c + (end if i < len(cmds) - 1 else "") for i, c in enumerate(cmds))


def _sq(cmd):
    return "'" + cmd.replace("'", "'\\''") + "'"


MIGRATE_HEAD = r"""#!/bin/sh
# Written by preflight_check.py on {when} for {source} -> {target}.
# Every run of the check rewrites it. Part 2 of preflight_report.html says when to run each step.
# Usage: sh {out}/migrate.sh STEP    (sh {out}/migrate.sh list shows the steps)
cd {root} || exit 1

run() {{  # show a command, run it, and stop if it fails
  printf '\n$ %s\n' "$1"
  sh -c "$1" || {{ printf '\nStopped: that command failed. Fix the cause, then run this step again.\n' >&2; exit 1; }}
}}
run_any() {{  # show a command and run it; its exit code is part of its answer (the check exits 2 on blockers)
  printf '\n$ %s\n' "$1"
  sh -c "$1"
}}
confirm() {{  # ask before anything that changes the new account for real
  printf '\n%s [y/N] ' "$1"
  read -r answer
  case "$answer" in y|Y|yes|Yes|YES) return 0 ;; *) printf 'Not done.\n'; exit 0 ;; esac
}}
"""


def migrate_script(phases, repo_root, meta):
    """migrate.sh: one subcommand per run book step that runs commands, so Part 2 prints short lines."""
    seen, funcs, cases, usage = set(), [], [], []
    for _, steps in phases:
        for s in steps:
            if not s.get("sub") or s["sub"] in seen or not s.get("script"):
                continue
            seen.add(s["sub"])
            fname = "step_" + s["sub"].replace("-", "_")
            funcs.append(f"{fname}() {{\n" + "\n".join(f"  {kind} {_sq(arg)}" for kind, arg in s["script"]) + "\n}")
            cases.append(f"  {s['sub']}) {fname} ;;")
            usage.append(f"  {s['sub']:<15} {s['title']}")
    usage.append(f"  {'resume FOLDER':<15} Carry on a deploy from FOLDER (its name from the summary table)")
    deploy = next((c for _, st in phases for s in st if s["id"] == "trouble" for c in s["runs"]), "")
    run = meta.get("run") or {}
    head = MIGRATE_HEAD.format(when=meta.get("when", ""), source=meta.get("source", ""), target=meta.get("target", ""),
                               out=run.get("out_dir", "preflight"), root=_sq(repo_root))
    return (head + "\n" + "\n\n".join(funcs) + "\n\nusage() {\n  cat <<'STEPS'\nSteps (Part 2 of the report says when "
            "to run each):\n" + "\n".join(usage) + "\nSTEPS\n}\n\ncase \"$1\" in\n" + "\n".join(cases)
            + f"\n  resume) [ -n \"$2\" ] || {{ usage; exit 2; }}; run {_sq(deploy.replace('FOLDER', ''))}\"$2\" ;;"
            + "\n  list|help|-h|--help|\"\") usage ;;"
            + "\n  *) printf 'Unknown step: %s\\n' \"$1\" >&2; usage; exit 2 ;;\nesac\n")


def write_migrate_sh(phases, repo_root, out_dir, meta):
    """Write <out_dir>/migrate.sh by replacing the file whole, so a step that reruns the check (which rewrites
    it) keeps reading the copy it started with. Returns the file name, or None without a run book."""
    if not phases:
        return None
    path = os.path.join(out_dir, "migrate.sh")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(migrate_script(phases, repo_root, meta))
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)
    return "migrate.sh"


def _inline(text):
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", _esc(text))
    return re.sub(r"\bstep (\d+)\b", r'<a href="#step-\1">step \1</a>', t)


def _item_links(nums):
    return ", ".join(f'<a href="#item-{x}">{x}</a>' for x in nums)


def render_runbook_html(phases, item_nums):
    if not phases:
        return ""
    out = ['<section class="runbook" id="part2"><div class="eyebrow">Part 2</div><h2>Running the migration</h2>'
           '<p>For the engineers who run the migration. Every command is set for these two accounts and fits on one '
           'line, so it copies cleanly from the PDF too. Most steps run a step of <code>migrate.sh</code>, which the '
           'check writes next to this report; what each one runs is listed under it. Steps that say <i>Before this '
           'step</i> point at the items in the action plan '
           'that belong first. Anything still missing is left out by the leave-out step and comes back later, so the '
           'migration can go ahead without waiting for everything.</p>']
    for i, (title, steps) in enumerate(phases, 1):
        out.append(f'<h3 class="phase" id="phase-{i}">{_esc(title)}</h3>')
        for s in steps:
            nums = sorted({x for k in s["before"] for x in item_nums.get(k, [])})
            out.append(f'<div class="step" id="step-{s["n"]}"><div class="sn">{s["n"]}</div><div class="sb"><h4>{_esc(s["title"])}</h4>'
                       f'<p>{_inline(s["text"])}</p>'
                       + (f'<div class="before">Before this step: item{"s" if len(nums) != 1 else ""} '
                          f'{_item_links(nums)}</div>' if nums else "")
                       + (f'<div class="cmd"><button type="button">Copy</button><pre>{_esc(chain(s["cmds"], s.get("sep", "&&")))}</pre></div>'
                          if s["cmds"] else "")
                       + (f'<div class="runs"><div class="flabel">What it runs (for reference)</div>'
                          f'<pre>{_esc(chr(10).join(s["runs"]))}</pre></div>' if s.get("runs") else "")
                       + (f'<div class="file"><div class="flabel">File contents</div><pre>{_esc(s["file"])}</pre></div>'
                          if s.get("file") else "")
                       + (f'<p class="snote">{_esc(s["note"])}</p>' if s.get("note") else "") + "</div></div>")
    out.append("</section>")
    return "\n".join(out)


def render_runbook_md(phases, titles=None):
    """Markdown twin of Part 2 for preflight_report.md; 'before' items are named by their finding titles."""
    if not phases:
        return []
    out = ["## Running the migration", "",
           "Every command runs from the cato-bulk-config-management folder and is set for these two accounts. Only the "
           "words in CAPITALS are yours to fill in.", ""]
    for title, steps in phases:
        out += [f"### {title}", ""]
        for s in steps:
            out += [f"**{s['n']}. {s['title']}**", "", s["text"], ""]
            if s["before"] and titles:
                out += ["Before this step: " + "; ".join(titles[k] for k in s["before"] if k in titles) + ".", ""]
            if s["cmds"]:
                out += ["```bash", chain(s["cmds"], s.get("sep", "&&")), "```", ""]
            if s.get("runs"):
                out += ["What it runs (for reference):", "", "```text", *s["runs"], "```", ""]
            if s.get("file"):
                out += ["```hcl", s["file"], "```", ""]
            if s.get("note"):
                out += [f"*{s['note']}*", ""]
    return out


COPY_JS = """<script>document.querySelectorAll('.cmd button').forEach(function(b){b.addEventListener('click',function(){
var t=b.nextElementSibling.textContent;function done(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy'},1500)}
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(done)}else{var a=document.createElement('textarea');
a.value=t;document.body.appendChild(a);a.select();try{document.execCommand('copy');done()}catch(e){}document.body.removeChild(a)}})});</script>"""


def _toc(entries, cls, label):
    """entries: (anchor, title, note, children) with children as (anchor, title, note)."""
    def li(a, t, note):
        return f'<a href="#{a}">{_esc(t)}</a>' + (f' <span class="tc">{_esc(note)}</span>' if note else "")
    rows = "".join(f'<li>{li(a, t, n)}' + (f'<ol>{"".join(f"<li>{li(*c)}</li>" for c in kids)}</ol>' if kids else "") + "</li>"
                   for a, t, n, kids in entries)
    ident = ' id="contents"' if cls == "toc" else ""
    return f'<nav class="{cls}"{ident} aria-label="{label}"><div class="eyebrow">Contents</div><ol>{rows}</ol></nav>'


NAV_JS = """<script>(function(){var links=document.querySelectorAll('.toc-side a'),map={};
links.forEach(function(a){map[a.getAttribute('href').slice(1)]=a});if(!('IntersectionObserver' in window))return;
var obs=new IntersectionObserver(function(es){es.forEach(function(e){if(!e.isIntersecting)return;
links.forEach(function(a){a.classList.remove('on')});var a=map[e.target.id];if(a)a.classList.add('on')})},
{rootMargin:'0px 0px -70% 0px'});Object.keys(map).forEach(function(id){var el=document.getElementById(id);if(el)obs.observe(el)})})();</script>"""


def _shown(rep):
    """Actionable findings as the customer sees them: directory items that repeat another are folded in."""
    keys = {(f["key"], f["ctx"].get("type")) for f in rep.findings}
    out = []
    for f in rep.findings:
        if f["severity"] not in ("BLOCKER", "ACTION", "WARN"):
            continue
        if f["key"] == "id_scim_inactive" and ("id_missing", "SCIM") in keys:
            continue
        if f["key"] == "id_ldap_inactive" and ("id_missing", "LDAP") in keys:
            continue
        out.append(f)
    return out


def render_customer_html(rep, exp, meta, customer=None):
    total = len(exp["rules"])
    actionable = _shown(rep)
    # One count everywhere: a rule "waits" if any item in the action plan names it (Appendix A lists them).
    req_rules = {r for f in actionable if f["severity"] == "BLOCKER" for r in f["rules"]}
    waits_all = {r for f in actionable for r in f["rules"]}
    ready_now = max(0, total - len(waits_all))
    pct = ready_now / total if total else 1.0
    counts = {s: sum(1 for f in actionable if f["severity"] == s) for s in ("BLOCKER", "ACTION", "WARN")}
    counts["OK"] = sum(1 for f in rep.findings if f["severity"] == "OK")
    if counts["BLOCKER"]:
        status = "Preparation needed"
        lead = (f"{ready_now} of your {total} policy rules can move to the new account as they are. Before the migration "
                f"starts, {_pl(counts['BLOCKER'], 'item')} {'needs' if counts['BLOCKER'] == 1 else 'need'} to be in place "
                f"and {counts['ACTION']} more {'needs' if counts['ACTION'] == 1 else 'need'} doing or confirming. Each one "
                "says who acts, why it matters and what to do.")
    elif counts["ACTION"]:
        status = "Nearly ready"
        lead = (f"Nothing stands in the way of the migration. {_pl(counts['ACTION'], 'item')} "
                f"{'needs' if counts['ACTION'] == 1 else 'need'} doing or confirming first, listed below with who acts "
                "and what to do.")
    else:
        status = "Ready to migrate"
        lead = "Everything this check can verify is in place. Review the notes below, then schedule the migration."

    by_owner = {o[0]: [] for o in OWNERS}
    for f in actionable:
        g = guide(f)
        by_owner.setdefault(g["owner"], []).append((f, g))
    order = {"BLOCKER": 0, "ACTION": 1, "WARN": 2}

    head_meta = ([("Prepared for", customer)] if customer else []) + [
                 ("Current account", meta["source"]), ("New account", meta["target"]),
                 ("Checked", meta["when"] + (", including live verification" if meta.get("probe") else ""))]
    other = len(waits_all) - len(req_rules)
    parts = [f"""<!DOCTYPE html><html lang="en-US"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Migration readiness report: {_esc(meta['target'])}</title>
<!-- Fonts: Archivo and Inter, SIL Open Font License 1.1. Logo: official Cato artwork, unmodified. -->
<style>{_font_face()}{CSS}</style></head><body>
<header class="hero"><div class="top"><div class="logo">{_logo('cato_logo_rgb_white_art_101.svg')}</div>
<span class="pill">{_esc(status)}</span></div>
<div class="eyebrow">Migration readiness report</div>
<h1>Getting your new Cato account ready for migration</h1>
<div class="meta">{''.join(f'<span>{_esc(k)}: <b>{_esc(v)}</b></span>' for k, v in head_meta)}</div></header>
<main class="page">
<section class="summary" id="summary">{_ring(pct)}<div><p class="lead">{_esc(lead)}</p>
<div class="stats">
<div class="stat req"><div class="num">{counts['BLOCKER']}</div><div class="lbl">Required before migration</div></div>
<div class="stat todo"><div class="num">{counts['ACTION']}</div><div class="lbl">To do or confirm</div></div>
<div class="stat"><div class="num">{counts['WARN']}</div><div class="lbl">To review</div></div>
<div class="stat ok"><div class="num">{counts['OK']}</div><div class="lbl">Already in place</div></div>
</div></div></section>
<p class="affects">{len(req_rules)} of {total} policy rules wait on an item required before migration{
    f"; {other} more wait on an item to do, confirm or review" if other else ""}.{
    ' <a href="#part2">Part 2</a> of this report is the step-by-step for running the migration.' if meta.get("run") else ""}</p>
<!--TOC-->
<h2 id="plan">Your action plan</h2>
<p>Grouped by who acts. Tick each item as it's done, then run the check again to confirm.</p>"""]

    item_no, seq = {}, 0
    for key, _, _, _ in OWNERS:
        for f, _ in sorted(by_owner.get(key, []), key=lambda x: order[x[0]["severity"]]):
            seq += 1
            item_no[id(f)] = seq
    item_nums, item_titles = {}, {}
    for key, name, sub, icon in OWNERS:
        items = sorted(by_owner.get(key, []), key=lambda x: order[x[0]["severity"]])
        if not items:
            continue
        n_req = sum(1 for f, _ in items if f["severity"] == "BLOCKER")
        parts.append(f'<section class="owner" id="owner-{key}"><div class="owner-head"><div class="ico">{icon}</div>'
                     f'<div><h3>{_esc(name)}</h3><div class="sub">{_esc(sub)}</div></div>'
                     f'<div class="count">{_pl(len(items), "item")}'
                     f'{f" &middot; {n_req} required" if n_req else ""}</div></div>')
        for f, g in items:
            item_nums.setdefault(f["key"], []).append(item_no[id(f)])
            item_titles.setdefault(f["key"], g["title"])
            label, cls = LABELS[f["severity"]]
            steps = "".join(f"<li>{_esc(s)}</li>" for s in g["steps"])
            listed = g.get("items", f["items"])
            names = listed[:16]
            more = len(listed) - len(names)
            parts.append(
                f'<div class="item" id="item-{item_no[id(f)]}"><div class="box"></div><div>'
                f'<span class="chip {cls}">{_esc(label)}</span><h3><span class="no">{item_no[id(f)]}</span>{_esc(g["title"])}</h3>'
                f'<p class="why">{_esc(g["why"])}</p>'
                + (f'<div class="what">What to do</div><ol>{steps}</ol>' if steps else "")
                + (f'<div class="names">{"".join(f"<div>{_esc(_friendly(x, f['key']))}</div>" for x in names)}</div>' if names else "")
                + (f'<div class="more">and {more} more, listed in the appendix</div>' if more > 0 else "")
                + "</div></div>")
        parts.append("</section>")

    ok = [f for f in rep.findings if f["severity"] == "OK"]
    if ok:
        parts.append('<h2 id="ready">Already in place</h2><div class="ready">'
                     + "".join(f"<div>{ICON_CHECK}<span>{_esc(ready_text(f))}</span></div>" for f in ok) + "</div>")

    keys = {f["key"] for f in rep.findings}
    cant = ["Your identity provider's SCIM or directory settings (the check sees only the users and groups they create)."]
    if keys & {"feat_srt"}:
        cant.append("Whether Static Range Translation is turned on for the new account (the check sees each range's "
                    "translation, not the account setting).")
    if keys & {"feat_l7", "feat_l7_confirmed"}:
        cant.append("LAN Layer 7 enforcement on each site.")
    if keys & {"feat_sanctioned"}:
        cant.append("Which applications are marked as sanctioned.")
    if keys & {"access_trial"}:
        cant.append("License quantities, because the new account is a trial and doesn't report them.")
    parts.append("<h2 id=\"cant\">What this check can't confirm for you</h2><div class=\"note\"><p>The Cato API doesn't expose "
                 + ("this" if len(cant) == 1 else "these") + ", so please confirm directly:</p><ul>"
                 + "".join(f"<li>{_esc(x)}</li>" for x in cant) + "</ul></div>")

    phases = build_runbook(rep, meta)
    parts.append(render_runbook_html(phases, item_nums))

    # Appendix A: each affected rule once, with the numbered items it waits on.
    waits = {}
    for f in actionable:
        for r in f["rules"]:
            waits.setdefault(r, set()).add(item_no[id(f)])
    parts.append('<section class="appendix" id="appendix-a"><div class="eyebrow">Appendix A</div><h2>Policy rules and what they wait on</h2>'
                 f'<p>{len(waits)} of your {total} policy rules wait on at least one item. The numbers match the items in '
                 'your action plan.</p><div class="rules-by-policy">')
    by_pol = {}
    for r in waits:
        fam, _, name = r.partition(": ")
        by_pol.setdefault(FAMILY_NAMES.get(fam, fam), []).append((name, waits[r]))
    for pol in sorted(by_pol):
        rows = "".join(f'<div class="r"><span>{_esc(_friendly(n))}</span><span>{_item_links(sorted(w))}</span></div>'
                       for n, w in sorted(by_pol[pol], key=lambda x: x[0].lower()))
        parts.append(f'<div class="pol"><b>{_esc(pol)}</b>{rows}</div>')
    parts.append("</div>")
    long_lists = [(f, guide(f)) for f in actionable if len(guide(f).get("items", f["items"])) > 16]
    if long_lists:
        parts.append('<h2 id="full-lists">Full lists</h2>')
        for f, g in long_lists:
            parts.append(f'<div class="app-item"><div class="t"><a href="#item-{item_no[id(f)]}">{item_no[id(f)]}</a>. {_esc(g["title"])}</div>'
                         f'<div class="app-rules">{"".join(f"<div>{_esc(_friendly(x, f["key"]))}</div>" for x in g.get("items", f["items"]))}</div></div>')
    parts.append('<div class="eyebrow" style="margin-top:34px">Appendix B</div><h2 id="appendix-b">Technical notes for the migration team</h2>')
    for f in sorted(actionable, key=lambda x: order[x["severity"]]):
        if not (f["fix"] or f["detail"]):
            continue
        parts.append(f'<div class="app-item"><div class="t"><a href="#item-{item_no[id(f)]}">{item_no[id(f)]}</a>. {_esc(f["title"])}</div>'
                     + (f'<div class="code">{_esc(f["detail"])}</div>' if f["detail"] else "")
                     + (f'<div class="code">{_esc(f["fix"])}</div>' if f["fix"] else "") + "</div>")
    parts.append("</section>")
    parts.append(f'<footer>{_logo("cato_logo_rgb_color_art_101.svg")}<div>Generated by the migration readiness check '
                 f'(preflight_check.py) on {_esc(meta["when"])}. This report contains configuration names from your '
                 'accounts; share it only with the people working on your migration.</div></footer></main>'
                 + COPY_JS + NAV_JS + '</body></html>')

    # Chapters, now that every section and its numbers are known.
    owners = [(f"owner-{k}", name, _pl(len(by_owner[k]), "item"), ()) for k, name, _, _ in OWNERS if by_owner.get(k)]
    entries = [("summary", "Readiness summary", None, ()),
               ("plan", "Your action plan", _pl(len(actionable), "item"), [o[:3] for o in owners])]
    if ok:
        entries.append(("ready", "Already in place", None, ()))
    entries.append(("cant", "What this check can't confirm", None, ()))
    if phases:
        kids = [(f"phase-{i}", t, f"steps {st[0]['n']} to {st[-1]['n']}" if len(st) > 1 else f"step {st[0]['n']}")
                for i, (t, st) in enumerate(phases, 1)]
        entries.append(("part2", "Part 2: Running the migration", None, kids))
    entries.append(("appendix-a", "Appendix A: Policy rules and what they wait on", None, ()))
    if long_lists:
        entries.append(("full-lists", "Full lists", None, ()))
    entries.append(("appendix-b", "Appendix B: Technical notes", None, ()))
    toc = _toc(entries, "toc", "Contents") + _toc(entries, "toc-side", "Chapters") + '<a class="totop" href="#contents">Contents</a>'
    parts[0] = parts[0].replace("<!--TOC-->", toc, 1)
    return "\n".join(parts)


def find_browser():
    for p in [os.environ.get("CHROME_PATH"),
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              "/Applications/Chromium.app/Contents/MacOS/Chromium",
              shutil.which("google-chrome"), shutil.which("google-chrome-stable"), shutil.which("chromium"),
              shutil.which("chromium-browser"), shutil.which("msedge"),
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"]:
        if p and os.path.exists(p):
            return p
    return None


def html_to_pdf(html_path, pdf_path):
    """Print the report to PDF with a local headless Chrome/Edge. Returns an error string or None."""
    browser = find_browser()
    if not browser:
        return "no Chrome, Chromium or Edge found (set CHROME_PATH)"
    url = "file://" + os.path.abspath(html_path)
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
           f"--print-to-pdf={os.path.abspath(pdf_path)}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if not os.path.exists(pdf_path) or os.path.getmtime(pdf_path) < time.time() - 120:
        return (r.stderr or "PDF was not written")[-300:]
    return None
