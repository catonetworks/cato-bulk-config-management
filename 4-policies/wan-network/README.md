# WAN Network (WNW)

## What this manages

Cato's WAN Network policy — routing and path-selection rules (rule type, route type, bandwidth priority, exceptions) that govern how traffic is routed across the Cato backbone, rather than whether it's allowed. This folder manages the entire WNW policy for the `<target-profile>` account as one bulk Terraform-managed unit.

## Module

[`catonetworks/bulk-wnw-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-wnw-rules/cato/latest) v0.0.1 — pinned in `main.tf`.

**Export** runs from the **repo root** (catocli's default `--output-directory` is `config_data`, so running from root lands the file in the right place with no extra flags). **Import and Terraform commands** run from **this folder** (`3-policies/wan-network/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_wnw_rules_and_sections.json` follows:

```
data.policy.wanNetwork.policy.rules[]             # each: { rule: { name, description, enabled, ruleType, routeType, source, destination, application, bandwidthPriority, exceptions, ... } }
data.policy.wanNetwork.policy.sections[]           # each: { section_index, section_name }
data.policy.wanNetwork.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

## Export (backup) workflow

Run from the repo root:

```bash
catocli export wnw_rules --json-filename all_wnw_rules_and_sections.json
```

## Update workflow

1. Edit the JSON directly, or re-export after a UI-driven change.
2. `terraform plan` — review the diff, paying attention to `routeType`/`bandwidthPriority` changes since these affect live traffic routing, not just access control.
3. `terraform apply`.
4. Commit the updated JSON.

## Brownfield adoption

Two adoption paths are supported; **Option A (native import blocks) is the recommended best practice** — declarative and previewable in `terraform plan` before state is touched.

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf)</summary>

Create a one-shot `migration.tf` in this folder (Terraform >= 1.7), `terraform plan` to preview (`N to import, 0 to add, 0 to change, 0 to destroy`; any "change" = stale JSON — re-export instead of overwriting the live policy), `terraform apply` to adopt, then delete the file. State addresses are keyed by rule/section **name**.

```hcl
locals {
  wnw_policy = jsondecode(file("../../config_data/all_wnw_rules_and_sections.json")).data.policy.wanNetwork.policy
}

import {
  for_each = { for s in local.wnw_policy.sections : s.section_name => s }

  to = module.wnw_rules.cato_wnw_section.sections[each.key]
  id = each.value.section_id
}

import {
  for_each = { for r in local.wnw_policy.rules : r.rule.name => r.rule }

  to = module.wnw_rules.cato_wnw_rule.rules[each.key]
  id = each.value.id
}
```
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import wnw_rules_to_tf --module-name module.wnw_rules "../../config_data/all_wnw_rules_and_sections.json"
```

Shells `terraform import` per resource — state is mutated immediately as it runs, with no plan preview.
</details>

Follow with `terraform plan` — expect no changes.

## Troubleshooting

- `catocli ping` / `catocli configure show` for credential issues.
- Because WNW rules affect routing behavior directly, treat any unexpected `terraform plan` diff here as higher-priority to investigate than an IFW/WF diff — re-export and compare before applying.
