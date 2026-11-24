# Dynamic IP Allocation (DIA)

> **Provider-release block resolved (2026-11-05/06):** earlier revisions of this
> README documented a hard `terraform validate` failure because
> `cato_dynamic_ip_allocation_policy` and `cato_bulk_dia_move_rule` only existed
> on the provider's `feat/bulk-policy-families` branch. Those resources shipped
> in `1.5.0-prerelease.2`, and this folder now pins `= 2.0.0`
> (published to the Terraform Registry) — `terraform init` and `terraform validate`
> both pass against this exact config.

## What this manages

Cato's **Dynamic IP Allocation** policy — rules controlling dynamic IP
assignment by global IP range, country, device posture, platform, and
source{user,usersGroup} — plus sections and the account-level
enable/disable toggle. This folder manages the entire Dynamic IP
Allocation policy for the `<target-profile>` account as one bulk
Terraform-managed unit, sourced from a single JSON export.

**Live state:** the `<target-profile>` account currently has **0 sections and 1
rule** (`EMEA-DYNAMIC`) in this policy family. `migration.tf` in this
folder imports that rule; there are no sections to import.
`policy_enabled` is left unmanaged (`null` default) in `main.tf`, so the
`cato_dynamic_ip_allocation_policy` singleton toggle is also not imported
— the module README documents that its import only applies when
`policy_enabled` is set to a non-null value.

## Module

[`terraform-cato-bulk-dia-rules`](https://github.com/catonetworks/terraform-cato-bulk-dia-rules) —
sourced from a **local path** (`../../../catonetworks/terraform-cato-bulk-dia-rules`)
instead of the registry, since no registry release exists yet for this
family. A commented-out registry source (`catonetworks/bulk-dia-rules/cato`,
version `0.0.1`) is left in `main.tf` to swap in once published. Under the
hood it creates `cato_dynamic_ip_allocation_section`,
`cato_dynamic_ip_allocation_rule`, and a single `cato_bulk_dia_move_rule`
resource that encodes rule/section ordering and publishes the policy
(available since provider `1.5.0-prerelease.2`). Dynamic IP Allocation ships
**disabled** on new tenants; this folder leaves the toggle unmanaged.

**Export** runs from the **repo root**. **Import and Terraform commands**
run from **this folder** (`3-policies/dynamic-ip-allocation/`).

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_dia_rules_and_sections.json` follows:

```
data.policy.dynamicIpAllocation.policy.sections[]           # each: { section_index, section_name }
data.policy.dynamicIpAllocation.policy.rules[]               # each: { audit, rule: { name, range.globalIpRange (required), country, devicePostureProfile, platform, source{user,usersGroup}, ... }, properties }
data.policy.dynamicIpAllocation.policy.rules_in_sections[]   # each: { index_in_section, section_name, rule_name }
```

`rules_in_sections` is what actually determines section/rule ordering.

## Export (backup) workflow

Run from the repo root:

```bash
CATO_PROFILE=<source-profile> catocli export dia_rules --json-filename all_dia_rules_and_sections.json --output-directory config_data
```

## Update workflow

1. Edit the JSON directly (or re-export after a Cato UI change).
2. `terraform plan` from this folder.
3. `terraform apply` once the plan matches intent.
4. Commit the updated JSON alongside the `.tf` changes.

## Brownfield adoption

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf, already written)</summary>

`migration.tf` in this folder imports the 1 live rule by name/id (no
sections exist live, so no section import blocks; no policy-toggle import
since `policy_enabled` isn't set):

```hcl
locals {
  migrate_json   = jsondecode(file("../../config_data/all_dia_rules_and_sections.json"))
  migrate_policy = local.migrate_json.data.policy.dynamicIpAllocation.policy
  migrate_rules  = { for rw in local.migrate_policy.rules : rw.rule.name => rw.rule.id }
}

import {
  for_each = local.migrate_rules

  to = module.dia_rules.cato_dynamic_ip_allocation_rule.rules[each.key]
  id = each.value
}
```

`terraform plan` against this should show `1 to import, 1 to add, 0 to
change, 0 to destroy` — the "1 to add" is
`cato_bulk_dia_move_rule.all_dia_rules`, which cannot be imported.
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import dia_rules_to_tf config_data/all_dia_rules_and_sections.json \
  --module-name module.dia_rules --auto-approve
```

Available now that the provider blocker is resolved (provider `2.0.0`).
</details>

## Troubleshooting

- `catocli ping` — verify API connectivity/credentials.
- `catocli configure show` — confirm the active profile's account ID matches
  `account_id_<source-profile>_dev` in `terraform.tfvars`.
- `terraform validate`/`init` now pass against provider `2.0.0`. Run
  `terraform plan` against `migration.tf` to adopt the 1 live rule, then
  delete `migration.tf`. Consider whether `policy_enabled` should be set —
  Dynamic IP Allocation ships disabled on new tenants.

> **Verified (2026-11-04, local `feat/bulk-policy-families` build; resources published in `1.5.0-prerelease.2`):** `terraform plan` succeeds — 1 to import, 2 to add, 0 to change.
