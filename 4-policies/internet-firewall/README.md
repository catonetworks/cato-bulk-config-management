# Internet Firewall (IFW)

## What this manages

Cato's Internet Firewall policy — the rules and sections that control traffic between your sites/users and the public internet (allow/block by source, destination, application, country, etc.). This folder manages the **entire** IFW policy for the `<target-profile>` account as one bulk Terraform-managed unit, sourced from a single JSON export rather than hand-written per-rule resources.

> **Upcoming schema change:** Cato IFW now supports sub-policies. A backward-compatible extension of this folder's JSON schema (adding `sub_policies[]` and sub-policy-aware ordering) is specified in [`../docs/ifw-sub-policy-json-design.md`](../docs/ifw-sub-policy-json-design.md) — proposed, not yet implemented in the module/CLI; existing flat configs like this folder's are unaffected.

## Module

[`catonetworks/bulk-if-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-if-rules/cato/latest) v0.0.8 — pinned in `main.tf`. Under the hood it creates `cato_if_section`, `cato_if_rule`, and a single `cato_bulk_if_move_rule` resource that encodes rule/section ordering.

**Export** runs from the **repo root** (catocli's default `--output-directory` is `config_data`, so running from root lands the file in the right place with no extra flags). **Import and Terraform commands** run from **this folder** (`3-policies/internet-firewall/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_ifw_rules_and_sections.json` follows:

```
data.policy.internetFirewall.policy.rules[]             # each: { rule: { name, description, enabled, source, destination, action, tracking, ... } }
data.policy.internetFirewall.policy.sections[]           # each: { section_index, section_name }
data.policy.internetFirewall.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

`rules_in_sections` is what actually determines section/rule ordering — sections and rules themselves can be listed in any order in the `rules`/`sections` arrays.

## Export (backup) workflow

Run from the repo root:

```bash
catocli export if_rules --json-filename all_ifw_rules_and_sections.json
```

This overwrites `config_data/all_ifw_rules_and_sections.json` with the live policy state from the `<target-profile>` account. Run this any time you want a fresh backup, or before diffing against what's currently committed.

## Update workflow

1. Edit the JSON directly (or re-export after making a change in the Cato UI, if the UI change should become the new source of truth).
2. `terraform plan` from this folder — review the diff carefully, especially rule ordering.
3. `terraform apply` once the plan matches intent.
4. Commit the updated JSON alongside the `.tf` changes (if any) so the repo stays the backup of record.

## Brownfield adoption

If this policy already exists live in Cato (it does, for the `<target-profile>` account) and you're bringing it under Terraform management for the first time, two adoption paths are supported. **Option A (native import blocks) is the recommended best practice** — it is declarative and previewable in `terraform plan` before state is touched.

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf)</summary>

Create a one-shot `migration.tf` in this folder (Terraform >= 1.7), run `terraform plan` to preview — expect `N to import, 0 to add, 0 to change, 0 to destroy` (any "change" means the JSON export is stale: re-export rather than letting Terraform overwrite the live policy) — then `terraform apply` to adopt, and delete `migration.tf` afterwards. State addresses are keyed by rule/section **name**, which is why the blocks line up with the exported JSON.

```hcl
locals {
  ifw_policy = jsondecode(file("../../config_data/all_ifw_rules_and_sections.json")).data.policy.internetFirewall.policy
}

import {
  for_each = { for s in local.ifw_policy.sections : s.section_name => s }

  to = module.if_rules.cato_if_section.sections[each.key]
  id = each.value.section_id
}

import {
  for_each = { for r in local.ifw_policy.rules : r.rule.name => r.rule }

  to = module.if_rules.cato_if_rule.rules[each.key]
  id = each.value.id
}
```

If the JSON contains sub-policies, their resources import the same way at `module.if_rules.cato_if_sub_policy.sub_policies["<name>"]` and `module.if_rules.cato_if_rule.sub_rules["<sub_policy>::<rule>"]` — see the bulk-if-rules module README for the full example.
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import if_rules_to_tf --module-name module.if_rules "../../config_data/all_ifw_rules_and_sections.json"
```

Shells `terraform import` per resource — state is mutated immediately as it runs, with no plan preview. Use it when the one-command flow is preferred over authoring import blocks.
</details>

Either way, finish with `terraform plan` — it should report **no changes**. If it does, the JSON and the live policy have drifted; re-export and compare before proceeding.

## Troubleshooting

- `catocli ping` — verify API connectivity/credentials.
- `catocli configure show` — confirm the active profile's account ID matches `account_id_<source-profile>_dev` in `terraform.tfvars`.
- If `terraform plan` shows unexpected changes after import, re-run the export and diff it against the committed JSON — someone may have edited the live policy outside of this repo.
