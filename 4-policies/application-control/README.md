# Application Control (APC)

## What this manages

Cato's Application Control policy — rules and sections that allow/block traffic by recognized application (not just IP/port). This folder manages the **entire** Application Control policy for the `<target-profile>` account as one bulk Terraform-managed unit, sourced from a single JSON export.

## Module

[`catonetworks/bulk-apc-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-apc-rules/cato/latest) (not yet published — using a local clone, see `main.tf`). Under the hood it creates `cato_application_control_section`, `cato_application_control_rule`, and a single `cato_bulk_apc_move_rule` resource that encodes ordering.

**Export** runs from the **repo root**. **Import and Terraform commands** run from **this folder** (`3-policies/application-control/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_apc_rules_and_sections.json` follows:

```
data.policy.applicationControl.policy.rules[]             # each: { rule: { name, description, enabled, source, destination, action, ... } }
data.policy.applicationControl.policy.sections[]           # each: { section_index, section_name, section_id }
data.policy.applicationControl.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

`rules_in_sections` determines actual section/rule ordering, not array position.

## Export (backup) workflow

Run from the repo root:

```bash
catocli export apc_rules --json-filename all_apc_rules_and_sections.json
```

This overwrites `config_data/all_apc_rules_and_sections.json` with the live policy state from the `<target-profile>` account.

## Update workflow

1. Edit the JSON directly (or re-export after a UI change).
2. `terraform plan` from this folder — review the diff carefully, especially rule ordering.
3. `terraform apply` once the plan matches intent.
4. Commit the updated JSON alongside the `.tf` changes (if any).

## Brownfield adoption

This policy already exists live in Cato (44 rules across 6 sections, as of the last export). Two adoption paths are supported — **Option A (native import blocks) is the recommended best practice**.

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf)</summary>

Create a one-shot `migration.tf` in this folder (Terraform >= 1.7), run `terraform plan` to preview — expect `N to import` plus `cato_bulk_apc_move_rule` and `terraform_data.guard_empty_policy` showing as `to add` (neither is importable). Any planned **change** means the JSON export is stale: re-export rather than letting Terraform overwrite the live policy.

```hcl
locals {
  apc = jsondecode(file("../../config_data/all_apc_rules_and_sections.json")).data.policy.applicationControl.policy

  apc_sections = { for s in local.apc.sections : s.section_name => s }
  apc_rules    = { for r in local.apc.rules : r.rule.name => r.rule }
}

import {
  for_each = local.apc_sections
  to       = module.apc_rules.cato_application_control_section.sections[each.key]
  id       = each.value.section_id
}

import {
  for_each = local.apc_rules
  to       = module.apc_rules.cato_application_control_rule.rules[each.key]
  id       = each.value.id
}
```

See `migration.tf` in this folder. Finish with `terraform apply`, then delete `migration.tf`.
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import apc_rules_to_tf --module-name module.apc_rules "../../config_data/all_apc_rules_and_sections.json"
```

Shells `terraform import` per resource — state is mutated immediately, no plan preview.
</details>

Either way, finish with `terraform plan` — it should report no further changes beyond the always-created guard/move-rule resources.

## Known limitations

- Ordering rows with `section_name: ""` are excluded from the move resource and surfaced via `unordered_rules`; their order is creation order.
- `source`/`destination` sub-selectors with provider `SizeAtLeast(1)` validators are only emitted when non-empty — an empty list in the export is equivalent to omitting the selector.
- Run applies with `-parallelism=1` when several module instances share an account.

## Troubleshooting

- `catocli ping` — verify API connectivity/credentials.
- `catocli configure show` — confirm the active profile's account ID matches `account_id_<source-profile>_dev` in `terraform.tfvars`.
- If `terraform plan` shows unexpected changes after import, re-run the export and diff against the committed JSON.

> **Verified against a local `feat/bulk-policy-families` provider build (2026-11-04):** `terraform plan` succeeds — 50 to import, 2 to add, 44 to change (one-time at-block re-assert). The "blocked" status above applies only to the published 1.5.0-prerelease.1.
