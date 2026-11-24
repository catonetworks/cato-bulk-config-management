# Before you migrate: the dependency check

New to this? The [Getting Started Guide to Account Migration](getting-started.md) is the one-page
version: the keys to gather, the commands to run, and what to do next.

`scripts/preflight_check.py` compares a source tenant with a target tenant and
tells you, before anything is applied, what the target is missing. Run it as
soon as you have a source export and credentials for the target.

It exists because a migration's dependencies only show up one failed apply at a
time otherwise. In the first greenfield test (a reference tenant into a fresh
tenant) the policies hit, in turn: a 3-IP licence cap, China PoPs the target
could not use, SCIM user groups that did not exist, objects only the CMA can
create, RBI switched off, LAN Layer 7 off on a site, Site Web Proxy unavailable,
a global IP range the API's firewall blocks, and 45 factory rules with clashing
names. Run against that same pair, this check lists every one of them up front,
and its count of rules that will fail matches the 98 rules that run had to
leave out.

## Quick start

```bash
python3 scripts/preflight_check.py --source-profile <source> --target-profile <target>
```

Add `--probe` to verify the things the API cannot list (see below):

```bash
python3 scripts/preflight_check.py --source-profile <source> --target-profile <target> --probe
```

Or let `bulk_apply.py` run it first and stop on blockers:

```bash
python3 scripts/bulk_apply.py --profile <target> --mode deploy --preflight-source <source> ...
```

| Option | Meaning |
|---|---|
| `--source-profile` | catocli profile of the source tenant. A read-only key is enough. |
| `--target-profile` | catocli profile of the target tenant. |
| `--config-dir` | The source export (default `./config_data`). |
| `--probe` | Verify non-listable objects and features on the target inside a draft revision that is discarded straight away. Needs edit rights on the target. Refused for a protected account. |
| `--out-dir` | Where the reports go (default `./preflight`, gitignored). |
| `--customer` | Customer name for the cover of the branded report. |
| `--pdf` | Also print the branded report to PDF, using a local Chrome, Chromium or Edge (`CHROME_PATH` to point at one). |
| `--strict` | Exit 1 when ACTION items remain, for CI. |
| `--quiet` | Print only the summary lines. |

Exit code: `2` when there is at least one BLOCKER, `1` with `--strict` and
ACTION items, otherwise `0`.

## Reading the result

Every finding has a severity:

| Severity | Meaning |
|---|---|
| BLOCKER | The migration will fail until this is done. |
| ACTION | Do this before you migrate, or confirm it is done (the API cannot see it). |
| WARN | Review it. |
| INFO, OK | For the record. |

The summary also counts the rules that depend on a BLOCKER (they will fail) and
on an ACTION item (they are at risk). Feed the same reasons to
`scripts/scope_policies_to_target.py` if you need to migrate what you can now
and the rest later.

The console shows the first dozen items of each finding. The full lists are in
`preflight/preflight_report.md` and, for scripts, `preflight/preflight_report.json`.

## The customer report

Every run also writes `preflight/preflight_report.html`: a Cato-branded readiness
report written for the customer, not for engineers. It's one self-contained file
(fonts and the official logo embedded), so it can be emailed as is, and it prints
cleanly to A4 or Letter. `--pdf` saves a PDF copy alongside it.

What it holds:

- A cover with the readiness status, the two accounts and when the check ran.
- Clickable chapters: a contents list under the summary (the action plan by who
  acts, Part 2 by phase, both appendices), a sidebar that follows you and marks
  the current chapter on wide screens, and a Contents button on narrower ones.
  Item numbers in Appendix A, Appendix B and the run book's "Before this step"
  lines link to the item, and "step N" in the run book links to that step. The
  links carry into the PDF.
- A readiness gauge (the share of policy rules that can move as they are) and
  the counts: required before migration, to do or confirm, to review, already in place.
- **Your action plan**: every item numbered and grouped by who acts (your Cato
  account team, Cato Support, your identity team, your Cato administrator, your
  migration team), each with why it matters, what to do and the names involved,
  with a box to tick.
- **Already in place** and **What this check can't confirm for you**.
- **Part 2, Running the migration**: the whole Terraform run as numbered steps,
  each command written out for this pair of accounts (profile names, account
  numbers, folders, the policies to clear, the Wi-Fi networks to fill in), with a
  Copy button on every command. Set up once per machine, prepare (export, rerun
  the check, legacy groups, copying identities to a test account, clearing sample
  rules, leaving out what isn't ready, secrets), run (foundations, then
  policies), check the result (re-plan, export the target, `order_check.py` and
  `fidelity_check.py`), and finish up (hand-made IP ranges, LAN Layer 7,
  bringing left-out rules back, IPSec tunnels, hardware, what to do when a step
  fails). Steps only appear when the findings call for them, and each deploy
  step says which action plan items belong before it. `--mirror` is added to
  every Terraform command when the pinned provider version is found in
  `~/.terraform.d/mirror`. The same steps are in `preflight_report.md`.
- `migrate.sh`, written next to the report on every run: one subcommand per
  Part 2 step (`sh preflight/migrate.sh foundations`, `... bring-back`,
  `... resume FOLDER`, `... list`), so every command Part 2 prints is one
  short line and copies cleanly even from a PDF viewer that turns line breaks
  into spaces. Each step shows what it runs, stops if a command fails, and
  asks before anything that deletes or copies into the new account. The
  first steps on a new machine are printed as short lines chained with `&&`,
  since `migrate.sh` doesn't exist yet: getting the repo and installing the
  Cato CLI in its own virtual environment (the same lines as the getting
  started guide), saving the keys with `catocli configure set --profile NAME
  --cato-token 'KEY' --account-id NUMBER` (account numbers filled in), the
  first export and the first check. The first two say they're already done on
  the machine that made the report.
- Appendix A: each affected policy rule once, with the numbered items it waits on.
  The cover, the action plan summary and this appendix all count the same rules.
- Appendix B: the technical notes for the migration engineers.

The wording follows the Cato brand voice: plain US English, sentence-case
headings, calm labels ("Required before migration" rather than "BLOCKER"), and
approved product names. The look follows the Brand Hub: Cato Green leads, Coral
and Sunflower only mark required and to-confirm items, Archivo and Inter type, and
the flat Ring as the readiness gauge. The assets live in `scripts/report_assets/`
(see its README); the logo files are checked against their published hashes and
left out if they have been modified.

To change what the report says about a finding, edit `guide()` in
`scripts/preflight_report.py`. Each finding has a stable key (for example
`pop_missing` or `cma_missing`), so the wording never depends on the console text.

## What it checks

| Area | Checks | How |
|---|---|---|
| Access | Both tenants reachable, not the same account, plan type. Trial accounts report no licence data, so capacity checks turn into a list of what to ask for. | `accountManagement` |
| Export | Every file the bulk folders read is present; static hosts were exported for every site type, not just sockets. | export folder + host count |
| Licensing | Public (allocated) IPs needed vs available, ZTNA users, add-on SKUs the source has and the target lacks. Sites: which exported sites hold a site licence or a share of pooled bandwidth in the source, against the target's free site licences and free pooled bandwidth (sites with no licence in the source need none). The API reports no bandwidth on site licences, so those compare by count. | `licensing` incl. `globalLicenseAllocations`, `SiteLicense.site`, `PooledBandwidthLicense.sites` |
| PoPs | Every PoP the source allocates IPs on, pins in WAN Network rules or uses for IPSec tunnels is available to the target, plus the rules and sites that depend on each missing one. | `entityLookup(type: location)` |
| Identity | Every user group and user the rules name: present in the target or not, grouped by how it gets there (SCIM, LDAP, manual, dynamic). Whether the target has any SCIM-provisioned users at all. With `scim_url` and `scim_token` in the target profile, a read-only call to the SCIM endpoint. | `user.userList`, `user.usersGroupList`, SCIM `GET /Users`, `/Groups` |
| CMA-only objects | Custom categories, device posture profiles (including `devicePostureProfile` in Always-On, TLS Inspection and Dynamic IP Allocation), bandwidth priorities beyond the defaults, user notification templates, DLP content and EDM profiles, RBI profiles. The API can reference these but cannot create or list them. | `--probe` |
| Features | RBI and Site Web Proxy (verified with `--probe`), real Static Range Translation, LAN firewall rules that need Layer 7 on a site, the 169.254.169.254 range (also via `allowedHostIP` and `excludeTraffic` in Terminal Server), rules that match the Sanctioned Apps category (its contents are an account setting nobody copies), Wi-Fi passwords, webhook endpoints, IPSec pre-shared keys. | export + `--probe` |
| Custom applications | Custom applications the Terraform provider refuses as exported: no criteria at all, or a criterion that is only "any protocol" (no port, range or destination). The API and the CMA accept both, so accounts have them. They're left out of `<config-dir>_target` so 0-dependencies doesn't fail on them, and the rules that use one wait until it exists in the target (created by hand, or fixed in the source and exported again). The migration never edits a criterion. | export + `customAppData.customApplicationList` |
| Integrity | Rules that point at classic groups (invisible to the groups API) and are not yet in `groups.json`, or at sites, ranges, apps or IPs that no longer exist in the source. Rules that name network interfaces, floating subnets or value sets, which the check cannot look up and the migration does not create (a WARN to check by hand). | export + `groups.groupList` |
| Target | Existing rules whose names clash with source rules (factory rules, or a previous run), existing sites, groups and allocated IPs, policy on/off differences. | `policy.<family>`, `entityLookup` |

### Coverage beyond the reference tenant

The checks were built against one reference tenant, so they were then held up
against the API itself: every `*RefInput` field in the 20 policy `addRule`
inputs of the live schema (2026-11-24). Each kind of object a rule can name is
now either checked above, built in to every account (catalog applications and
categories, countries, services, system groups, App Control activities and
content types, the Sanctioned Apps category itself), created by the migration
from the export (FQDN and IP range containers, mailing lists, subscription
groups, webhooks, private apps), or flagged for a person to check
(`int_unchecked`). Two fields name a known object under another key
(`devicePostureProfile`, and `allowedHostIP`/`excludeTraffic` for global IP
ranges); the check and `scope_policies_to_target.py` share an alias table for
them. When the API grows new rule fields, rerun that comparison.

### What the probes do

Some dependencies have no read API. The only way to know whether a target has a
custom category called "Pen Testing" is to try to use it. With `--probe` the
check opens one named draft revision (`preflight-probe`) on each of Internet
Firewall, WAN Network, App Control and Site Web Proxy. It adds a disabled test
rule per object, reads the answer, and discards the revision in a `finally`
block. Nothing is published, so the live policy and any other admin's draft
are untouched. The discard result is printed, and the revision ids are listed
at the end of the report.

The answers it relies on:

| API answer | Read as |
|---|---|
| `ruleHasInvalidEntity` naming the object | the object is missing |
| `SUCCESS`, or a validation error about something else | the object exists |
| `Can't use RBI action on a rule when RBI is disabled` | RBI is off |
| `Unsupported field [addRule]` on `siteWebProxy` | Site Web Proxy rules cannot be created on this account (needs Cato) |

## What it cannot see

The public API exposes no IdP or SCIM configuration, no account-level Static
Range Translation switch and no per-site LAN Layer 7 switch. For those the check
reports what the source needs (an ACTION) instead of what the target has. Trial
accounts return no licence data, so licence capacity on a trial is likewise an
ACTION to confirm.

### What the API can and cannot do here

Every "the API cannot" in this tool was checked against the
[Cato API reference](https://api.catonetworks.com/documentation/) and the live
schema on 2026-11-24:

| Object or setting | Read | Create or set | Notes |
|---|---|---|---|
| User groups | `user.usersGroupList` | no | No mutation creates, edits or adds members to a user group. `groups.createGroup` creates Advanced Groups, a different object. A SCIM push (`scripts/scim_sync.py`) can create groups, but they are then SCIM groups. |
| Users | `user.userList` | `user.createUser` (Beta) | Creates a MANUAL user with name, email, department, job title and phone. No group membership. |
| Dynamic user groups | `user.dynamicUserGroupsConfiguration` | `user.updateDynamicUserGroupsConfiguration` (Beta) | Turns the department attribute on or off. |
| Custom categories, device posture profiles, bandwidth priorities, user notification templates, DLP content and EDM profiles, RBI profiles | no | no | Only `*Ref` types inside rules. The `posture` API is SaaS Security Posture and `notificationSubscription` is mailing lists; neither is one of these. |
| Static Range Translation | per range (`translatedSubnet`) | per range | The account-level switch that allows translation is not in the API, and the API refuses a translated subnet while it is off. |
| LAN Layer 7 enforcement | no | no | Per-site setting, CMA only. |
| IdP and SCIM app configuration | no | no | Only the users and groups it provisions are visible (`userImportType: SCIM`). |

## Exporting the source

`scripts/export_source.py --profile <source>` runs the whole source export:
`catocli export all --strip-ids --no-account-suffix`, the static hosts on every
site type, and the webhooks. catocli 3.3.0 abandons `export all` at the first
type its key is refused ("Access denied", for example a role without the
"Socket Bypass" permission); this script records that type, runs `export all`
again for the types it skipped, and carries on. What was refused or failed goes
into `config_data/export_report.json`, and the check reports it as
`export_refused` (with the permission each type needs) or `export_failed`.
Refusals the check also sees by reading the policy live are listed once.

Some exports leave a site's native range details (local IP, DHCP and the
rest) on its LAN interface, with only the subnet on the site itself; this was
seen on sites with no WAN interfaces. The site modules read the local IP from
the site, so those sites failed to plan. `export_source.py` restates them in
the usual shape after every export: the details on the site's `native_range`,
and that interface as the default LAN. Nothing is added or dropped.
`--normalize-only` does the same to an export you already have, with no API
calls.

It shows its progress as it goes, so a long export never looks stalled. Its
own lines are stamped with the time since it started (`[export_source 1:05]`)
and say which of the three steps is running. catocli's line for each type
appears as that type starts and finishes: catocli holds its output back when
it writes into a pipe, so the script runs it with `PYTHONUNBUFFERED=1`. After
15 quiet seconds (`--heartbeat SECONDS` changes this) it prints which type is
still exporting and how many are done.

## Leaving out what the target can't hold yet

Every run also writes three files next to the report, built from whatever is
still missing on that run:

| File | What it is | What to do with it |
|---|---|---|
| `scope_command.sh` | A ready-made `scope_policies_to_target.py` command that writes `<config-dir>_target`: the export minus every rule that waits on a BLOCKER, plus the LAN firewall rules that need Layer 7, and minus the custom applications Terraform can't create (`--drop-custom-app`). With nothing missing it writes an identical copy. | `sh preflight/scope_command.sh`, then deploy with `--config-dir config_data_target` |
| `exclude-0-dependencies.auto.tfvars` | `global_ip_ranges_exclude` and `allocated_ips_exclude` (ranges the API firewall blocks, allocations on PoPs the target lacks) | Copy into `0-dependencies/` |
| `exclude-1-sites.auto.tfvars` | `ipsec_sites_exclude` (IPSec sites on those PoPs) | Copy into `1-sites/` |

The idea is simple: fix what you can, run the check again, and whatever is
still missing gets left out by these files. Rerun the check after any change
and they are rewritten to match. Missing CMA-only objects are only left out
once a `--probe` run has proved them missing; without `--probe` they are an
ACTION, not something to leave out.

Ranges holding 169.254.169.254 need care with timing, because the
`cato_global_ip_ranges` resource behaves differently before and after its first
deploy. The first deploy sends every range it is given in one request, and the
API blocks this address. After that it owns every range in the account, so a
range that exists but is excluded gets planned for deletion. So the check
keeps such a range excluded until it exists in the target **and**
`0-dependencies` has been deployed (it looks for the resource in
`0-dependencies/terraform.tfstate`). After that it drops it from the exclusion
file and reports it as OK. A range made by hand before the first deploy stays
excluded for that deploy, with an ACTION to rerun the check straight after.

LAN Layer 7 enforcement is a per-site switch the API cannot read, so the check
always leaves out the LAN firewall rules that need it, until you tell it
otherwise. Once the switch is on for the sites it names (the sites must exist
first), run it with `--lan-l7-confirmed` and those rules stay in.

Wi-Fi passwords are never written to these files on purpose. `1-sites`
accepts any value you give it, so a placeholder would go live as the real
password. Put the real ones in `wifi_ssid_passkeys` yourself.

Tested against a fresh trial target: the generated command left out exactly the 100
rules the check flags (98 behind BLOCKERs plus the 2 Layer 7 rules), and the
two exclusion files matched the ones written by hand for the first run.

## Example: a reference tenant into a fresh trial tenant

With `--probe`, the run found 12 blockers, 8 actions and 3 warnings, and 98 of
181 rules (174 plus 7 nested LAN firewall rules) that will fail until the
blockers are fixed:

- 2 China PoPs not available to the target (an entitlement the trial lacks).
- 10 manual and 11 SCIM user groups and users missing, behind 94 rules.
- 2 custom categories, 6 device posture profiles, 3 bandwidth priorities, 1 user
  notification template, 3 DLP content profiles and 2 EDM profiles missing
  (6 of the 9 content profiles are built in and were found).
- RBI disabled, Site Web Proxy unavailable, and one global range of 169.254.169.254/32.
- Actions: 19 public IPs, 34 ZTNA users and 15 sites to confirm on a trial, 2 LAN
  firewall rules needing Layer 7 on one branch site, 2 Wi-Fi passwords, and 26
  factory rules sharing names with source rules.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline: reference collection, probe interpretation, every check
on synthetic data, and the SCIM endpoint check against a local stand-in server.
