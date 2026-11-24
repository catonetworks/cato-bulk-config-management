# 3-groups — Bulk Groups

This folder bulk-manages **Groups** — the one dependency object type that is
scoped to live sites (`SITE` / `SITE_NETWORK_SUBNET` members), and therefore
must apply *after* `1-sites/` rather than alongside the other dependency
types in `0-dependencies/`. It was split out of `0-dependencies/` on
2026-11-06 for exactly this reason — see `docs/bulk-apply-runbook.md` and
CLAUDE.md's Structure section for the full deployment-order rationale. Uses
the native `cato_group` provider resource only (provider `1.5.0-prerelease.1`+).
No wrapper module and no CLI/python provisioners: a plain `for_each` over
`groups.json`, inline in `main.tf` — identical resource block to the one
`0-dependencies/main.tf` carried before the split.

Conventions (shared with every other bulk folder in this repo):
- **State is indexed by name** — `for_each` is keyed by the group's name, so
  addresses stay stable across greenfield creation, brownfield import, and
  tenant-to-tenant migration.
- **Members reference related objects by name**, not ID, wherever the
  provider resolves names. IDs appear only in `migration.tf`'s import blocks
  and for the member types that cannot resolve by name yet.

Account: **<target-profile> (<account-id>)**. Live inventory at adoption time: 4 groups.

## Usage

The folder reads one JSON configuration file from the shared `../config_data/` directory:

- **Groups**: `groups.json` — one `cato_group` per element

### Basic Example

```bash
cd 3-groups
cp terraform.tfvars.example terraform.tfvars   # fill in token + account id (gitignored)
terraform init
terraform plan
terraform apply -parallelism=1   # Cato API requires sequential execution
```

### JSON Configuration Structure

`groups.json` is a top-level JSON array, refreshed from the live tenant via `catocli export` (see Brownfield section).

#### Key Fields

**`groups.json`** — `[{id, name, description, members: [{type, id, name}]}]`
- `id` — Cato group ID; used only by `migration.tf` import blocks, not by the resource config.
- `members[].type` — one of `FLOATING_SUBNET`, `GLOBAL_IP_RANGE`, `HOST`, `NETWORK_INTERFACE`, `SITE`, `SITE_NETWORK_SUBNET`.
- `members[]` carries **both `id` and `name`**: the provider resolves by `id` when both are set (id wins), and `name` rides along so these configs become tenant-portable once name-based resolution ships for the member types that are id-only today (`FLOATING_SUBNET`, `HOST`, `NETWORK_INTERFACE`, `SITE_NETWORK_SUBNET`).
- `cato_group` requires ≥1 member — a zero-member group cannot be managed here.
- Live groups in this account are **site-scoped** (`SITE` / `SITE_NETWORK_SUBNET` members), which is why this folder deploys after `1-sites/` in the repo's fixed order — a group member referencing a site that doesn't exist yet fails.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0-prerelease.1` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## Working with Existing Objects (Brownfield Deployments)

For brownfield tenants where groups already exist in the Cato Management Application, export them with the Cato CLI and adopt them into Terraform state with the `import` blocks in `migration.tf`.

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

Run from the repo root:

```bash
catocli export groups --json-filename config_data/groups.json
```

Supports `--strip-ids` for tenant-to-tenant migration (drops tenant-specific ids; see Tenant-to-Tenant Migration below).

Fallback (older catocli without this export) — the raw queries behind it:

```bash
catocli query groups groupList '{"groupListInput":{"paging":{"from":0,"limit":1000}}}'
catocli query groups group members '{"groupMembersListInput":{"filter":[],"paging":{"from":0,"limit":1000}},"groupRefInput":{"by":"ID","input":"<group id>"}}'
```

### Importing Objects into Terraform State

`migration.tf` performs the one-shot adoption declaratively — no per-object `terraform import` commands needed:

- An `import` block adopts every group, keyed by the `id` in `groups.json`.
- The companion handover lives on the `0-dependencies/` side: a one-shot
  `0-dependencies/migration-groups-handover.tf` carries the `removed` block
  (`destroy = false`) that forgets `cato_group.this` from that folder's state
  **without** deleting the live groups — see that file's header comment.

After the first successful apply here (and in `0-dependencies/`), both
one-shot migration files can be deleted — import/removed blocks are no-ops
once state matches.

### Typical Brownfield Workflow (handover from 0-dependencies)

1. **Export the current groups:**
   ```bash
   catocli export groups --json-filename config_data/groups.json
   ```

2. **Plan this folder — expect imports only:**
   ```bash
   cd 3-groups && terraform plan
   ```
   A clean adoption shows `Plan: N to import, 0 to add, 0 to change, 0 to destroy.` Any "change" means the JSON is stale relative to the live tenant — **re-export rather than letting Terraform "fix" live objects to match an old backup.**

3. **Apply here first:**
   ```bash
   terraform apply -parallelism=1
   ```

4. **Then plan/apply `0-dependencies/`** to run its `removed` block — expect `Plan: 0 to add, 0 to change, 0 to destroy` there (a pure forget, no API calls against the groups themselves).

5. **Verify:** `terraform plan` → `No changes.` in both folders.

### Backup and Restore Workflow

If unintended changes are made directly in the Cato Management Application:

1. **Restore to last known good:** `terraform apply -parallelism=1` restores groups to match the committed JSON.
2. **Or re-export and diff:** re-run the export and `git diff config_data/groups.json` to review what changed before deciding which side wins.

## Tenant-to-Tenant Migration

- Export from the source tenant with `--strip-ids` to drop tenant-specific IDs, then apply to the target tenant (skip `migration.tf` — greenfield there).
- Group members referencing `FLOATING_SUBNET` / `HOST` / `NETWORK_INTERFACE` / `SITE_NETWORK_SUBNET` are id-bound today (all other member types are referenced by name already); until the provider's name-based member resolution lands for those types, their member ids must be re-resolved in the target tenant (`catocli query entityLookup` by name/type). In this account's live data, members are site-scoped (`SITE_NETWORK_SUBNET` by id, `SITE` by name) — apply this folder only after the target's `1-sites/` has been adopted/created.

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
| [cato_group.this](https://registry.terraform.io/providers/catonetworks/cato/latest/docs/resources/group) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| cato_token_<source-profile>_dev | Cato API token with Edit permissions. Optional when cato_profile / CATO_PROFILE is used. | `string` | `null` | no |
| account_id_<source-profile>_dev | Cato account ID. Optional when cato_profile / CATO_PROFILE is used. | `string` | `null` | no |
| cato_profile | Optional catocli profile name (from ~/.cato/credentials) to source token, account_id, and endpoint from. | `string` | `null` | no |

## Outputs

| Name | Description |
|------|-------------|
| group_ids | Map of group name => Cato group ID |
| group_names | Names of all managed groups |
| deployment_summary | Counts of managed group objects |
