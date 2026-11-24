# Terminal Server (TS)

## What this manages

Cato's Terminal Server policy — rules controlling access to published terminal-server/RDP-style resources. This folder manages the **entire** Terminal Server policy for the `<target-profile>` account as one bulk Terraform-managed unit, sourced from a single JSON export.

**This family is greenfield** — as of the last export, the `<target-profile>` account has 0 Terminal Server rules and 0 sections. `main.tf` sets `allow_empty_policy = true` deliberately so `terraform plan`/`apply` aren't blocked by the module's empty-policy safety guard. There is no brownfield adoption step for this folder (no `migration.tf`) since there's nothing live to import.

## Module

[`catonetworks/bulk-ts-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-ts-rules/cato/latest) (not yet published — using a local clone, see `main.tf`). Under the hood it creates `cato_terminal_server_section`, `cato_terminal_server_rule`, an optional `cato_terminal_server_policy` enable/disable toggle (unmanaged here — `policy_enabled` defaults to `null`), and a single `cato_bulk_ts_move_rule` resource that encodes ordering.

**Export** runs from the **repo root**. **Import and Terraform commands** run from **this folder** (`3-policies/terminal-server/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_ts_rules_and_sections.json` follows:

```
data.policy.terminalServer.policy.rules[]             # each: { rule: { name, description, enabled, ... } }
data.policy.terminalServer.policy.sections[]           # each: { section_index, section_name, section_id }
data.policy.terminalServer.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

Currently all three arrays are empty.

## Export (backup) workflow

Run from the repo root:

```bash
catocli export ts_rules --json-filename all_ts_rules_and_sections.json
```

Re-run this any time rules get added to this policy outside of Terraform, to pick up drift. Once real rules exist, remove the `allow_empty_policy = true` guard override from `main.tf` and follow the normal brownfield-adoption pattern used by the other `3-policies/*` policy folders (see e.g. `3-policies/split-tunnel/README.md`) to write a `migration.tf`.

## Update workflow

1. Add/edit rules directly in the JSON (this is a greenfield config — there's no live policy to diff against yet).
2. `terraform plan` from this folder.
3. `terraform apply` once the plan matches intent.
4. Commit the updated JSON alongside the `.tf` changes.

## Brownfield adoption

Not applicable today — 0 live rules, 0 live sections. `terraform plan` should show only the module's always-created `terraform_data.guard_empty_policy` and `cato_bulk_ts_move_rule` resources (plus any rules you've added to the JSON yourself) as `to add`, never `to import`. If a future export shows non-zero rules/sections, follow the Option A / Option B pattern documented in the other `3-policies/*` READMEs (e.g. `3-policies/split-tunnel/README.md`) to adopt them, and drop the `allow_empty_policy` override.

## Troubleshooting

- `catocli ping` — verify API connectivity/credentials.
- `catocli configure show` — confirm the active profile's account ID matches `account_id_<source-profile>_dev` in `terraform.tfvars`.
- If `terraform plan` unexpectedly shows rules "to import" or "to change", someone has created live Terminal Server rules since the last export — re-export and treat this as a brownfield family going forward.

> **Verified against a local `feat/bulk-policy-families` provider build (2026-11-04):** `terraform plan` succeeds — 2 to add (greenfield). The "blocked" status above applies only to the published 1.5.0-prerelease.1.
