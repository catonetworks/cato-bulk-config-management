# LAN Firewall

## What this manages

Cato's LAN Firewall (`socketLan`) policy — network rules (which LAN segments/sites can route to each other) and, nested under each network rule, firewall rules (allow/block by source, destination, application, etc.). This folder manages the **entire** LAN Firewall policy for the `<target-profile>` account as one bulk Terraform-managed unit, sourced from a single JSON export.

## Module

[`catonetworks/bulk-lan-fw-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-lan-fw-rules/cato/latest) (not yet published — using a local clone, see `main.tf`). Under the hood it creates `cato_socket_lan_section` (main + sub-policy sub-sections), `cato_lf_sub_policy`, `cato_socket_lan_network_rule`, `cato_socket_lan_firewall_rule`, and a single `cato_bulk_lf_move_rule` resource that encodes ordering.

**Export** runs from the **repo root**. **Import and Terraform commands** run from **this folder** (`3-policies/lan-firewall/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_lan_fw_rules_and_sections.json` follows:

```
data.policy.socketLan.policy.sections[]          # each: { section_index, section_name, section_id }
data.policy.socketLan.policy.sub_policies[]       # each sub-policy (empty today)
data.policy.socketLan.policy.network_rules[]      # each: { rule: {...}, firewall_rules: [{ rule: {...} }, ...] }
data.policy.socketLan.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

Note the rules key is `network_rules`, not `rules` — the LAN FW export schema differs from the other bulk rule exports. `rules_in_sections` determines actual section/rule ordering.

## Export (backup) workflow

Run from the repo root:

```bash
catocli export lan_fw_rules --json-filename all_lan_fw_rules_and_sections.json
```

This overwrites `config_data/all_lan_fw_rules_and_sections.json` with the live policy state from the `<target-profile>` account.

## Update workflow

1. Edit the JSON directly (or re-export after a UI change).
2. `terraform plan` from this folder — review the diff carefully, especially rule ordering.
3. `terraform apply` once the plan matches intent.
4. Commit the updated JSON alongside the `.tf` changes (if any).

## Brownfield adoption

This policy already exists live in Cato (3 sections, 7 network rules, 8 nested firewall rules, 0 sub-policies as of the last export). Two adoption paths are supported — **Option A (native import blocks) is the recommended best practice**.

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf)</summary>

`migration.tf` in this folder imports sections, sub-policies, sub-sections, network rules, and nested firewall rules by name. Run `terraform plan` to preview — expect `N to import` plus `cato_bulk_lf_move_rule` and `terraform_data.guard_empty_policy` showing as `to add` (neither is importable: the move resource re-publishes ordering on every apply with no object of its own, and the guard is a plan-time precondition check). Any planned **change** (not just import/add) means the JSON export is stale — re-export rather than letting `terraform apply` overwrite the live policy.

```hcl
locals {
  lanfw = jsondecode(file("../../config_data/all_lan_fw_rules_and_sections.json")).data.policy.socketLan.policy

  lanfw_main_sections = {
    for s in local.lanfw.sections : s.section_name => s
    if try(s.sub_policy_name, null) == null
  }
  lanfw_network_rules = {
    for nr in local.lanfw.network_rules : nr.rule.name => nr.rule
  }
  lanfw_firewall_rules = merge([
    for nr in local.lanfw.network_rules : {
      for fw in try(nr.firewall_rules, []) :
      "${nr.rule.name}::${fw.rule.name}" => fw.rule
    }
  ]...)
}

import {
  for_each = local.lanfw_main_sections
  to       = module.lan_fw_rules.cato_socket_lan_section.sections[each.key]
  id       = each.value.section_id
}

import {
  for_each = local.lanfw_network_rules
  to       = module.lan_fw_rules.cato_socket_lan_network_rule.network_rules[each.key]
  id       = each.value.id
}

import {
  for_each = local.lanfw_firewall_rules
  to       = module.lan_fw_rules.cato_socket_lan_firewall_rule.firewall_rules[each.key]
  id       = each.value.id
}
```

See `migration.tf` in this folder for the full version (also covers sub-policies/sub-sections, currently empty live). Finish with `terraform apply`, then delete `migration.tf`.
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import lan_fw_rules_to_tf --module-name module.lan_fw_rules "../../config_data/all_lan_fw_rules_and_sections.json"
```

Shells `terraform import` per resource — state is mutated immediately, no plan preview.
</details>

Either way, finish with `terraform plan` — it should report no further changes beyond the always-created guard/move-rule resources.

## Known limitations

- Ordering rows with `section_name: ""` (rules at the policy root, or inside a sub-policy without a sub-section) are excluded from the move resource and surfaced via `unordered_network_rules`; their order is creation order.
- A sub-policy's own `enabled` flag cannot be set via the public API — toggle it in CMA.
- The CMA UI shows per-network-rule "Block Any" children and a "Default rules" section; these are UI-synthesized defaults the API does not return, so they're never exported and not managed here.

## Troubleshooting

- `catocli ping` — verify API connectivity/credentials.
- `catocli configure show` — confirm the active profile's account ID matches `account_id_<source-profile>_dev` in `terraform.tfvars`.
- If `terraform plan` shows unexpected changes after import, re-run the export and diff against the committed JSON.
