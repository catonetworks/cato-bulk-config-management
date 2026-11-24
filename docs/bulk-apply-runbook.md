# Bulk-apply runbook — replicating <source-profile> into a dev tenant

The end-to-end process for replicating the full <source-profile> configuration
(account <SOURCE_ACCOUNT_ID>, the source of truth) into a target tenant — ultimately
**<target-profile>** (<TARGET_ACCOUNT_ID>), rehearsed first on **<test-profile>** (<TEST_ACCOUNT_ID>). Every
destructive step (purge `--execute`, `terraform apply`, SCIM push) is run by
a human; the scripts gate, sequence, and verify.

## The one-page sequence

| # | Step | Command | Mutates |
|---|------|---------|---------|
| 1 | Refresh exports from <source-profile> | `CATO_PROFILE=<source-profile> catocli export all --no-account-suffix --archive-directory config_data_archive` + review diff vs `config_data/` | nothing |
| 2 | Lint the dataset | `python3 scripts/validate_config_data.py` | nothing |
| 3 | Refresh SCIM dataset | `python3 scripts/scim_sync.py export --source-profile <source-profile>` | nothing |
| 4 | Purge the target (optional, first replication or reset) | `python3 scripts/purge_all.py --profile <target> --dry-run` → review → `--execute` | **target** |
| 5 | Plan everything | `python3 scripts/bulk_apply.py --profile <target> --mode plan` | nothing |
| 6 | Review every plan + the webhook manual-gate warning | read the summary table + saved plans in `.bulk_plans/`; if `bulk_apply.py` printed a webhook warning (see below), create the named webhooks/integrations in the target CMA now | **target** (CMA only, outside Terraform) |
| 7 | Push SCIM users/groups — GATE, before policies apply | `scripts/scim_sync.py push --target-profile <target> --execute` | **target** |
| 8 | Apply | `python3 scripts/bulk_apply.py --profile <target> --mode apply` | **target** |
| 9 | Verify | `CATO_PROFILE=<target> catocli export all --no-account-suffix --archive-directory config_data_archive` → diff vs `config_data/` | nothing |

Steps 4, 6 (webhook creation), 7, and 8 are the operator's. Steps 4, 7, and 8 each
demand an interactive typed confirmation of the resolved target account id.

**Step 7 moved ahead of step 8** (previously ran after apply) — 11 policy
families resolve `usersGroup`/`user` names at apply time (see § SCIM gate
below), so the push must land before `4-policies/*` applies, not after.
`bulk_apply.py` reinforces this with a one-line reminder printed in apply
mode right before the first `4-policies/*` folder runs — it's an advisory
gate, not an enforced pause, since all 22 folders apply in one invocation;
doing step 7 before step 8 is what actually satisfies it.

Steps 1 and 9's `catocli export all` requires the catocli release that ships
`export all` natively (unreleased as of 2026-11-06; supersedes the removed
`scripts/export_all_configs.py`).

## Folder order (what bulk_apply.py enforces)

`0-dependencies` → `1-sites` → `2-app-connectors` → `3-groups` → all `4-policies/*` policy folders
(alphabetical — they are independent of each other, but all reference objects owned by
the first three). `3-groups` sits between `1-sites` and `4-policies/*` because group
members in this account are site-scoped (`SITE`/`SITE_NETWORK_SUBNET`); it was split out
of `0-dependencies` on 2026-11-06 specifically so its folder-number wouldn't misstate the
real dependency order (`0-dependencies`' own objects have no such site dependency). All
terraform commands run `-parallelism=1` (API rate behavior, repo convention).
Stop-on-first-failure: a failed folder halts the run; fix, then resume with
`--start-at <folder>`.

## Webhook manual gate

Webhooks have no `cato_webhook` Terraform provider resource and no catocli export type
yet (tracked as a follow-up). `bulk_apply.py` checks `config_data/subscription_groups.json`
for non-empty `integration[]` arrays on every plan and apply against a target tenant and
prints the referenced webhook/integration names — e.g. this account's `ServiceNow -
Create`, `ServiceNow - Update`, `Slack SOC IR - Create`. This is a **warning, never a
refusal**: `bulk_apply.py` cannot create or verify webhooks itself. Create them by hand
in the target CMA before `0-dependencies`' apply reaches `cato_subscription_group.this`
(step 8 above), or the subscription-group create will fail loudly against a name that
doesn't resolve. Once `webhooks.json` exists (post catocli `export webhooks`),
`scripts/validate_config_data.py` cross-checks these names automatically (§ Validator
below).

## SCIM gate

11 policy families resolve `usersGroup`/`user` names at apply time: `ao`, `apc`, `atr`,
`cc`, `dia`, `ifw`, `pa`, `st`, `tls`, `wf`, `wnw`. `bulk_apply.py` prints a one-line
reminder in apply mode, right before the first `4-policies/*` folder in the run, but
cannot verify target SCIM state itself (no API calls from this script) — it's an
advisory gate. The actual gate is operational: run step 7 (SCIM push) before step 8
(apply), per the one-page sequence above. `scripts/validate_config_data.py` also
cross-checks rule `usersGroup`/`user` references against `config_data/scim_groups.csv`/
`scim_users.csv` when those files are present (§ Validator below) — including this
account's two known stale references to deleted SCIM groups (`"Remote Users (deleted)"`,
`"Quarantine Users Group (deleted)"`), which are rule-hygiene items for the operator to clean up
in CMA, not a replication blocker.

(2026-11-10 update, revised: SCIM push is OPT-IN. Pass `--include-scim` to have
`bulk_apply.py` run `scim_sync.py push --execute --confirmed-account <id>` FIRST, before
any folder. Without the flag SCIM is untouched and a one-line skip notice is printed —
the SCIM import is create-only, so pushing into a tenant that already has the objects
fails them all as duplicates; only pass `--include-scim` against a purged/empty target.
With the flag, a MISSING `config_data/scim_users.csv` is a hard gate failure, not a
silent skip.)

## Allocated-IP remap gate (2026-11-10)

Allocated-IP display names embed the Cato-ASSIGNED ip (`"<pop> - <ip>"`), so a source
tenant's `allocationIp` rule references can never resolve in a target — the target's
allocations get new IPs. The cross-tenant identity of an allocation is
`(pop_location_name, description)` (the validator enforces that pair's uniqueness in
`allocated_ips.json`). In apply/deploy mode against a target, `bulk_apply.py`:

1. applies `0-dependencies` (which creates the target's allocations via the new
   `cato_allocated_ip` resource, state-keyed by the SOURCE display name);
2. right before the first allocationIp-referencing folder (`4-policies/wan-network`,
   the only family with such refs today — see `ALLOCATED_IP_REMAP` in the script),
   runs `scripts/remap_allocated_ips.py`, which reads `0-dependencies`' `allocated_ip_map`
   output (source name → target name) and stages rewritten policy JSONs under the
   gitignored `config_data/.remapped/`;
3. points the folder at the staged copy via its `TF_VAR_wnw_rules_json_file_path`
   override. The committed exports are never mutated. An unmatched ref is a hard error
   (a mis-scoped security rule is worse than a failed deploy).

## Validator (`scripts/validate_config_data.py`)

Run automatically as a gate inside `bulk_apply.py` (`--skip-validate` opts out). Beyond
the structural checks (shape, unique names, `rules_in_sections` resolution,
cross-file name integrity, import-id presence):

- `webhooks.json` is optional-until-present (same treatment `dhcp_relay_groups.json` got
  before it existed); once present, `subscription_groups.json`'s `integration[]` names
  are cross-checked against it.
- When `config_data/scim_users.csv`/`scim_groups.csv` exist, policy rule
  `usersGroup`/`user` name references are cross-checked against them (warn on a miss);
  when absent, a single INFO line records the skip rather than saying nothing.

## Credentials and provider resolution

- `--profile <name>` names a catocli profile; `scripts/cato_profile.py`
  resolves it and injects `TF_VAR_cato_token_<source-profile>_dev` /
  `TF_VAR_account_id_<source-profile>_dev` into the terraform subprocess env,
  **overriding anything the shell exported** (the interactive shell is known
  to carry TF_VARs for other tenants).
- terraform runs under a generated clean CLI config
  (`TF_CLI_CONFIG_FILE=.bulk_plans/clean.tfrc`) so the **pinned provider**
  (`= 2.0.0-prerelease.1`) is what gets exercised — not whatever
  `dev_overrides` your personal `~/.terraformrc` happens to carry. That
  version is **published to the Terraform Registry**, so the generated config
  is just `provider_installation { direct {} }` — the provider resolves
  straight from the registry, **no filesystem mirror**. Pass
  `--allow-dev-overrides` only when deliberately testing a different local
  provider build via your own `~/.terraformrc`, or `--mirror` to serve a
  local provider build from `~/.terraform.d/mirror` instead of the registry.
- Plan mode sets `DISABLE_POLICY_RULE_CLEANUP=true` so the provider's
  draft-cleanup mutation can't touch in-progress CMA drafts during a plan.

## Brownfield source vs greenfield target

The same folders serve two different jobs:

**Against <source-profile> (source, brownfield)** — plans are a *health check*:
state-owning folders must read `No changes.`; `0-dependencies`/`1-sites`
read `N to import, 0 add/change/destroy` until their one-shot
`migration.tf` adoption is applied. Any unexpected "change" means the JSON
export is stale — re-export (step 1), never apply the diff onto <source-profile>.

**Against a purged dev tenant (greenfield)** — every folder is pure creates:
`N to add, 0 change, 0 destroy`. Two guards protect this path:

1. `migration.tf` **hard stop**: import blocks carry <source-profile> object ids and
   evaluate at *plan* time, so they cannot run against another tenant at all.
   bulk_apply.py refuses **both plan and apply modes** on a non-source
   account while any selected folder contains a `migration.tf`. Park them
   first with the built-in helper —
   `python3 scripts/bulk_apply.py --profile <target> --park-migrations`
   (renames to `migration.tf.parked`; `--unpark-migrations` reverses; parking
   is refused on the source tenant) — they are one-shot adoption files for
   <source-profile> only.
2. **Separate state**: the folders' local tfstate belongs to <source-profile>. For a
   target-tenant replication, run from a fresh clone (or move the
   `terraform.tfstate*` files aside) so <source-profile> state and dev state never
   mix. A replication clone starts stateless on purpose.

Other greenfield gaps the plan review must expect (same list as the
`CHECKLIST.md` generated by `catocli export all --strip-ids`): Wi-Fi PSKs must
be supplied via variables, site licenses re-assigned, physical sockets
re-registered, and id-only group members / mailing-list admin ids re-resolved
on the target.

**Unmanaged referents** — objects this repo doesn't own at all, so no
Terraform plan will ever create or verify them; they must already exist (or
resolve to a built-in) in the target before the referencing rule applies
cleanly:

- `customCategory` (ifw) — custom URL/app categories, no provider resource.
- DLP content/EDM profiles + `userNotification` (apc) — no provider resource.
- `wnw` `allocatedIp` and `popLocation` — tenant-assigned/network-topology values, not
  replicated objects.
- Mailing-list `admin` refs — id-only, re-resolved manually (see `0-dependencies/README.md`
  § Tenant-to-Tenant Migration).
- Webhooks — see § Webhook manual gate above.
- Built-in catalog objects (`application`, `appCategory`, `systemGroup`, `service`,
  `country`, `device`, …) resolve fine on any tenant — no action needed, listed here only
  for completeness.

## Apply-mode guards (bulk_apply.py)

- Applies **only saved plan files** from a prior `--mode plan` run; refuses
  a missing plan or one older than the folder's `.tf`/`config_data` inputs.
- Applying to the source tenant (<SOURCE_ACCOUNT_ID>) requires the extra
  `--source-tenant-apply` flag — a <source-profile> apply is always a deliberate act.
- Non-TTY applies are refused; there is no CI apply path.

## SCIM (step 7 — before apply, see § SCIM gate above)

Users and user groups are not Terraform-managed; `3-scim-users/` holds the
committed CSV dataset and `scripts/scim_sync.py` pushes it through the
custom SCIM connector (`catocli scim import`). Users import before groups
(groups reference members by `external_id`). See `3-scim-users/README.md`.

## Verification (step 9)

Export the **target** tenant and diff against `config_data/`. Expected
residual diffs on a fresh target: object ids (target-assigned), system
default rules the purge skipped, and the known export omissions documented
in `1-sites/README.md`. Anything else is a replication gap — fix the JSON or
the module, re-plan, re-apply.

## Rehearsal discipline

<test-profile> first, <target-profile> second, <source-profile> never. The full rehearsal is:
purge <test-profile> → SCIM push → bulk apply → verify → only then repeat on
<target-profile>.
