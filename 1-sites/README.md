# Sites

## What this manages

Cato site resources for every site type this account uses, split across one Terraform file per type:

| File | Site type | Resource(s) |
|---|---|---|
| `socket_sites.tf` | Physical socket sites (X1500, X1600, X1600_LTE, X1700) | full bulk module — site + WAN/LAN interfaces + LAG + network ranges + Wi-Fi SSIDs |
| `vsocket_sites.tf` | vSocket/cloud sites (AWS/Azure/ESX) | `cato_socket_site` — site shell (native range) only, **placeholder** |
| `ipsec_sites.tf` | IPSec sites | `cato_ipsec_site` — site + empty primary endpoint, **placeholder** |
| `interconnect_sites.tf` | Cloud Interconnect sites | `cato_cloud_interconnect_site` — site metadata only, **placeholder** |

`static_hosts.tf` carries the socket-site static hosts (brownfield-adopted alongside socket sites, not a separate site type).

Physical socket sites still get the full, deep-build module — that part is unchanged. The `<target-profile>` account currently has 4 socket sites live, exported to the shared repo-root `config_data/socket_sites.json` — this is a brownfield folder, like the policy folders. The other three types are newer, lighter-weight **placeholder** builds (see "Site types" below) driven from their own JSON files; each is optional — if its JSON file doesn't exist, that file's `for_each` is empty and the plan is clean.

**Export** runs from the **repo root** (catocli's default `--output-directory` is `config_data`, so running from root lands the file in the right place with no extra flags). **Import and Terraform commands** run from **this folder** (`1-sites/`), one level below the shared repo-root `config_data/`.

## Site types

**PLACEHOLDER** means: the shell exists so that cross-tenant references resolve — group `SITE`-membership and policy rules that reference the site by name — but nothing deeper. Deployment/registration, HA secondaries, tunnel configuration, bandwidth-management licenses, and per-site named network ranges/sub-interfaces are **not** created by the placeholder and remain manual/follow-up steps. In particular, group members that reference objects *inside* a placeholder site (`SITE_NETWORK_SUBNET`, `HOST`, `NETWORK_INTERFACE`) do **not** resolve until that site gets a full build.

- **`vsocket_sites.tf`** — vSocket/cloud sites, fed from `../config_data/vsocket_sites.json` (`catocli export vsocket_sites`). `resource "cato_socket_site" "vsocket"`, `for_each` over the JSON's `sites[]`. The export's snapshot `connType` (legacy values like `VSOCKET_VGX_AWS`/`VSOCKET_VGX_AZURE`/`VSOCKET_VGX_ESX`) is mapped to the provider's creatable `SiteConnectionTypeEnum` (`SOCKET_AWS1500`/`SOCKET_AZ1500`/`SOCKET_ESX1500`) — the provider validator only accepts the 8 `SOCKET_*` values, not the snapshot's legacy `VSOCKET_*` names.
- **`ipsec_sites.tf`** — IPSec sites, fed from `../config_data/ipsec_sites.json` (`catocli export ipsec_sites`). `resource "cato_ipsec_site" "this"`. Creates the site plus an empty `ipsec = { primary = {} }` endpoint only; tunnels (PSKs, peer IPs, IKE crypto) are added later/manually.
- **`interconnect_sites.tf`** — Cloud Interconnect sites, fed from `../config_data/interconnect_sites.json` (`catocli export interconnect_sites`). `resource "cato_cloud_interconnect_site" "this"`. Site metadata only; physical connections and BGP peers are separate, later steps.

Adopting a placeholder site into a fuller build later (e.g. once a vSocket is actually deployed and registered) is a deferred/future discussion — no import procedure is documented yet for that transition.

## Module

[`catonetworks/socket-bulk-sites/cato`](https://registry.terraform.io/modules/catonetworks/socket-bulk-sites/cato/latest) v0.0.14 — pinned in `socket_sites.tf` (moved here verbatim from `main.tf`; state address `module.sites` is unchanged). Supports JSON **or** CSV input; this folder is set up for JSON (`sites_json_file_path`) to stay consistent with the four policy folders. CSV is available via `sites_csv_file_path` + `sites_csv_network_ranges_folder_path` if you'd rather manage sites spreadsheet-style — see the module README for that layout. The three placeholder site types above are JSON-only for now (no module, no CSV support) — native provider resources declared directly in their own file.

## Authentication

Credentials resolve in precedence order: explicit vars (`terraform.tfvars`) → `CATO_TOKEN`/`CATO_BASEURL` environment variables → a catocli profile (`cato_profile` var or `CATO_PROFILE` env) read from `~/.cato/credentials`. With a profile, no tfvars are needed at all:

```bash
CATO_PROFILE=<target-profile> terraform plan
```

Requires the published provider `2.0.0-prerelease.1` from the Terraform Registry. Verify which tenant a profile targets with `catocli configure show` before planning.

## JSON schema

The exported `socket_sites.json` file is a flat site-configuration structure (not the `data.policy.*` shape used by the rule modules) — see the [module's sample JSON template](https://github.com/catonetworks/terraform-cato-socket-bulk-sites/blob/main/templates/socket_sites.json) for the exact layout once you've generated your own export.

`vsocket_sites.json`, `ipsec_sites.json`, and `interconnect_sites.json` are each a simple `{ "sites": [...] }` wrapper (name, description, type, native range, site location) — much lighter than the socket shape above since they only need to carry what a placeholder actually creates. JSON-only for now; CSV support for these three is deferred.

## Export (backup) workflow

Run from the repo root:

```bash
catocli export socket_sites --json-filename socket_sites.json
catocli export vsocket_sites --json-filename vsocket_sites.json
catocli export ipsec_sites --json-filename ipsec_sites.json
catocli export interconnect_sites --json-filename interconnect_sites.json
```

This overwrites the corresponding file under `config_data/`. Re-run any of these any time to refresh that backup. The three placeholder exports are optional — skip whichever ones don't apply to this account; a missing file just means an empty `for_each` on the next plan.

### Known export gaps — fill in by hand before applying

The Cato API's read path doesn't currently return these fields, so they'll be **missing** from the export and must be populated manually in the JSON before the first `terraform apply`:

- `lag_min_links`
- `dhcp_type`
- `ip_range`
- `relay_group_id` / `relay_group_name`
- `gateway`
- `translated_subnet`
- `internet_only`
- `local_ip`
- `range_type`

## Update workflow

1. Edit the JSON directly, or re-export and re-apply the manual fixups above.
2. `terraform plan` — review carefully; site/interface changes can affect live connectivity.
3. `terraform apply`.
4. Commit the updated JSON.

## Outputs

| Name | Description |
|------|-------------|
| socket_sites | Map of all socket site resources by site name (pass-through of `module.sites.socket_sites`) |
| wan_interfaces | Map of WAN interfaces by socket site name |
| network_ranges | Map of network ranges by socket site name |
| vsocket_sites | Map of vSocket/cloud placeholder sites, name => site id |
| ipsec_sites | Map of IPSec placeholder sites, name => site id |
| interconnect_sites | Map of Cloud Interconnect placeholder sites, name => site id |

## Brownfield adoption

This section covers physical socket sites (`socket_sites.tf`/`module.sites`) — the deep build with WAN/LAN interfaces, LAG members, network ranges, and Wi-Fi SSIDs. Run from this folder. If using CSV, validate site-location syntax (country/state/city/timezone) before touching Terraform state at all — this catches typos that would otherwise surface as a confusing apply-time error:

```bash
catocli import validate_site_location ../config_data_csv/socket_sites.csv -f=csv
```

Two adoption paths are supported; **Option A (native import blocks) is the recommended best practice** — declarative and previewable in `terraform plan` before state is touched. This folder already uses it for static hosts and Wi-Fi SSIDs (`migration.tf` / `static_hosts.tf`).

<details>
<summary>Option A (recommended) — native Terraform import blocks (migration.tf)</summary>

Already implemented in this folder for the objects it defines directly:

- **Static hosts** — `import` blocks over `config_data/static_hosts.json`, composite id `"<site-id>/<host-id>"`, addresses `cato_static_host.this["<site>:<host>"]`.
- **Wi-Fi SSIDs** — `import` blocks over `sites[].wifi_ssids[]`, plain SSID id, addresses `module.sites.module.socket-site["<site>"].cato_wifi_ssid.this["<ssid>"]`.

Preview with `terraform plan` (`N to import, 0 to add, 0 to change, 0 to destroy`; any "change" = stale JSON — re-export instead of overwriting the live tenant), adopt with `terraform apply`, then delete `migration.tf`. For the sites' deep internals (socket site, WAN/LAN interfaces, LAG members, network ranges), the full import-block example lives in the socket-bulk-sites module README; Option B below remains the most battle-tested path for those today.
</details>

<details>
<summary>Option B — catocli import (imperative alternative)</summary>

Validate the file has all required fields and do a dry run before actually importing:

```bash
# Dry run -- checks the file, doesn't touch state
catocli import socket_sites_to_tf --module-name module.sites --json-file="../config_data/socket_sites.json" --validate

# Real import (covers sites, WAN/LAN interfaces, LAG members, network ranges, wifi SSIDs)
catocli import socket_sites_to_tf --module-name module.sites --json-file="../config_data/socket_sites.json"
```

Shells `terraform import` per resource — state is mutated immediately as it runs, with no plan preview.
</details>

Follow with `terraform plan` — expect no changes once the manual fixups above are applied; any remaining diff is likely one of the known export gaps.

## Troubleshooting

- `catocli ping` / `catocli configure show` for credential issues; `catocli entity site list` to confirm the CLI can actually see your sites.
- Missing network range files (CSV mode only): ensure each site has a corresponding `{site_name}_network_ranges.csv`, or a custom filename set via the `network_ranges_csv_filename` column.
- DHCP relay issues: confirm `relay_group_id`/`relay_group_name` actually exists in the account — this is one of the fields not populated by export.
- LAG interfaces: use `LAN_LAG_MEMBER` for `lan_interface_dest_type` on LAG members; they don't take a subnet.
- **"Resource already exists in state"** on import: something's already tracked under that address. Inspect with `terraform state list` / `terraform show`, then `terraform state rm 'module.sites.cato_socket_site["Your Site Name"]'` and re-import.
- **`terraform plan` shows unexpected changes** (e.g. `Plan: 0 to add, 25 to change, 0 to destroy`) after everything should be in sync: re-export to a scratch directory and diff against the committed file rather than guessing —

  ```bash
  catocli export socket_sites --json-filename socket_sites.json --output-directory=config_data_verify
  diff config_data/socket_sites.json config_data_verify/socket_sites.json
  ```

  (run from the repo root, same as the main export command)
