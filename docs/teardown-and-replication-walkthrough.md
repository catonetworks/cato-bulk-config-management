# Teardown & replication walkthrough

A concrete operator runbook for two jobs:

- **Part A** — purge every custom object out of a target tenant (reset it to empty).
- **Part B** — replicate the <source-profile> configuration (account <SOURCE_ACCOUNT_ID>, source of truth)
  into another tenant, rehearsed on **<test-profile>** (account <TEST_ACCOUNT_ID>) before going anywhere
  near **<target-profile>** (account <TARGET_ACCOUNT_ID>).

This document is the concrete, copy-pasteable walk of the process skeleton in
`docs/bulk-apply-runbook.md` — it does not contradict that doc, it fills in every
command and every prompt the operator actually sees. Read that doc too.

Three steps in this whole flow are destructive and gated behind an interactive typed
account-id confirmation. They are marked **DESTRUCTIVE GATE** throughout:

1. `scripts/purge_all.py` (or any `scripts/purge/purge_<type>.py`) with `--execute`
2. `scripts/bulk_apply.py --mode apply`
3. `scripts/scim_sync.py push --execute` (and `scim_sync.py purge --execute`)

Every other command below is read-only (exports, dry-runs, plans, lint).

---

## 1. One-page overview

```
                    ┌─────────────────────────────────────────────┐
                    │   <source-profile> (account <SOURCE_ACCOUNT_ID>) — SOURCE OF TRUTH   │
                    │   NEVER purged. NEVER apply'd without         │
                    │   --source-tenant-apply. Hard-blocked by name │
                    │   ("<source-profile>", "<source-profile-readonly>") AND by      │
                    │   account id (<SOURCE_ACCOUNT_ID>) in every guarded script. │
                    └───────────────────┬───────────────────────────┘
                                         │ export (read-only)
                                         ▼
                    config_data/*.json  +  config_data/scim_*.csv
                                         │
                 ┌───────────────────────┼────────────────────────┐
                 │                       │                        │
                 ▼                       ▼                        ▼
         Part A: purge            Part B: bulk_apply        Part B: scim_sync push
         (any non-source           --mode plan then          --execute
          target tenant)           --mode apply
                 │                       │                        │
                 ▼                       ▼                        ▼
         <test-profile> (<TEST_ACCOUNT_ID>)  ──rehearse first──>  <target-profile> (<TARGET_ACCOUNT_ID>)
         sanctioned test target                 ultimate target
```

**Ordering note**: despite the diagram showing bulk_apply and SCIM push as parallel
branches, `scim_sync.py push --execute` must complete **before** `bulk_apply.py`'s
`--mode apply` run reaches any `4-policies/*` folder — 11 policy families resolve
`usersGroup`/`user` names at apply time. See Part B below; `bulk_apply.py` prints a
one-line reminder of this gate but cannot enforce or verify it itself.

| Stage | Script | Mutates | Source-tenant guard |
|---|---|---|---|
| Profile check | `scripts/cato_profile.py <name>` | nothing | n/a (read-only) |
| Purge dry-run / execute | `scripts/purge_all.py`, `scripts/purge/purge_<type>.py` | target only | refuses profile name `<source-profile>`/`<source-profile-readonly>` or account <SOURCE_ACCOUNT_ID>, no override |
| SCIM export | `scripts/scim_sync.py export --source-profile <source-profile>` | nothing (reads <source-profile> only) | n/a |
| SCIM push / purge | `scripts/scim_sync.py push\|purge` | target only | same refusal, no override |
| Plan / apply | `scripts/bulk_apply.py` | target only on `--mode apply` | source apply needs `--source-tenant-apply`; non-source apply refuses if any selected folder has `migration.tf` |
| Tenant-to-tenant export | `catocli export all --strip-ids --no-account-suffix --output-directory <dir>` | nothing (read-only) | n/a |

Profiles used in this doc (from `~/.cato/credentials`, resolved via
`scripts/cato_profile.py`):

| Profile | Account | Role |
|---|---|---|
| `<source-profile>` | <SOURCE_ACCOUNT_ID> | source of truth — read-only in every workflow here |
| `<test-profile>` | <TEST_ACCOUNT_ID> | sanctioned rehearsal/test target (what the user calls "dev1"; no profile literally named `dev1` exists) |
| `<target-profile>` | <TARGET_ACCOUNT_ID> | ultimate replication target |

---

## Part A — Teardown / purge a target tenant

Applies to any non-source tenant (<test-profile>, <target-profile>, or any future target). The
engine (`scripts/purge/engine.py`) always enumerates live objects from the TARGET via
`catocli export <type>`, then deletes via catocli mutations, in this fixed order
(`scripts/purge/families.py`):

```
internet_firewall, wan_firewall, wan_network, tls_inspection, lan_firewall,
application_control, remote_port_forward, socket_bypass, split_tunnel,
terminal_server, always_on, client_connectivity, dynamic_ip_allocation,
private_access, app_tenant_restriction, anti_malware_file_hash,
sdp_static_ip_allocation, site_web_proxy        (18 policy families)
  → custom_applications → groups → global_ip_ranges → dhcp_relay_groups
```

Policy families go first (children of rules/sections before dependencies), then
`custom_applications` → `groups` → `global_ip_ranges` → `dhcp_relay_groups`, because
rules reference apps/groups/ranges and relay groups reference nothing. **Sites,
network ranges, and static hosts are never touched by this engine** — they're
Terraform-owned via `1-sites/`; destroy them with Terraform if that's ever needed.
SCIM users/groups are excluded unless you pass `--include-scim` (separate blast
radius, runs last, shells `scim_sync.py purge` under the same profile).

### A1. Profile sanity check

```bash
python3 scripts/cato_profile.py <test-profile>
```

Confirms the profile resolves to the account you think it does, and whether SCIM
creds are configured, before touching anything:

```
profile:    <test-profile>
account_id: <TEST_ACCOUNT_ID>
endpoint:   <default>
scim:       configured
```

If this doesn't match the tenant you intend to purge, stop — every downstream script
trusts this resolution, not your shell's `CATO_PROFILE`/`TF_VAR_*`.

### A2. Full dry-run inventory

```bash
python3 scripts/purge_all.py --profile <test-profile>
```

(`--dry-run` is the default; no flag needed.) This is one `catocli export` per type —
read-only. It prints, per type:

```
target: profile '<test-profile>' -> account <TEST_ACCOUNT_ID>
types (22, safe order): internet_firewall, wan_firewall, ...

--- internet_firewall ---
[dry-run] internet_firewall: 7 object(s)
  would delete rule:          Allow-Outbound-HTTPS
  would delete section:       Inbound
  would SKIP section:         Catch All (protected)
...
--- groups ---
[dry-run] groups: 3 object(s)
  would delete group: Engineering
```

Sections matching `catch all` / `catch-all` / `default` (case-insensitive, by name
only — exports carry no "system" flag) are always marked `would SKIP ... (protected)`
in dry-run and are never deleted even under `--execute`.

To scope the inventory to a subset while testing the engine itself:

```bash
python3 scripts/purge_all.py --profile <test-profile> --only groups,internet_firewall
python3 scripts/purge_all.py --profile <test-profile> --skip dhcp_relay_groups
```

Or per-type, using the thin wrappers directly (same engine, one type):

```bash
python3 scripts/purge/purge_groups.py --profile <test-profile>
```

### A3. Review

Read the dry-run output end to end. Anything you don't expect to see deleted (wrong
tenant, unexpected object count) — stop here, don't proceed to `--execute`.

### A4. DESTRUCTIVE GATE — execute the purge

```bash
python3 scripts/purge_all.py --profile <test-profile> --execute
```

What the operator sees, in order:

1. The same profile-resolution + type-list log lines as the dry-run.
2. An interactive prompt (requires a real TTY — refused under CI/non-interactive
   shells):
   ```
   Type the target account id (<TEST_ACCOUNT_ID>) to confirm purge:
   ```
   Typing anything other than the exact resolved account id aborts with exit code 2
   and deletes nothing.
3. Per-type progress as each family/dependency is actually deleted, e.g.:
   ```
   --- internet_firewall ---
   internet_firewall             deleted=6    skipped=1   failed=0
   ```
   (`skipped` = protected sections by name; `failed` = API refused — logged with the
   trailing API error text.)
4. A final combined summary block (one `summary_line()` per type) once every type has
   run.
5. Exit code: `0` clean, `1` if anything failed, `2` on guard refusal.

To purge one type only, interactively:

```bash
python3 scripts/purge/purge_internet_firewall.py --profile <test-profile> --execute
```

To also wipe SCIM users/groups in the same pass (runs after all 22 Terraform-adjacent
types, same `--execute`/account-id prompt mechanics, shelling `scim_sync.py purge`):

```bash
python3 scripts/purge_all.py --profile <test-profile> --execute --include-scim
```

Capture a machine-readable report of exactly what happened (deleted/skipped/failed
per type) for later review:

```bash
python3 scripts/purge_all.py --profile <test-profile> --execute --json-report /tmp/<test-profile>-purge.json
```

### A5. Post-purge verification

Re-run the dry-run — expect ~0 objects everywhere except the protected catch-all/
default sections (which never show as deletable, they're filtered before the count):

```bash
python3 scripts/purge_all.py --profile <test-profile>
```

```
[dry-run] internet_firewall: 1 object(s)
  would SKIP section:         Catch All (protected)
```

A nonzero count outside of a protected section means something survived — check the
`--json-report` or re-run output for `failed` entries (the API refused the delete;
the error text is included and is the expected signal for true system objects the
API won't let you remove).

### A6. Failure handling

- **`--keep-going`** — without it, `purge_all.py` stops at the first type that
  reports any `failed` entry. With it, it continues through the remaining types and
  still exits `1` if anything failed anywhere, so you get a complete picture in one
  pass:
  ```bash
  python3 scripts/purge_all.py --profile <test-profile> --execute --keep-going --json-report /tmp/<test-profile>-purge.json
  ```
- **Re-run idempotency** — the engine always re-enumerates from a fresh
  `catocli export` before deleting, so re-running `purge_all.py --execute` after a
  partial failure is safe: already-deleted objects simply won't appear in the next
  enumeration, and only what's still live gets attempted again.
- **`--json-report`** — writes `[{"type", "deleted": [...], "skipped": [...],
  "failed": [...]}]` per type; use it to diff "what did this run actually do" without
  re-reading terminal scrollback.

---

## Part B — Replicate <source-profile> into <test-profile> (or any non-source account)

Rehearsal discipline: **<test-profile> first, <target-profile> second, <source-profile> never.** Full
rehearsal = purge <test-profile> → bulk apply → SCIM push → verify → only then repeat on
<target-profile>. The steps below use `<test-profile>` as `<target>`; substitute `<target-profile>`
only after <test-profile> has verified clean.

### B1. Refresh exports from <source-profile>

```bash
CATO_PROFILE=<source-profile> catocli export all --no-account-suffix --archive-directory config_data_archive
```

(Requires the catocli release that ships `export all` natively — unreleased as of
2026-11-06; supersedes the removed `scripts/export_all_configs.py`.) Writes every
policy/dependency/site type into `config_data_archive/` under the same
filenames `config_data/` uses (e.g. `all_ifw_rules_and_sections.json`,
`groups.json`, `socket_sites.json`). Review the diff before promoting:

```bash
diff config_data/all_ifw_rules_and_sections.json config_data_archive/all_ifw_rules_and_sections.json
# repeat per file, or diff the whole directories
```

Copy any accepted changes from `config_data_archive/` into `config_data/` by hand — this
script never writes `config_data/` itself (see `config_data_archive/README.md`; a human
reviews before promoting).

### B2. Lint the dataset

```bash
python3 scripts/validate_config_data.py
```

Checks shape, unique names, `rules_in_sections` resolution, cross-file name
references, and import-id presence across everything under `config_data/`. Fix any
ERROR-severity finding before continuing — `bulk_apply.py` runs this same gate itself
and aborts on failure unless you pass `--skip-validate`.

### B3. Refresh the SCIM dataset

```bash
python3 scripts/scim_sync.py export --source-profile <source-profile>
```

Read-only against <source-profile> only. Writes the normalized, id-blanked CSVs into
`config_data/scim_users.csv` and `scim_groups.csv` (ids blanked so a push
into an empty tenant CREATEs rather than UPDATEs; `external_id` is the cross-tenant
join key — never hand-edit it). Review the git diff (rows are sorted for stable
diffs) before pushing.

### B4. Prepare a replication workspace

The folders' local `terraform.tfstate*` belongs to <source-profile>. Applying against <test-profile>
from the same checkout would mix <source-profile> state with <test-profile> state. Pick one:

**Option 1 — fresh clone (recommended):**

```bash
git clone /Users/Shared/github.com/cato-networks-fcto/reference-architecture-cma-configs /tmp/<source-profile>-replication-<test-profile>
cd /tmp/<source-profile>-replication-<test-profile>
```

A fresh clone starts stateless on purpose — no `terraform.tfstate` exists yet in any
folder, so every `terraform plan` against <test-profile> is pure creates.

**Option 2 — move state aside in place** (if you must stay in the working repo):

```bash
for d in 0-dependencies 1-sites 3-groups 4-policies/*; do
  [ -f "$d/terraform.tfstate" ] && mv "$d/terraform.tfstate" "$d/terraform.tfstate.<source-profile>.bak"
  [ -f "$d/terraform.tfstate.backup" ] && mv "$d/terraform.tfstate.backup" "$d/terraform.tfstate.backup.<source-profile>.bak"
done
```

**Either option — park `migration.tf` before any plan or apply.** Its `import` blocks
carry <source-profile> object ids, and import blocks evaluate at **plan** time — against another
tenant they fail with walls of "Cannot import non-existent remote object" errors.
`bulk_apply.py` now hard-stops **both plan and apply modes** on any selected folder
containing `migration.tf` when the target account isn't <SOURCE_ACCOUNT_ID>, and ships a helper that
does the parking for you:

```bash
python3 scripts/bulk_apply.py --profile <test-profile> --park-migrations
```

This renames each selected folder's `migration.tf` to `migration.tf.parked` (reverse
with `--unpark-migrations`). Parking is refused when the profile resolves to the
source tenant, where `migration.tf` is intentional re-adoption config. The guard
also prints a warning (not a refusal) if `terraform.tfstate` files are present for a
target-tenant run — a replication tree should be stateless (see Option 1/2 above).

### B5. Plan everything

```bash
python3 scripts/bulk_apply.py --profile <test-profile> --mode plan
```

Runs `terraform init -upgrade` / `validate` / `plan -out=.bulk_plans/<folder>.tfplan`
across all 22 folders in fixed order (`0-dependencies` → `1-sites` → `2-app-connectors` → `3-groups` → every
`4-policies/*` folder alphabetically — `3-groups` sits after `1-sites` because its
members are site-scoped), `-parallelism=1`, under a generated clean
`TF_CLI_CONFIG_FILE` (`.bulk_plans/clean.tfrc`, registry `direct {}` only — forces the
pinned registry provider version, not a local dev build). Sets
`DISABLE_POLICY_RULE_CLEANUP=true` so the provider's draft-cleanup mutation can't fire
during a plan. Stops at the first folder that fails `init`/`validate`/`plan`.

Also prints the **webhook manual-gate warning** here (and again in apply mode) if
`config_data/subscription_groups.json` references any webhook/integration — see B6
below for what to do with it.

Useful scoping flags:

```bash
# re-plan just one folder after fixing something
python3 scripts/bulk_apply.py --profile <test-profile> --mode plan --folders 4-policies/internet-firewall

# resume a stopped run partway through the sequence
python3 scripts/bulk_apply.py --profile <test-profile> --mode plan --start-at 4-policies/lan-firewall

# preview what a purge of <test-profile> would do first (prints inventory only, never deletes)
python3 scripts/bulk_apply.py --profile <test-profile> --mode plan --purge-first

# skip the validate_config_data.py gate (not recommended)
python3 scripts/bulk_apply.py --profile <test-profile> --mode plan --skip-validate

# use your ~/.terraformrc as-is instead of the generated clean config
python3 scripts/bulk_apply.py --profile <test-profile> --mode plan --allow-dev-overrides
```

### B6. Review every plan

Read the summary table printed at the end:

```
================================================================
folder                       status             add  chg  del  imp
------------------------------------------------------------------
0-dependencies               ok                  26    0    0    0
1-sites                      ok                  88    0    0    0
4-policies/always-on         ok                   4    0    0    0
...
```

**What clean looks like against a purged, greenfield target: pure adds — `N to add,
0 change, 0 destroy, 0 import` in every folder.** Any `change` or `destroy` on a
greenfield target means the JSON in `config_data/` is stale or the target wasn't
fully purged — re-export/re-purge, don't let Terraform apply a diff you don't
understand. (Contrast with planning against <source-profile> itself, where state-owning
folders should read `No changes.` and `0-dependencies`/`1-sites` show
`N to import, 0/0/0` until their `migration.tf` is applied — that's the brownfield
case, not what you're doing here.) Also inspect the saved plans directly if you want
full resource-level detail:

```bash
terraform -chdir=4-policies/internet-firewall show .bulk_plans/3-policies__internet-firewall.tfplan
```

**Webhook manual gate**: if B5 printed a `WARNING (webhook manual gate): ...` line, create
the named webhooks/integrations in the target CMA now, before B8 (apply) reaches
`0-dependencies`'s `cato_subscription_group.this` — webhooks have no Terraform provider
resource, so `bulk_apply.py` can only warn, not create or verify them. This account's
subscription groups reference `ServiceNow - Create`, `ServiceNow - Update`, and `Slack
SOC IR - Create`.

### B7. Push SCIM users/groups — DESTRUCTIVE GATE, before apply

```bash
# dry-run first (local schema + membership checks only, no API calls)
python3 scripts/scim_sync.py push --target-profile <test-profile>

# real push
python3 scripts/scim_sync.py push --target-profile <test-profile> --execute
```

Same account-id confirmation prompt pattern as purge/apply. Imports users first, then
groups (groups reference members by `external_id`, which must already exist as a
user). `push` and `purge` both refuse the source tenant outright (profile name
`<source-profile>`/`<source-profile-readonly>`, or account <SOURCE_ACCOUNT_ID>) — no override flag exists.

**This step now runs before B8, not after** (moved 2026-11-06): 11 policy families
(`ao`, `apc`, `atr`, `cc`, `dia`, `ifw`, `pa`, `st`, `tls`, `wf`, `wnw`) resolve
`usersGroup`/`user` names at apply time, so the SCIM push must have landed before any
`4-policies/*` folder applies. `bulk_apply.py` prints a one-line reminder of this gate
right before the first `4-policies/*` folder in an apply run, but — since all 22 folders
apply in one invocation — the reminder is advisory, not an enforced pause; doing this
step before B8 is what actually satisfies it.

### B8. DESTRUCTIVE GATE — apply

```bash
python3 scripts/bulk_apply.py --profile <test-profile> --mode apply
```

Guards the operator will hit, in order:

1. **Greenfield guard** — refuses immediately if any selected folder still contains
   `migration.tf` (see B4): `REFUSED (greenfield guard): these folders contain
   migration.tf, whose import blocks carry SOURCE-tenant ids and cannot run against a
   target tenant: ...`. Exit 2.
2. **Interactive confirmation** (TTY required, no CI path):
   ```
   Type the target account id (<TEST_ACCOUNT_ID>) to confirm:
   ```
   Wrong input or no TTY → exit 2, nothing applied.
3. **Per-folder plan-file guards**, checked before each `terraform apply`:
   - `REFUSED: no saved plan for <folder> -- run --mode plan first.` if B5 was
     skipped or the folder wasn't included.
   - `REFUSED: <folder>'s plan file is older than its inputs -- re-plan first.` if any
     `.tf`/`.tfvars` file in the folder or any `config_data/*.json` file changed since
     the plan was saved (stale-plan guard).
   - `REFUSED: <folder>'s plan was created for account <X>, but --profile '<test-profile>'
     resolves to <Y>.` if the saved plan's `.meta` sidecar (written at plan time)
     doesn't match the account you're applying as — protects against replaying a
     plan captured under a different profile.
4. Applies each folder's **saved plan file only** (`terraform apply <plan>.tfplan`),
   one folder at a time, stopping on the first failure. On stop, fix the issue and
   resume with `--start-at <folder>` (after re-planning that folder if its inputs
   changed).
5. Final summary table, same shape as the plan-mode one, with `applied` in the status
   column for folders that succeeded.

Applying to <source-profile> itself is a separate, deliberately harder path — requires
`--source-tenant-apply` in addition to everything above — and is out of scope for
this replication walkthrough; <source-profile> is read-only source of truth here.

### B9. Post-apply verification

```bash
CATO_PROFILE=<test-profile> catocli export all --no-account-suffix --archive-directory config_data_archive
diff -r config_data/ config_data_archive/   # or per-file, as in B1
```

Expected residual diffs on a fresh target: object ids (target-assigned, not
comparable across tenants), any system default rules the purge skipped, and the
known export omissions documented in `1-sites/README.md` (fields the API read path
doesn't currently return at all, e.g. `lag_min_links`, `dhcp_type`, `ip_range`,
`relay_group_id`/`relay_group_name`, `gateway`, `translated_subnet`,
`internet_only`, `local_ip`, `range_type` — these must be hand-filled in the JSON
before `1-sites/`'s first apply, independent of which tenant is the target). Anything
else is a real replication gap — fix the JSON or the module, re-plan, re-apply.

Also verify SCIM counts:

```bash
CATO_PROFILE=<test-profile> catocli scim get_users -p
CATO_PROFILE=<test-profile> catocli scim get_groups -p
wc -l config_data/scim_*.csv
```

**Rule hygiene (<source-profile>, not the target)**: `scripts/validate_config_data.py` flags two
stale rule references in the source data itself — `"Remote Users (deleted)"` (WF) and
`"Quarantine Users Group (deleted)"` (WF) — against deleted SCIM groups. These aren't
replication defects; they're existing <source-profile> rules worth cleaning up in CMA when
convenient, for the operator.

### B10. Greenfield manual-resolution checklist (cannot be automated)

These are the items the `CHECKLIST.md` generated by `catocli export all --strip-ids`
calls out by name, and the same list `docs/bulk-apply-runbook.md` cross-references —
none of them can be scripted away. See also `docs/bulk-apply-runbook.md`'s
**Unmanaged referents** list (customCategory, DLP content/EDM profiles + userNotification,
`wnw` `allocatedIp`/`popLocation`, webhooks, built-in catalog objects) for items that
aren't owned by this repo at all, so no plan will ever surface them as a gap:

- **Group members with id-only types.** `FLOATING_SUBNET`, `HOST`,
  `NETWORK_INTERFACE`, `SITE_NETWORK_SUBNET` group members are only addressable by id
  in the source tenant; name-based resolution doesn't exist for these types yet.
  Re-resolve each one's id on the target via `catocli entity entityLookup` before
  `0-dependencies/` applies cleanly.
- **Mailing-list admin ids.** The provider's mailing-list admin reference requires an
  id; re-resolve each admin's id on the target the same way.
- **Wi-Fi PSKs.** The API never returns Wi-Fi passkeys on export. Supply each one via
  the `wifi_ssid_passkeys` Terraform variable in `1-sites/`, keyed by
  `"<site name>:<ssid name>"` (e.g. `"Branch 1:Guest Wi-Fi"`).
- **Licenses.** `license_id`/`license_bw` are source-tenant-specific; re-assign a
  license to each site on the target as part of onboarding, before or during
  `1-sites/`'s apply.
- **Physical socket re-registration.** Terraform/JSON replication does not move
  hardware — every physical socket behind a replicated site must be physically
  re-registered against the target account with a new registration code.

If you're doing a full tenant-to-tenant replication (not just re-running this repo's
`config_data/` against a second account) rather than a same-repo rehearsal, generate
this checklist directly against your source profile:

```bash
catocli export all --strip-ids --no-account-suffix --output-directory /tmp/<source-profile>-migration
cat /tmp/<source-profile>-migration/CHECKLIST.md
```

(Requires the catocli release that ships `export all --strip-ids`, unreleased as of
2026-11-06; supersedes the removed `scripts/migrate_tenant.py`.) This is read-only —
it never applies or imports anything anywhere.

---

## 4. Generalizing to another account

Nothing in this flow is hardcoded to <test-profile>/<target-profile> except the documented
blocklist that protects the *source* tenant (`<source-profile>`/`<source-profile-readonly>` by profile
name, or account <SOURCE_ACCOUNT_ID> by resolved id — checked in `scripts/purge/engine.py`,
`scripts/scim_sync.py`, and `scripts/bulk_apply.py` independently, each with no
override flag). To point any of this at a brand-new target account:

1. Configure a catocli profile for it:
   ```bash
   catocli configure set --profile <newprofile> --cato-token <token> --account-id <id>
   ```
2. If you'll use SCIM push/purge, also configure SCIM creds for that profile:
   ```bash
   catocli configure set --profile <newprofile> --scim-url <url> --scim-token <token>
   ```
3. Confirm the resolution:
   ```bash
   python3 scripts/cato_profile.py <newprofile>
   ```
4. Substitute `<newprofile>` for `<test-profile>`/`<target-profile>` everywhere in Part A and
   Part B above. Every script re-resolves credentials from the named profile each
   time it runs — there's no cached state tying any script to a specific account
   other than the `<source-profile>`/<SOURCE_ACCOUNT_ID> blocklist.

The only account that can never be a `<target>` in this doc is <SOURCE_ACCOUNT_ID> (<source-profile>) — every
guarded script refuses it unconditionally (purge: outright; bulk_apply apply mode:
only with the explicit, separate `--source-tenant-apply` escape hatch).

---

## 5. Quick-reference command appendix

```bash
# -- profile check --------------------------------------------------------
python3 scripts/cato_profile.py <profile>

# -- Part A: purge ---------------------------------------------------------
python3 scripts/purge_all.py --profile <target>                                   # dry-run, all types
python3 scripts/purge_all.py --profile <target> --only groups,internet_firewall    # scoped dry-run
python3 scripts/purge_all.py --profile <target> --execute                          # DESTRUCTIVE
python3 scripts/purge_all.py --profile <target> --execute --keep-going --json-report report.json
python3 scripts/purge_all.py --profile <target> --execute --include-scim           # + SCIM users/groups
python3 scripts/purge/purge_<type>.py --profile <target> [--execute]               # single type

# -- Part B: replicate ------------------------------------------------------
CATO_PROFILE=<source-profile> catocli export all --no-account-suffix --archive-directory config_data_archive  # refresh exports
diff -r config_data/ config_data_archive/                                             # review
python3 scripts/validate_config_data.py                                            # lint

python3 scripts/scim_sync.py export --source-profile <source-profile>                       # refresh SCIM dataset

# workspace prep (fresh clone OR move state aside + park migration.tf)
git clone <this-repo> /tmp/replication-<target>
find . -maxdepth 2 -name migration.tf -exec bash -c \
  'mv "$1" .migration-parked/"$(basename "$(dirname "$1")")".migration.tf' _ {} \;

python3 scripts/bulk_apply.py --profile <target> --mode plan                       # plan all
python3 scripts/bulk_apply.py --profile <target> --mode plan --folders <folder>    # scoped plan
python3 scripts/bulk_apply.py --profile <target> --mode apply                      # DESTRUCTIVE

python3 scripts/scim_sync.py push --target-profile <target>                        # SCIM dry-run
python3 scripts/scim_sync.py push --target-profile <target> --execute              # DESTRUCTIVE

CATO_PROFILE=<target> catocli export all --no-account-suffix --archive-directory config_data_archive  # post-apply verify
diff -r config_data/ config_data_archive/

catocli export all --strip-ids --no-account-suffix --output-directory <dir>        # tenant-to-tenant checklist
```

The `catocli export all` commands above require the catocli release that ships
`export all` (`--strip-ids` for the last one); unreleased as of 2026-11-06. They
supersede the removed `scripts/export_all_configs.py` and `scripts/migrate_tenant.py`.
