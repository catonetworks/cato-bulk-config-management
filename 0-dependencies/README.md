# 0-dependencies — Bulk Dependency Objects (IP Ranges, Custom Apps, Containers, Notification Recipients, Webhooks, DHCP Relay Groups)

This folder bulk-manages eight of the nine dependency object types that the policy folders (`4-policies/*`) reference by name but don't own — **Global IP Ranges**, **Custom Applications**, **FQDN Containers**, **IP Address Range Containers**, **Mailing Lists**, **Subscription Groups**, **Webhooks**, and **DHCP Relay Groups** — from JSON configuration files, using native `cato` provider resources only (provider `1.5.0-prerelease.1`+, which introduced the custom-application, container, mailing-list, and subscription-group resources; `1.5.0-prerelease.3` added the `cato_webhook` resource). No wrapper modules and no CLI/python provisioners: each type is a plain `for_each` (or singleton) over its JSON file, inline in `main.tf`.

**Groups moved to [`../3-groups/`](../3-groups/)** on 2026-11-06: live group members in this
account are site-scoped (`SITE`/`SITE_NETWORK_SUBNET`), so groups must deploy
*after* `1-sites/` rather than alongside these tenant-wide dependency types,
which have no such ordering constraint. See `0-dependencies/migration-groups-handover.tf`
for the one-shot state handover and CLAUDE.md's Structure section for the
full deployment-order rationale.

Conventions (apply to every type here):
- **State is indexed by name** — every `for_each` is keyed by the object's name, so addresses stay stable across greenfield creation, brownfield import, and tenant-to-tenant migration.
- **Attributes reference related objects by name**, not ID, wherever the provider resolves names (subscription-group → mailing-list refs). IDs appear only in import blocks and for the admin types that cannot resolve by name yet.

Account: **<target-profile> (<account-id>)**. Live inventory at adoption time: 9 global IP ranges, 13 custom applications, 2 FQDN containers, 1 IP range container, 1 mailing list, 4 subscription groups, 0 DHCP relay groups (22 objects; 4 groups now live in `../3-groups/`).

## Usage

The folder reads eight JSON configuration files from the shared `../config_data/` directory:

- **Global IP Ranges**: `global_ip_ranges.json` — one **singleton** `cato_global_ip_ranges` resource owns the account's entire range set (declared only when there are ranges: with none, the provider can't plan an empty set)
- **Custom Applications**: `custom_applications.json` — one `cato_custom_application` per element
- **FQDN Containers**: `fqdn_containers.json` — one `cato_fqdn_container` per element
- **IP Address Range Containers**: `ip_range_containers.json` — one `cato_ip_address_range_container` per element
- **Mailing Lists**: `mailing_lists.json` — one `cato_mailing_list` per element
- **Subscription Groups**: `subscription_groups.json` — one `cato_subscription_group` per element
- **Webhooks**: `webhooks.json` — one `cato_webhook` per element; deploys **before** Subscription Groups, which reference webhooks by name via `integration[]`
- **DHCP Relay Groups**: `dhcp_relay_groups.json` — one `cato_dhcp_relay_group` per element (empty in this account today)

### Basic Example

```bash
cd 0-dependencies
cp terraform.tfvars.example terraform.tfvars   # fill in token + account id (gitignored)
terraform init
terraform plan
terraform apply -parallelism=1   # Cato API requires sequential execution
```

### JSON Configuration Structure

All files but one are top-level JSON arrays, refreshed from the live tenant via `catocli export` (see Brownfield section) — `app_connectors.json` is `{connectors[], groups[]}`. (`groups.json` moved to `../3-groups/README.md`.) The 2026-11-10 additions: `allocated_ips.json` (PoP allocated IPs — Cato assigns the ip, so targets get NEW ips and policy `allocationIp` refs are remapped at deploy time by `../scripts/remap_allocated_ips.py`), `private_applications.json`, and `app_connectors.json` (connector groups are created implicitly via each connector's `group_name`).

#### Key Fields

**`global_ip_ranges.json`** — `[{name, description, ipRange}]`
- `ipRange` maps to the resource's `ip_range` (single IP, `start-end`, or CIDR).
- The resource is a **singleton**: removing an element from the JSON removes the live range on apply.

**`custom_applications.json`** — `[{id, name, description, category: [{id, name}], criteria: [...]}]`
- `id` — Cato application ID (e.g. `CustomApp_<account-id>_45274` or a UUID); used only for import.
- `criteria[]` — `{protocol, port[], portRange[], destination: {destinationIp: {ip[], ipRange[], subnet[]}, domain[], fqdn[]}}`; `main.tf` maps camelCase JSON to the provider's snake_case schema and casts ports to strings.

**`fqdn_containers.json`** / **`ip_range_containers.json`** — `[{id, name, description, mode, values: [...]}]` (+ `file_type`, `sync_data` for URL-mode containers)
- `mode` — `INLINE` (this config owns the full member set via `values`) or `URL` (sync settings own it). `name` and `mode` are **replace-on-change**; `description` is a required attribute (exported as `""`).
- FQDN `values` are strings; IP-range `values` are `{from, to}` pairs. INLINE values are reconstructed at export time via the container download API.

**`mailing_lists.json`** — `[{id, name, address: [...], admin: [{id, name}]}]`
- The provider requires at least one address **or** admin to create a list; admin refs are id-only today (name rides along). A list exported with both empty (possible live state) imports fine but cannot be re-created greenfield until it has a member.

**`subscription_groups.json`** — `[{id, name, mailing_list: [{id, name}], integration: [{id, name}]}]`
- `main.tf` references mailing lists and integrations **by name**; the ids in the JSON are used only by `migration.tf` for import.
- `cato_subscription_group.this` carries `depends_on = [cato_mailing_list.this, cato_webhook.this]` — notification chain ordering, since every subscription group in this account references a mailing list by name and `integration[]` can reference a webhook by name too.

**`webhooks.json`** — `[{id, name, enabled, url, requestMethod, auth: {authType}, header: [...], body, responseMapping: [...], relatedWebhookId}]`
- Refreshed by **`scripts/export_webhooks.py --profile <profile>`**, NOT `catocli export webhooks` — interim workaround: the full generated webhook query selects the `auth` union member `CustomHeadersAuth`, a type the bundled schema advertises but the live notification service hasn't deployed yet, so it fails query-time with `Unknown type 'CustomHeadersAuth'`. The script issues a raw query selecting only the scalar `auth { authType }` (never a union-member fragment), plus `header[]`, `body`, and `responseMapping[]` — a richer, credential-free capture than the stock export. Retire this script once the API deploys `CustomHeadersAuth` and fold its fields into `catocli export webhooks`.
- **Secrets are not in the export.** `auth.authType` only tells you the auth *kind* — real credentials come from the `webhook_secrets` tfvars map (sensitive), keyed by webhook name, each entry supplying exactly one of `basic{username,password}` / `bearer{bearer_token}` / `custom{name,value}`. `body` (a payload template, not a secret) comes straight from the export.
- **NO_AUTH limitation (interim):** the `cato_webhook` resource requires exactly one of `basic`/`bearer`/`custom` and has no `NO_AUTH` input (`NoAuth` is an output-only GraphQL type). A webhook with no matching `webhook_secrets` entry — including every `NO_AUTH` webhook — is created with `var.webhook_default_auth`, an interim placeholder custom header (default name `X-Cato-Webhook-Placeholder`), so it sends that extra header on every call until the provider gains `NO_AUTH` support. The `webhooks_using_placeholder_auth` output lists which webhooks got the placeholder.
- `relatedWebhookId` is a source-tenant id; `main.tf` resolves it to the referenced webhook's **name** and passes it as `related_webhook_ref = {by = "NAME", input = <name>}` for cross-tenant portability. If the related webhook is created in the same apply, a single convergence re-apply may be needed to wire the reference.

**`dhcp_relay_groups.json`** — `[{id, name, server: [...]}]`
- `id` — opaque Cato resource identifier (not a legacy numeric entity id); used only for import. The provider resolves/recreates by `name`.
- `server` — list of DHCP relay server IP addresses; both `name` and `server` are required by the resource. Currently empty in this account (greenfield for this type).

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0-prerelease.1` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## Working with Existing Objects (Brownfield Deployments)

For brownfield tenants where these objects already exist in the Cato Management Application, export them with the Cato CLI and adopt them into Terraform state with the `import` blocks in `migration.tf`.

### Installing and Configuring Cato CLI

1. **Install the Cato CLI:**
   ```bash
   pip3 install catocli
   ```

2. **Configure the CLI with your Cato credentials:**
   ```bash
   catocli configure set
   ```

### Exporting Existing Objects

Run from the repo root (requires catocli with the `global_ip_ranges` / `custom_apps` export subcommands; `catocli export groups` now feeds `../3-groups/`):

```bash
catocli export global_ip_ranges    --json-filename config_data/global_ip_ranges.json
catocli export custom_apps         --json-filename config_data/custom_applications.json
catocli export fqdn_containers     --json-filename config_data/fqdn_containers.json
catocli export ip_range_containers --json-filename config_data/ip_range_containers.json
catocli export mailing_lists       --json-filename config_data/mailing_lists.json
catocli export subscription_groups --json-filename config_data/subscription_groups.json
catocli export dhcp_relay_groups   --json-filename config_data/dhcp_relay_groups.json
catocli export allocated_ips       --json-filename config_data/allocated_ips.json
catocli export private_apps        --json-filename config_data/private_applications.json
catocli export app_connectors      --json-filename config_data/app_connectors.json

# Webhooks are the one exception -- NOT catocli export (see Key Fields above for why):
python3 scripts/export_webhooks.py --profile <source-profile> --output config_data/webhooks.json
```

All catocli-exported types above support `--strip-ids` for tenant-to-tenant migration (drops tenant-specific ids; see Tenant-to-Tenant Migration below). `scripts/export_webhooks.py` has no `--strip-ids` flag — webhooks already migrate name-first (see the Tenant-to-Tenant Migration section).

Fallback (older catocli without these exports) — the raw queries behind them:

```bash
catocli query object globalIpRangeList '{"globalIpRangeListInput":{}}'
catocli query customAppData customApplicationList '{"customApplicationListInput":{"paging":{"from":0,"limit":1000}}}'
```

Note: the API key needs sufficient permissions for `customAppData` — a view-only key may get `Access denied` on that query.

### Importing Objects into Terraform State

`migration.tf` performs the one-shot adoption declaratively — no per-object `terraform import` commands needed:

- `import` blocks adopt the global-IP-ranges singleton (its importer ignores the ID and hydrates the whole account set), and every other type by the plain `id` carried in its JSON export (custom applications, both container types, mailing lists, subscription groups, webhooks, DHCP relay groups — the latter's `for_each` is naturally a no-op today since `dhcp_relay_groups.json` is an empty list).
- A `removed` block (with `destroy = false`) forgets the legacy `module.custom_applications` `terraform_data` anchors from state **without** running their destroy provisioners (which would have deleted the live apps).

After the first successful apply, `migration.tf` can be deleted — import/removed blocks are no-ops once state matches.

A separate, newer one-shot file, `migration-groups-handover.tf`, forgets `cato_group.this` from this folder's state (`destroy = false` — the live groups aren't touched) so `../3-groups/` can adopt them into its own state. Apply `../3-groups/` first (or in the same session), then this folder.

### Typical Brownfield Workflow

1. **Export existing objects:**
   ```bash
   catocli export global_ip_ranges --json-filename config_data/global_ip_ranges.json
   catocli export custom_apps --json-filename config_data/custom_applications.json
   ```

2. **Plan — expect imports only:**
   ```bash
   cd 0-dependencies && terraform plan
   ```
   A clean adoption shows `Plan: N to import, 0 to add, 0 to change, 0 to destroy.` Any "change" means the JSON is stale relative to the live tenant — **re-export rather than letting Terraform "fix" live objects to match an old backup.**

3. **Apply:**
   ```bash
   terraform apply -parallelism=1
   ```

4. **Verify:** `terraform plan` → `No changes.` Outputs now expose real Cato IDs for every object (including custom applications, which the old CLI wrapper could not track).

### Backup and Restore Workflow

If unintended changes are made directly in the Cato Management Application:

1. **Restore to last known good:** `terraform apply -parallelism=1` restores objects to match the committed JSON.
2. **Or re-export and diff:** re-run the exports and `git diff config_data/` to review what changed before deciding which side wins.

## Tenant-to-Tenant Migration

- Export from the source tenant with `--strip-ids` to drop tenant-specific IDs, then apply to the target tenant (skip `migration.tf` — greenfield there).
- Global IP ranges, custom applications, both container types, and DHCP relay groups carry no cross-tenant references and migrate as-is. Subscription groups reference mailing lists and webhooks by name, so they migrate cleanly as long as those come along.
- Mailing-list admin refs are id-only — re-resolve admin ids in the target tenant.
- Webhooks migrate name-first: `related_webhook_ref` is resolved to the referenced webhook's name in `main.tf`, so it carries across tenants without rewriting. What does NOT migrate: auth secrets (never exported — supply a `webhook_secrets` entry per webhook in the target's tfvars) and, until the provider gains `NO_AUTH` support, any `NO_AUTH` webhook lands with the `webhook_default_auth` placeholder header in the target too.
- Group migration (id-only member types, site-scoping) is documented in `../3-groups/README.md`.

## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.7.0 |
| cato | = 2.0.0-prerelease.1 |

## Providers

| Name | Version |
|------|---------|
| cato | = 2.0.0-prerelease.1 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [cato_global_ip_ranges.this](https://registry.terraform.io/providers/catonetworks/cato/latest/docs/resources/global_ip_ranges) | resource |
| [cato_custom_application.this](https://registry.terraform.io/providers/catonetworks/cato/latest/docs/resources/custom_application) | resource |
| [cato_fqdn_container.this](https://registry.terraform.io/providers/catonetworks/cato/latest/docs/resources/fqdn_container) | resource |
| [cato_ip_address_range_container.this](https://registry.terraform.io/providers/catonetworks/cato/latest/docs/resources/ip_address_range_container) | resource |
| [cato_mailing_list.this](https://registry.terraform.io/providers/catonetworks/cato/latest/docs/resources/mailing_list) | resource |
| [cato_subscription_group.this](https://registry.terraform.io/providers/catonetworks/cato/latest/docs/resources/subscription_group) | resource |
| [cato_webhook.this](https://registry.terraform.io/providers/catonetworks/cato/latest/docs/resources/webhook) | resource |
| [cato_dhcp_relay_group.this](https://registry.terraform.io/providers/catonetworks/cato/latest/docs/resources/dhcp_relay_group) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| cato_token_<source-profile>_dev | Cato API token with Edit permissions. Optional when cato_profile / CATO_PROFILE is used. | `string` | `null` | no |
| account_id_<source-profile>_dev | Cato account ID. Optional when cato_profile / CATO_PROFILE is used. | `string` | `null` | no |
| cato_profile | Optional catocli profile name (from ~/.cato/credentials) to source token, account_id, and endpoint from. | `string` | `null` | no |
| webhook_secrets | Auth secrets for webhooks, keyed by webhook name (matching `config_data/webhooks.json`); exactly one of `basic`/`bearer`/`custom` per entry. Set in a gitignored `*.tfvars`. | `map(object({...}))` | `{}` | no |
| webhook_default_auth | INTERIM placeholder auth for webhooks with no `webhook_secrets` entry (including every `NO_AUTH` webhook, which the provider can't express yet). | `object({...})` | `{ custom = { name = "X-Cato-Webhook-Placeholder", value = "placeholder" } }` | no |

## Outputs

| Name | Description |
|------|-------------|
| global_ip_range_ids | Map of global IP range name => Cato range ID |
| global_ip_range_names | Names of all managed global IP ranges |
| custom_application_ids | Map of custom application name => Cato application ID |
| custom_application_names | Names of all managed custom applications |
| fqdn_container_ids | Map of FQDN container name => Cato container ID |
| ip_range_container_ids | Map of IP range container name => Cato container ID |
| mailing_list_ids | Map of mailing list name => Cato mailing list ID |
| subscription_group_ids | Map of subscription group name => Cato subscription group ID |
| webhook_ids | Map of webhook name => Cato webhook ID |
| webhooks_using_placeholder_auth | Webhooks deployed with the interim `webhook_default_auth` placeholder (no real `webhook_secrets` entry supplied) |
| dhcp_relay_group_ids | Map of DHCP relay group name => Cato DHCP relay group ID |
| deployment_summary | Counts of managed dependency objects |
