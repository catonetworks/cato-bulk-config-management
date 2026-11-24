# Site Web Proxy (SWP)

## What this manages

Cato's Site Web Proxy policy — rules that steer site (not just user/client) web traffic through Cato's explicit web proxy. This folder manages the **entire** SWP policy for the `<target-profile>` account as one bulk Terraform-managed unit, sourced from a single JSON export.

> **Experimental: rule management.** `cato_site_web_proxy_rule` is flagged upstream as held back pending a server-side `addRule` API defect fix, though the provider schema pinned here (`2.0.0`) already includes the resource and the module's `main.tf` creates/manages it. **It has no `ImportState` support** — brownfield adoption below can only bring the live *sections* under Terraform management; the one live rule cannot be imported and will show as a planned "to add" (i.e. a create, not an import) until the provider ships import support. Treat rule create/update/delete through this folder as experimental until that lands — verify changes carefully in CMA after apply.

## Module

[`catonetworks/bulk-swp-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-swp-rules/cato/latest) (not yet published — using a local clone, see `main.tf`). Under the hood it creates `cato_site_web_proxy_section`, `cato_site_web_proxy_rule`, an optional `cato_site_web_proxy_policy` enable/disable toggle (unmanaged here — `policy_enabled` defaults to `null`), and a single `cato_bulk_swp_move_rule` resource that encodes ordering.

**Export** runs from the **repo root**. **Import and Terraform commands** run from **this folder** (`3-policies/site-web-proxy/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_swp_rules_and_sections.json` follows:

```
data.policy.siteWebProxy.policy.rules[]             # each: { rule: { name, description, enabled, ... } }
data.policy.siteWebProxy.policy.sections[]           # each: { section_index, section_name, section_id }
data.policy.siteWebProxy.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

## Export (backup) workflow

Run from the repo root:

```bash
catocli export swp_rules --json-filename all_swp_rules_and_sections.json
```

## Update workflow

1. Edit the JSON directly (or re-export after a UI change).
2. `terraform plan` from this folder.
3. `terraform apply` once the plan matches intent.
4. Commit the updated JSON alongside the `.tf` changes (if any).

## Brownfield adoption

This policy already exists live in Cato (1 rule, 0 sections, as of the last export). Two adoption paths are supported — **Option A (native import blocks) is the recommended best practice**, but note the rule-import caveat above: with 0 live sections and a rule type that can't be imported, this family currently has **nothing importable**, and `terraform plan` will show the 1 live rule as a create rather than an import. Re-check the provider CHANGELOG before assuming rule import has shipped.

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf)</summary>

```hcl
# migration.tf - sections only; cato_site_web_proxy_rule has no ImportState support.
locals {
  swp_migration_json     = jsondecode(file("../../config_data/all_swp_rules_and_sections.json"))
  swp_migration_policy   = local.swp_migration_json.data.policy.siteWebProxy.policy
  swp_migration_sections = { for s in local.swp_migration_policy.sections : s.section_name => s.section_id }
}

import {
  for_each = local.swp_migration_sections
  to       = module.swp_rules.cato_site_web_proxy_section.sections[each.key]
  id       = each.value
}
```

`cato_bulk_swp_move_rule` is plan-time orchestration with no persisted object of its own, so it's never imported — expect it (and `terraform_data.guard_empty_policy`) to show as `to add` on the preview plan, **plus the live rule itself as `to add`** (the known rule-import gap above). See `migration.tf` in this folder. Finish with `terraform apply`, then delete `migration.tf`.
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import swp_rules_to_tf --module-name module.swp_rules "../../config_data/all_swp_rules_and_sections.json"
```

Mutates Terraform state immediately — no plan preview before the import happens. Subject to the same rule-import caveat above: it can only bring sections under management until `cato_site_web_proxy_rule` ships import support.
</details>

Either way, `terraform plan` will **not** be fully clean for this family until `cato_site_web_proxy_rule` import ships — see the experimental note above.

## Troubleshooting

- `catocli ping` — verify API connectivity/credentials.
- `catocli configure show` — confirm the active profile's account ID matches `account_id_<source-profile>_dev` in `terraform.tfvars`.
- If `terraform apply` fails with a "rule already exists" style error, that's the known import gap — the rule exists live but Terraform has no record of it in state.

> **Verified against a local `feat/bulk-policy-families` provider build (2026-11-04):** `terraform plan` succeeds — 1 to import, 2 to add, 0 to change (rule import works on the branch). The "blocked" status above applies only to the published 1.5.0-prerelease.1.
