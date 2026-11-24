# WAN Firewall (WF)

## What this manages

Cato's WAN Firewall policy — rules and sections controlling traffic between sites/segments across the Cato private backbone (site-to-site, site-to-datacenter, etc.), as distinct from Internet Firewall's site-to-internet traffic. This folder manages the entire WF policy for the `<target-profile>` account as one bulk Terraform-managed unit.

## Module

[`catonetworks/bulk-wf-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-wf-rules/cato/latest) v0.0.9 — pinned in `main.tf`. Creates `cato_wf_section`, `cato_wf_rule`, and a bulk move resource for ordering.

**Export** runs from the **repo root** (catocli's default `--output-directory` is `config_data`, so running from root lands the file in the right place with no extra flags). **Import and Terraform commands** run from **this folder** (`3-policies/wan-firewall/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_wf_rules_and_sections.json` follows:

```
data.policy.wanFirewall.policy.rules[]             # each: { rule: { name, description, enabled, source, destination, application, action, tracking, ... } }
data.policy.wanFirewall.policy.sections[]           # each: { section_index, section_name }
data.policy.wanFirewall.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

As with IFW, `rules_in_sections` drives the actual ordering shown in the Cato policy UI.

## Export (backup) workflow

Run from the repo root:

```bash
catocli export wf_rules --json-filename all_wf_rules_and_sections.json
```

## Update workflow

1. Edit the JSON directly, or re-export after a UI-driven change you want to adopt as the new baseline.
2. `terraform plan` — review the diff.
3. `terraform apply`.
4. Commit the updated JSON.

## Brownfield adoption

Two adoption paths are supported; **Option A (native import blocks) is the recommended best practice** — declarative and previewable in `terraform plan` before state is touched.

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf)</summary>

Create a one-shot `migration.tf` in this folder (Terraform >= 1.7), `terraform plan` to preview (`N to import, 0 to add, 0 to change, 0 to destroy`; any "change" = stale JSON — re-export instead of overwriting the live policy), `terraform apply` to adopt, then delete the file. State addresses are keyed by rule/section **name**.

```hcl
locals {
  wf_policy = jsondecode(file("../../config_data/all_wf_rules_and_sections.json")).data.policy.wanFirewall.policy
}

import {
  for_each = { for s in local.wf_policy.sections : s.section_name => s }

  to = module.wf_rules.cato_wf_section.sections[each.key]
  id = each.value.section_id
}

import {
  for_each = { for r in local.wf_policy.rules : r.rule.name => r.rule }

  to = module.wf_rules.cato_wf_rule.rules[each.key]
  id = each.value.id
}
```

Sub-policy resources (if present in the JSON) import at `module.wf_rules.cato_wf_sub_policy.sub_policies["<name>"]` and `module.wf_rules.cato_wf_rule.sub_rules["<sub_policy>::<rule>"]` — see the bulk-wf-rules module README.
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import wf_rules_to_tf --module-name module.wf_rules "../../config_data/all_wf_rules_and_sections.json"
```

Shells `terraform import` per resource — state is mutated immediately as it runs, with no plan preview.
</details>

Follow with `terraform plan` — expect no changes if the adoption matched the live policy.

## Troubleshooting

- `catocli ping` / `catocli configure show` to rule out credential/profile issues.
- Unexpected `terraform plan` diffs after import usually mean the live policy changed since the last export — re-export and compare.
