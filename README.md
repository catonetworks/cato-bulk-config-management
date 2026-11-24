# Cato Bulk Config Management

Infrastructure-as-code for managing a [Cato Networks](https://www.catonetworks.com/)
SASE tenant's **sites, policies, and shared dependencies in bulk** — using
Terraform plus the Cato CLI (`catocli`) — instead of hand-writing one resource
per firewall rule or site.

Each policy or object type is managed as **one JSON file** (exported from the
live tenant with `catocli`) fed into a matching Cato bulk Terraform module. That
JSON file *is* the backup of the policy: commit it after every export and you
get a point-in-time, diffable, restorable copy of the whole configuration — and
the same JSON can be replayed into a different tenant to replicate it.

This package is fully **tenant-agnostic**: it ships with no account ids, profile
names, or data. You supply your own `config_data/` exports and credentials.

**Moving to a new account?** Start with the
[Getting Started Guide to Account Migration](docs/getting-started.md)
([one-page PDF](docs/getting-started-guide.pdf)): the two API keys to gather, the
commands that run the before-you-migrate check, and what to do with its report.

---

## 1. Provider requirement (read first)

Every folder pins the Cato Terraform provider to an **exact pre-release
version**:

```hcl
cato = {
  source  = "catonetworks/cato"
  version = "= 2.0.0-prerelease.1"
}
```

Version `2.0.0-prerelease.1` is **published to the public Terraform Registry**
and is resolved **directly from the registry** — there is **no filesystem
mirror and no `dev_overrides`**. Because it is a pre-release, Terraform only
selects it when a configuration pins it explicitly (which these folders do).

Make sure your Terraform CLI config resolves providers from the registry.
A minimal `~/.terraformrc` (or a file pointed at by `TF_CLI_CONFIG_FILE`) is:

```hcl
provider_installation {
  direct {}
}
```

If your personal `~/.terraformrc` contains `dev_overrides`, use a clean config
for these folders:

```bash
printf 'provider_installation {\n  direct {}\n}\n' > /tmp/clean.tfrc
export TF_CLI_CONFIG_FILE=/tmp/clean.tfrc
```

Requires Terraform `>= 1.7.0` (the dependency/migration folders use
`import`/`removed` blocks with `for_each`).

### Cato CLI requirement

The export/import workflows and the bundled scripts require the Cato CLI
(`catocli`) **version 3.3.0**:

```bash
pip install catocli==3.3.0
```

---

## 2. Repository layout and deploy order

Folders are numbered in dependency order — some object types reference others by
name, so they must be created first:

| Folder | Contents |
|---|---|
| `0-dependencies/` | Global IP ranges, custom applications, FQDN / IP-range containers, mailing lists, subscription groups, webhooks, DHCP relay groups, allocated IPs |
| `0-scim-users/` | SCIM user / user-group replication docs (driven by `scripts/scim_sync.py`; no Terraform) |
| `1-sites/` | Socket sites, network ranges, WAN interfaces, static hosts, vSocket / IPSec / interconnect placeholder sites |
| `2-app-connectors/` | ZTNA app connectors + private applications |
| `3-groups/` | Groups (members are site-scoped, so this deploys after sites) |
| `4-policies/*/` | 18 policy families, one subfolder each (internet-firewall, wan-firewall, wan-network, tls-inspection, lan-firewall, application-control, remote-port-forward, socket-bypass, sdp-static-ip-allocation, split-tunnel, site-web-proxy, terminal-server, anti-malware-file-hash, always-on, app-tenant-restriction, client-connectivity, dynamic-ip-allocation, private-access) |
| `config_data/` | **Shared, repo-root** JSON exports — one file per type (see `config_data/fixtures/` for the shapes) |
| `scripts/` | Automation: bulk apply, purge, SCIM sync, config validation, scrub check |
| `docs/` | Design docs and runbooks |

Deploy order is `0-dependencies` → (SCIM push) → `1-sites` → `2-app-connectors`
→ `3-groups` → `4-policies/*`. `scripts/bulk_apply.py`'s
`FOLDER_SEQUENCE` is the source of truth for the run order. Each folder is also
independently `terraform init`/`plan`/`apply`-able.

### Module sources (pinned registry versions)

Site and policy folders source a published `catonetworks/*/cato` bulk module,
pinned to an exact version. Each `main.tf` keeps a commented-out local-clone
`source` line beneath for module development.

| Folder | Module |
|---|---|
| `1-sites/` | [`catonetworks/socket-bulk-sites/cato`](https://registry.terraform.io/modules/catonetworks/socket-bulk-sites/cato) |
| `4-policies/internet-firewall/` | [`catonetworks/bulk-if-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-if-rules/cato) |
| `4-policies/wan-firewall/` | [`catonetworks/bulk-wf-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-wf-rules/cato) |
| `4-policies/wan-network/` | [`catonetworks/bulk-wnw-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-wnw-rules/cato) |
| `4-policies/tls-inspection/` | [`catonetworks/bulk-tls-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-tls-rules/cato) |
| `4-policies/lan-firewall/` | [`catonetworks/bulk-lan-fw-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-lan-fw-rules/cato) |
| `4-policies/application-control/` | [`catonetworks/bulk-app-control-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-app-control-rules/cato) |
| `4-policies/remote-port-forward/` | [`catonetworks/bulk-rpf-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-rpf-rules/cato) |
| `4-policies/socket-bypass/` | [`catonetworks/bulk-sb-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-sb-rules/cato) |
| `4-policies/sdp-static-ip-allocation/` | [`catonetworks/bulk-ssia-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-ssia-rules/cato) |
| `4-policies/split-tunnel/` | [`catonetworks/bulk-st-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-st-rules/cato) |
| `4-policies/site-web-proxy/` | [`catonetworks/bulk-swp-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-swp-rules/cato) |
| `4-policies/terminal-server/` | [`catonetworks/bulk-ts-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-ts-rules/cato) |
| `4-policies/anti-malware-file-hash/` | [`catonetworks/bulk-amfh-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-amfh-rules/cato) |
| `4-policies/always-on/` | [`catonetworks/bulk-ao-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-ao-rules/cato) |
| `4-policies/app-tenant-restriction/` | [`catonetworks/bulk-atr-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-atr-rules/cato) |
| `4-policies/client-connectivity/` | [`catonetworks/bulk-cc-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-cc-rules/cato) |
| `4-policies/dynamic-ip-allocation/` | [`catonetworks/bulk-dia-rules/cato`](https://registry.terraform.io/modules/catonetworks/bulk-dia-rules/cato) |
| `4-policies/private-access/` | [`catonetworks/bulk-private-access/cato`](https://registry.terraform.io/modules/catonetworks/bulk-private-access/cato) |

`0-dependencies/`, `2-app-connectors/`, and `3-groups/` use native
provider resources inline (no wrapper module).

---

## 3. Quickstart (standalone)

```bash
# 1. Clone this repo.
git clone <this-repo-url> cato-bulk-config-management
cd cato-bulk-config-management

# 2. Populate config_data/ with YOUR tenant's exports (one JSON per type).
#    See config_data/fixtures/ for a tenant-agnostic example of every file's
#    shape, and each folder's README for the exact `catocli export` command.
#    To try the shapes end to end first, point a folder at the fixtures:
mkdir -p config_data
cp config_data/fixtures/*.json config_data/   # or run catocli export ...

# 3. Provide credentials (see Authentication below). Either a tfvars file:
cd 0-dependencies
cp terraform.tfvars.example terraform.tfvars   # then edit
#    ...or a catocli profile (CATO_PROFILE=<profile>).

# 4. Standard per-folder workflow:
export TF_CLI_CONFIG_FILE=/tmp/clean.tfrc   # registry-direct (see section 1)
terraform init
terraform validate
terraform plan        # should show no changes if config_data matches the tenant
terraform apply
```

To drive every folder in order from one place, use the bulk orchestrator:

```bash
python3 scripts/bulk_apply.py --profile <profile> --config-dir ./config_data           # plan all
python3 scripts/bulk_apply.py --profile <profile> --config-dir ./config_data --mode apply
```

`--config-dir` is threaded to Terraform as `TF_VAR_config_data_dir` (absolute)
and to the validate / SCIM sub-steps, so a single flag redirects every folder's
`file()` reads at an external data set.

---

## 4. Authentication

Each folder's provider accepts credentials three ways; the token/account
variables are generic (`cato_token`, `account_id`) and default to `null`, and an
optional `cato_profile` wires catocli-profile auth into the provider.

**Option A — explicit credentials** (`terraform.tfvars`):

```hcl
cato_token = "your-api-token"   # sensitive
account_id = "your-account-id"
```

**Option B — catocli profile** (no tfvars needed):

```bash
CATO_PROFILE=<profile> terraform plan
```

The repo scripts always authenticate from a named catocli profile
(`~/.cato/credentials`) via `scripts/cato_profile.py` and never trust
shell-exported `TF_VAR_*`.

---

## 5. `config_data/` and fixtures

`config_data/` is shared at the repo root — one JSON export per type, referenced
as `../config_data/...` (top-level folders) or `../../config_data/...`
(`4-policies/*`) via a `config_data_dir` variable that each folder defaults to
its own relative path. Override it with `-var config_data_dir=/abs/path` or
`TF_VAR_config_data_dir` (that's what `--config-dir` sets).

The committed `config_data/fixtures/` directory holds a **tenant-agnostic
example of every file the folders read**, using RFC 5737 documentation IP ranges
(`192.0.2.0/24`, `198.51.100.0/24`), `example.com`, and placeholder ids. Use it
to see each file's shape and to smoke-test the stack before wiring real exports.
Everything else under `config_data/` is gitignored (point-in-time tenant data).

> `4-policies/wan-network/` and `4-policies/remote-port-forward/` additionally
> expose their own `*_json_file_path` variables (they remap allocated-IP display
> names per target tenant); they keep those explicit path vars rather than
> `config_data_dir`.

---

## 6. Scripts and the source-tenant guard

| Script | Purpose |
|---|---|
| `scripts/bulk_apply.py` | Plan/apply/deploy/destroy every folder in order |
| `scripts/export_source.py` | Exports the source account for a migration (`catocli export all`, static hosts on every site type, webhooks) and carries on past any type the key is refused for lack of permission, recording it in `config_data/export_report.json` for the check to report |
| `scripts/preflight_check.py` | Before-you-migrate dependency check: compares source and target (licences, PoPs, SCIM/IdP users and groups, CMA-only objects, features, factory rules) and lists what to enable or create first, plus a Cato-branded readiness report for the customer (HTML, `--pdf` for PDF). See [docs/preflight-check.md](docs/preflight-check.md) |
| `scripts/purge_all.py` + `scripts/purge/` | Clear all policies/dependencies from a TARGET tenant |
| `scripts/scim_sync.py` | Export/push/purge SCIM users + groups |
| `scripts/validate_config_data.py` | Lint `config_data/` before a plan/apply |
| `scripts/export_webhooks.py` | Export webhooks (with structural fields) to `config_data/` |
| `scripts/cato_profile.py` | Shared credential/profile helper |
| `scripts/scrub_check` | Fail if any tenant-specific identifier leaked into the tree |

All of them accept `--config-dir` (default `./config_data`).

**Source-tenant guard (configure your own).** The destructive scripts (purge,
SCIM push/purge, bulk apply) refuse to operate against a *protected* account or
profile. This package ships with an **empty** protected list — nothing is
protected until you declare it. Set your own guard in either place (they merge):

`cbcm.config.json` at the repo root (copy `cbcm.config.json.example`):

```json
{
  "protected_accounts": ["12345"],
  "protected_profiles": ["prod", "prod-readonly"]
}
```

…or environment variables (comma-separated):

```bash
export CBCM_PROTECTED_ACCOUNTS="12345,67890"
export CBCM_PROTECTED_PROFILES="prod,prod-readonly"
```

A protected account/profile can never be purged or SCIM-pushed (no override
flag), and `bulk_apply.py` requires `--source-tenant-apply` to apply to it.

Purge and SCIM push/purge default to a dry run; `--execute` requires an
interactive TTY and a typed confirmation of the resolved account id.

---

## 7. Before publishing / in CI

```bash
scripts/scrub_check                    # must exit 0 (no tenant identifiers)
python3 scripts/validate_config_data.py --config-dir config_data/fixtures
terraform fmt -recursive -check
```

---

## 8. Brownfield state adoption

Some folders ship a `migration.tf.parked` file: native Terraform `import`
blocks that adopt existing live objects into state. Rename it to `migration.tf`
to activate, run `terraform plan` (a clean adoption reads `N to import, 0 to
add, 0 to change, 0 to destroy` — any "change" means your JSON is stale,
re-export rather than overwrite the live tenant), apply once, then delete it.
`scripts/bulk_apply.py --park-migrations` / `--unpark-migrations` toggle them in
bulk. See each folder's README for per-type import-id formats.
