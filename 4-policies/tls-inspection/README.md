# TLS Inspection

## What this manages

Cato's TLS Inspection policy — rules controlling which traffic gets decrypted and inspected (by source, platform, country, connection origin, application) and how untrusted-certificate situations are handled. This folder manages the entire TLS Inspection policy for the `<target-profile>` account as one bulk Terraform-managed unit.

## Module

[`catonetworks/bulk-tls-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-tls-rules/cato/latest) v0.0.1 — pinned in `main.tf`.

**Export** runs from the **repo root** (catocli's default `--output-directory` is `config_data`, so running from root lands the file in the right place with no extra flags). **Import and Terraform commands** run from **this folder** (`3-policies/tls-inspection/`), one level below the shared repo-root `config_data/`.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_tls_rules_and_sections.json` follows:

```
data.policy.tlsInspect.policy.rules[]             # each: { rule: { name, description, enabled, source, platform, country, connectionOrigin, application, action, untrustedCertificateAction, ... } }
data.policy.tlsInspect.policy.sections[]           # each: { section_index, section_name }
data.policy.tlsInspect.policy.rules_in_sections[]  # each: { index_in_section, section_name, rule_name }
```

(The `<target-profile>` account's current export has 0 rules and 0 sections — TLS Inspection hasn't been configured there yet; this is a greenfield config for that policy type.)

## Best practice: category phasing

Per the module's own guidance, if you introduce custom categories for phased TLS inspection rollout, use:

1. `TLSi_Phase_1` — Alcohol and Tobacco, Anonymizers, Botnets, Cheating, Compromised, Criminal Activity, Cults, Hacking, Illegal Drugs, Keyloggers, Malware, Nudity, P2P, Parked domains, Phishing, Porn, Questionable, SPAM, Spyware, Tasteless, Violence and Hate, Weapons.
2. `TLSi_Phase_2` — Entertainment, Gambling, Games, Greeting Cards, Leisure and Recreation, News, Politics, Real Estate, Religion, Sex education, Shopping, Sports, Vehicles, Web Hosting.

See [Working with Categories](https://support.catonetworks.com/hc/en-us/articles/13314286857501-Working-with-Categories).

## Export (backup) workflow

Run from the repo root:

```bash
catocli export tls_rules --json-filename all_tls_rules_and_sections.json
```

## Update workflow

1. Edit the JSON directly, or re-export after a UI-driven change.
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
  tls_policy = jsondecode(file("../../config_data/all_tls_rules_and_sections.json")).data.policy.tlsInspect.policy
}

import {
  for_each = { for s in local.tls_policy.sections : s.section_name => s }

  to = module.tls_rules.cato_tls_section.sections[each.key]
  id = each.value.section_id
}

import {
  for_each = { for r in local.tls_policy.rules : r.rule.name => r.rule }

  to = module.tls_rules.cato_tls_rule.rules[each.key]
  id = each.value.id
}
```
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import tls_rules_to_tf --module-name module.tls_rules "../../config_data/all_tls_rules_and_sections.json"
```

Shells `terraform import` per resource — state is mutated immediately as it runs, with no plan preview.
</details>

Follow with `terraform plan` — expect no changes.

## Troubleshooting

- `catocli ping` / `catocli configure show` for credential issues.
- If decryption/inspection behavior changes unexpectedly in testing, re-export and diff first — TLS rule changes have a direct, immediate effect on client connections.
