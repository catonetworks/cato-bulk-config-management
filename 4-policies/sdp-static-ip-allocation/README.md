# SDP Static IP Allocation (SSIA)

## What this manages

Cato's SDP Static IP Allocation policy — rules that assign a persistent/static internal IP to specific SDP (Secure Direct Presence) client connections instead of the default dynamic pool. This folder manages the **entire** SSIA policy for the `<target-profile>` account as one bulk Terraform-managed unit, sourced from a single JSON export.

## Module

[`catonetworks/bulk-ssia-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-ssia-rules/cato/latest) (not yet published — using a local clone, see `main.tf`). Under the hood it creates `cato_sdp_static_ip_allocation_section`, `cato_sdp_static_ip_allocation_rule`, an optional `cato_sdp_static_ip_allocation_policy` enable/disable toggle (unmanaged here — `policy_enabled` defaults to `null`), and a single `cato_bulk_ssia_move_rule` resource that encodes ordering.

**Export** runs from the **repo root**. **Import and Terraform commands** run from **this folder** (`3-policies/sdp-static-ip-allocation/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_ssia_rules_and_sections.json` follows:

```
data.policy.sdpStaticIpAllocation.policy.rules[]             # each: { rule: { name, description, enabled, ... } }
data.policy.sdpStaticIpAllocation.policy.sections[]           # each: { section_index, section_name, section_id }
data.policy.sdpStaticIpAllocation.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

## Export (backup) workflow

Run from the repo root:

```bash
catocli export ssia_rules --json-filename all_ssia_rules_and_sections.json
```

## Update workflow

1. Edit the JSON directly (or re-export after a UI change).
2. `terraform plan` from this folder.
3. `terraform apply` once the plan matches intent.
4. Commit the updated JSON alongside the `.tf` changes (if any).

## Brownfield adoption

This policy already exists live in Cato (1 rule, 0 sections, as of the last export). Two adoption paths are supported — **Option A (native import blocks) is the recommended best practice**.

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf)</summary>

```hcl
locals {
  ssia_migration_json     = jsondecode(file("../../config_data/all_ssia_rules_and_sections.json"))
  ssia_migration_policy   = local.ssia_migration_json.data.policy.sdpStaticIpAllocation.policy
  ssia_migration_sections = { for s in local.ssia_migration_policy.sections : s.section_name => s.section_id }
  ssia_migration_rules    = { for rw in local.ssia_migration_policy.rules : rw.rule.name => rw.rule.id }
}

import {
  for_each = local.ssia_migration_sections
  to       = module.ssia_rules.cato_sdp_static_ip_allocation_section.sections[each.key]
  id       = each.value
}

import {
  for_each = local.ssia_migration_rules
  to       = module.ssia_rules.cato_sdp_static_ip_allocation_rule.rules[each.key]
  id       = each.value
}
```

`cato_bulk_ssia_move_rule` is plan-time orchestration with no persisted object of its own, so it's never imported — expect it (and `terraform_data.guard_empty_policy`) to show as `to add` on the preview plan. Any planned **change** means the JSON is stale — re-export before applying.

See `migration.tf` in this folder. Finish with `terraform apply`, then delete `migration.tf`.
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import ssia_rules_to_tf --module-name module.ssia_rules "../../config_data/all_ssia_rules_and_sections.json"
```

Mutates Terraform state immediately — no plan preview before the import happens.
</details>

Either way, finish with `terraform plan` — it should report no further changes beyond the always-created guard/move-rule resources.

## Troubleshooting

- `catocli ping` — verify API connectivity/credentials.
- `catocli configure show` — confirm the active profile's account ID matches `account_id_<source-profile>_dev` in `terraform.tfvars`.
- If `terraform plan` shows unexpected changes after import, re-run the export and diff against the committed JSON.

> **Verified against a local `feat/bulk-policy-families` provider build (2026-11-04):** `terraform plan` succeeds — 1 to import, 2 to add, 0 to change. The "blocked" status above applies only to the published 1.5.0-prerelease.1.
