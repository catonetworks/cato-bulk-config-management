# Split Tunnel (ST)

## What this manages

Cato's Split Tunnel policy — rules that exempt specific destinations from the SDP client's full tunnel, routing them directly instead of through Cato. This folder manages the **entire** Split Tunnel policy for the `<target-profile>` account as one bulk Terraform-managed unit, sourced from a single JSON export.

## Module

[`catonetworks/bulk-st-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-st-rules/cato/latest) (not yet published — using a local clone, see `main.tf`). Under the hood it creates `cato_split_tunnel_section`, `cato_split_tunnel_rule`, an optional `cato_split_tunnel_policy` enable/disable toggle (unmanaged here — `policy_enabled` defaults to `null`), and a single `cato_bulk_st_move_rule` resource that encodes ordering.

**Export** runs from the **repo root**. **Import and Terraform commands** run from **this folder** (`3-policies/split-tunnel/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_st_rules_and_sections.json` follows:

```
data.policy.splitTunnel.policy.rules[]             # each: { rule: { name, description, enabled, ... } }
data.policy.splitTunnel.policy.sections[]           # each: { section_index, section_name, section_id }
data.policy.splitTunnel.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

## Export (backup) workflow

Run from the repo root:

```bash
catocli export st_rules --json-filename all_st_rules_and_sections.json
```

## Update workflow

1. Edit the JSON directly (or re-export after a UI change).
2. `terraform plan` from this folder.
3. `terraform apply` once the plan matches intent.
4. Commit the updated JSON alongside the `.tf` changes (if any).

## Brownfield adoption

This policy already exists live in Cato (4 rules, 0 sections, as of the last export). Two adoption paths are supported — **Option A (native import blocks) is the recommended best practice**.

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf)</summary>

```hcl
locals {
  st_migration_json     = jsondecode(file("../../config_data/all_st_rules_and_sections.json"))
  st_migration_policy   = local.st_migration_json.data.policy.splitTunnel.policy
  st_migration_sections = { for s in local.st_migration_policy.sections : s.section_name => s.section_id }
  st_migration_rules    = { for rw in local.st_migration_policy.rules : rw.rule.name => rw.rule.id }
}

import {
  for_each = local.st_migration_sections
  to       = module.st_rules.cato_split_tunnel_section.sections[each.key]
  id       = each.value
}

import {
  for_each = local.st_migration_rules
  to       = module.st_rules.cato_split_tunnel_rule.rules[each.key]
  id       = each.value
}
```

`cato_bulk_st_move_rule` is plan-time orchestration with no persisted object of its own, so it's never imported — expect it (and `terraform_data.guard_empty_policy`) to show as `to add` on the preview plan. Any planned **change** means the JSON is stale — re-export before applying.

See `migration.tf` in this folder. Finish with `terraform apply`, then delete `migration.tf`.
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import st_rules_to_tf --module-name module.st_rules "../../config_data/all_st_rules_and_sections.json"
```

Mutates Terraform state immediately — no plan preview before the import happens.
</details>

Either way, finish with `terraform plan` — it should report no further changes beyond the always-created guard/move-rule resources.

## Troubleshooting

- `catocli ping` — verify API connectivity/credentials.
- `catocli configure show` — confirm the active profile's account ID matches `account_id_<source-profile>_dev` in `terraform.tfvars`.
- If `terraform plan` shows unexpected changes after import, re-run the export and diff against the committed JSON.

> **Verified against a local `feat/bulk-policy-families` provider build (2026-11-04):** `terraform plan` succeeds — 4 to import, 2 to add, 1 to change. The "blocked" status above applies only to the published 1.5.0-prerelease.1.
