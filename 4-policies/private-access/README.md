# Private Access (PA)

Manages Cato's Private Access policy — a flat, ordered rule list covering access to internally published applications — as one bulk Terraform-managed unit sourced from a single JSON export. See "What this manages" further below for the full picture.

**Current status: ready.** Its former blocker — an `applications` export-shape
mismatch inside the module — has been fixed upstream (see the next section).

## Former blocker — `applications` export-shape mismatch (resolved in the module)

Private Access was never gated behind the provider the way the other five
families' move/toggle resources were: both `cato_private_access_rule` and
`cato_private_access_rule_bulk` have shipped for a while and are present in the
published GA `2.0.0`. `terraform validate` has always passed here. Historical
capture:

```
$ terraform init -input=false
Initializing the backend...
Initializing modules...
Initializing provider plugins...
Terraform has been successfully initialized!

$ terraform validate
Success! The configuration is valid.
```

The real blocker was a schema mismatch between what `catocli export pa_rules`
emits for the `applications` field and what the module's HCL expected.
Previously, `terraform plan` with real credentials failed inside the module
with a `locals` evaluation error (historical capture, `-parallelism=1`, never
applied):

```
$ terraform plan -input=false -parallelism=1

Terraform used the selected providers to generate the following execution
plan. Resource actions are indicated with the following symbols:
  + create

Terraform planned the following actions, but then encountered a problem:

  # module.pa_rules.terraform_data.guard_empty_policy will be created
  + resource "terraform_data" "guard_empty_policy" {
      + id = (known after apply)
    }

Plan: 1 to add, 0 to change, 0 to destroy.

╷
│ Error: Unsupported attribute
│
│   on ../../../catonetworks/terraform-cato-bulk-private-access/main.tf line 52, in locals:
│   52:           (can(a.application.name) ? { name = a.application.name } : { id = a.application.id })
│
│ This value does not have any attributes.
╵
```

### Root cause and fix

The live export (`../../config_data/all_pa_rules_and_sections.json`, produced
by `catocli export pa_rules`) emits `applications` as a **single wrapper
object** containing one array under the key `"application"` — e.g. for rule
`Beta User Access - OS Validation`:

```json
"applications": { "application": [ { "name": "WIKI" } ] }
```

The module's `rule_bodies` local originally iterated `rw.rule.applications`
directly, expecting a list of `{ application: { name | id } }` wrappers, so it
failed on the single-object shape. The module
(`terraform-cato-bulk-private-access/main.tf`, ~line 52) now unwraps the inner
`application` array first and tolerates **both** shapes:

```hcl
applications = [
  for a in try(rw.rule.applications.application, rw.rule.applications, []) :
  (
    can(a.application) ?
    (can(a.application.name) ? { name = a.application.name } : { id = a.application.id }) :
    (can(a.name) ? { name = a.name } : { id = a.id })
  )
]
```

With that fix, `terraform validate` passes and `terraform plan` proceeds
through the `applications` evaluation. This folder is **no longer blocked
end-to-end**.

## What this manages

Cato's **Private Access** policy — a flat, ordered rule list (no sections
yet in the terraform surface, no sub-policies) covering action,
applications, source (users/user_groups), connection_origins, countries,
devices, platforms, schedule, active_period, tracking, and
user_attributes.risk_score. This folder manages the entire Private Access
policy for the `<target-profile>` account as one bulk Terraform-managed unit,
sourced from a single JSON export.

**Live state:** the `<target-profile>` account currently has **3 rules** (`Beta
User Access - OS Validation`, `Block Risky Access to Corporate
Applictions`, `Access to Corporate Application`) and no sections (this
policy type has none in the terraform surface). `migration.tf` in this
folder imports all 3 rules by name/id.

## Module

[`terraform-cato-bulk-private-access`](https://github.com/catonetworks/terraform-cato-bulk-private-access) —
sourced from a **local path** (`../../../catonetworks/terraform-cato-bulk-private-access`)
instead of the registry, since no registry release exists yet for this
family (kept consistent with the other five new folders even though this
one's resources already validate — see commented-out registry source,
`catonetworks/bulk-private-access/cato` version `0.0.1`, in `main.tf`).
Under the hood it creates `cato_private_access_rule` (full attribute
coverage, publishes per rule) and a single
`cato_private_access_rule_bulk` resource that encodes rule ordering and
republishes on changes. The account-level policy toggle
(`cato_private_access_policy`) is intentionally **not** managed by this
module — enable the policy separately (CMA or a standalone resource) if
enforcement is wanted.

**Export** runs from the **repo root**. **Import and Terraform commands**
run from **this folder** (`3-policies/private-access/`).

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

`../../config_data/all_pa_rules_and_sections.json` follows:

```
data.policy.privateAccess.policy.rules[]            # each: { audit, rule: { name, action.action, applications: { application[] }, source{user,usersGroup}, connectionsOriginList, country, device, platform, schedule, userAttributes.riskScore, ... }, properties }
data.policy.privateAccess.policy.rules_in_policy[]   # each: { index_in_policy, rule_name } — THE ordering table (no sections for this type)
```

Note the `applications` shape documented above under "Root cause and fix" —
the live exporter emits a single wrapper object around one `application` array;
the module unwraps it (and still tolerates a flat list of wrappers).

## Export (backup) workflow

Run from the repo root:

```bash
CATO_PROFILE=<source-profile> catocli export pa_rules --json-filename all_pa_rules_and_sections.json --output-directory config_data
```

## Update workflow

1. Edit the JSON directly (or re-export after a Cato UI change).
2. `terraform plan` from this folder.
3. `terraform apply`.
4. Commit the updated JSON alongside the `.tf` changes.

## Brownfield adoption

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf, already written)</summary>

`migration.tf` in this folder imports the 3 live rules by name/id. This
module has no section resources (Private Access has none yet) and no
policy-toggle resource wired in, so `cato_private_access_rule` is the only
importable resource:

```hcl
locals {
  migrate_json  = jsondecode(file("../../config_data/all_pa_rules_and_sections.json"))
  migrate_rules = { for rw in local.migrate_json.data.policy.privateAccess.policy.rules : rw.rule.name => rw.rule.id }
}

import {
  for_each = local.migrate_rules

  to = module.pa_rules.cato_private_access_rule.rules[each.key]
  id = each.value
}
```

`terraform plan` against this should show `3 to import, 1 to add, 0 to change,
0 to destroy` (the "1 to add" being
`cato_private_access_rule_bulk.all_pa_rules`, which cannot be imported).
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

```bash
catocli import pa_rules_to_tf config_data/all_pa_rules_and_sections.json \
  --module-name module.pa_rules --auto-approve
```

An imperative alternative to Option A's import blocks; the underlying plan
evaluation now handles the `applications` wrapper shape.
</details>

## Known limitations

- `applications` export shape — the exporter emits a single wrapper object;
  the module unwraps it (see "Root cause and fix" above). No action needed.
- `source.systemGroup` appears in exports for fidelity but has no
  terraform attribute yet.
- `active_period` is only passed when meaningful.
- Run applies with `-parallelism=1` when several module instances share an
  account.

## Troubleshooting

- `catocli ping` — verify API connectivity/credentials.
- `catocli configure show` — confirm the active profile's account ID
  matches `account_id_<source-profile>_dev` in `terraform.tfvars`.
- The former `applications` locals error is fixed (the module unwraps the
  wrapper object). If a future exporter change alters the `applications` shape
  again, re-verify with `terraform plan -parallelism=1` (real creds, never
  apply).
