# Release instructions — repos, versions, tags (prepared 2026-11-04)

Everything below sits **uncommitted** in local working trees. Per-repo changelogs have
been updated with the exact version to tag. Order matters: provider → modules → CLI →
reference repo re-pins.

## New in this batch

- The 14 new `2-*` folders (lan-firewall, application-control, remote-port-forward,
  socket-bypass, sdp-static-ip-allocation, split-tunnel, site-web-proxy,
  terminal-server, anti-malware-file-hash, always-on, app-tenant-restriction,
  client-connectivity, dynamic-ip-allocation, private-access) consume local module
  clones for their first release and should switch to registry pins at `v0.0.1` once
  each module has its first registry release (see section 2's table).
- Six of those (anti-malware-file-hash, always-on, app-tenant-restriction,
  client-connectivity, dynamic-ip-allocation, private-access) additionally require the
  next terraform-provider-cato release (`feat/bulk-policy-families`) before they can
  move off local provider builds (see section 1).
- `scripts/export_all_configs.py` and `scripts/validate_config_data.py` have been
  extended to cover all 14 families plus the new `dhcp_relay_groups` dependency type;
  the latter degrades to a warning (not a failure) for any of these 15 files until it
  actually exists in `config_data/`.

## 0. Repos you must CREATE on GitHub first

> **UPDATE 2026-11-05: stale — both repos already exist upstream.** The
> publish-readiness sweep confirmed `catonetworks/terraform-cato-bulk-ao-rules`
> and `catonetworks/terraform-cato-bulk-atr-rules` exist on GitHub (verified via
> the GitHub API; local `origin` remotes are configured). Nothing to create —
> only push + tag remain. Full per-module checklist:
> `docs/module-publish-readiness.md`.

| Local clone | Action |
|---|---|
| `terraform-cato-bulk-ao-rules` | ~~No `origin` remote~~ repo exists — push `main`, tag |
| `terraform-cato-bulk-atr-rules` | ~~No `origin` remote~~ repo exists — push `main`, tag |

All other module repos already have an `origin` under `github.com/catonetworks/`.

## 1. Provider (prerequisite for 12 of the 14 new folders)

> **UPDATE 2026-11-05: `v1.5.0-prerelease.2` is published on the Terraform Registry**
> (26 commits ahead of prerelease.1: the policy-family resources, per-family
> `cato_bulk_*_move_rule` movers, `cato_pa_section`, the `*_policy` enable/disable
> toggles, SDK v1.1.4 — i.e. the consolidated-branch content below, released). The
> reference repo's 20 folders and the socket-bulk-sites module pin are now bumped to
> `= 1.5.0-prerelease.2`, and the 6 provider-gated folders are unblocked against the
> published registry build (no dev_overrides needed). Which side of the RPF/AMFH/SWP
> naming-conflict pairs shipped is confirmed by the folder revalidation results.
> The branch-build instructions below are retained for history.

`terraform-provider-cato` 1.5.0-prerelease.1 is already published, but it lacks the
bulk-move resources for **apc, rpf, sb, ssia, st, swp, ts, amfh, atr, cc, dia** plus the
policy toggles and `cato_site_web_proxy_rule` — all on the unmerged
`feat/bulk-policy-families` branch. Of the 14 new folders, only **3-policies/lan-firewall**
and **3-policies/private-access** run against the published prerelease today.

**Branch-build verification (2026-11-04):** all 12 gated folders were tested against a
local dev_overrides build of `origin/feat/bulk-policy-families` (479afb6) — every one
plans successfully (per-folder results in each README). Two fixes were made during that
test: `terraform-cato-bulk-ao-rules` renamed its mover from the never-shipped
`cato_bulk_ao_move_rule` to the real `cato_always_on_rule_bulk`, and
`3-policies/site-web-proxy/migration.tf` gained the rule import (the branch's
`cato_site_web_proxy_rule` DOES support import). To re-run the test:
`cd terraform-provider-cato && git checkout test/bulk-families && make install && tfsync dev`
(then `tfsync mirror` + re-checkout `v1.5.0-prerelease.1` when done; while in dev mode
do NOT plan 0-sites/1-dependencies/the four original 3-policies/* folders — the branch build
lacks wifi/custom-app/container/mailing/subscription resources).

**Before releasing the branch:** it is 158 commits behind the prerelease tag — rebase/
merge onto main first, and resolve the naming conflicts it introduces
(`cato_remote_port_fwd_*` vs the published `cato_remote_port_forward_*`;
`cato_pa_section` vs `cato_private_access_section`). Note the bulk-rpf-rules module is
written against the BRANCH names. Also fix before/with the release: the PA
`applications` wrapper-shape mismatch between `catocli export pa_rules` and the
bulk-private-access module (documented in 3-policies/private-access/README.md), and ideally
provider issues #1–#5 in docs/provider-issues-draft.md.

**Consolidated build (2026-11-04, same night):** the merge above has been performed
locally — provider branch `feat/bulk-policy-families-consolidated` holds
`v1.5.0-prerelease.1` merged with `origin/feat/bulk-policy-families` (479afb6) under a
union registration strategy: both RPF/AMFH/SWP naming families registered side-by-side
(see the MERGE NOTE comments in `internal/provider/provider.go`), the branch's
socket-bypass rule/section implementations superseded by the tag's, SDK pinned at the
branch's v1.1.4, and one orphaned branch-side test removed
(`sbrule/converters_socket_bypass_rule_test.go`, which targeted the dropped converter).
The merge is fully staged but **deliberately uncommitted** (`.git/MERGE_HEAD` intact) so
the merge commit can be reviewed and made manually. The build passes `go build` and the
full unit suite and is installed as the active dev_overrides binary. Plan-verified with
<source-profile> credentials: `1-dependencies` gives the clean `26 to import, 0 add, 0 change,
0 destroy` (prerelease resources intact), and **all 14** new 2-* folders plan
successfully — the dev-mode "do NOT plan 0-sites/1-dependencies" restriction above
applies only to the branch-only test build, not this consolidated one. The PA
`applications` wrapper-shape mismatch is now fixed module-side (bulk-private-access
accepts both the wrapper-object and flat-list shapes). Two observations for the release
review: (a) `cleanupDrafts` in provider.go fires a `policyPrivateAccessDiscardRevision`
mutation on first Configure — pre-existing in both tag and branch (skippable via
`DISABLE_POLICY_RULE_CLEANUP=true`), but it mutates the tenant during plan and can
discard a human's in-progress CMA PA draft; (b) decide whether to keep both RPF/AMFH/SWP
naming families or deprecate one before tagging.

## 2. Terraform modules — version per repo

Branch notes: `bulk-if-rules`, `bulk-wf-rules`, `bulk-app-control-rules`,
`bulk-private-access` are checked out on `feat/bulk-coverage` — merge to `main` (or tag
from the branch per your convention) before tagging.

| Repo | Current tag | **Tag to publish** | Notes |
|---|---|---|---|
| terraform-cato-bulk-if-rules | v0.0.8 | **v0.0.9** | guard + dual-option import docs (+ whatever feat/bulk-coverage carries) |
| terraform-cato-bulk-wf-rules | v0.0.9 | **v0.0.10** | same |
| terraform-cato-bulk-wnw-rules | v0.0.1 | **v0.0.2** | guard + docs |
| terraform-cato-bulk-tls-rules | v0.0.1 | **v0.0.2** | guard + docs |
| terraform-cato-socket | v0.0.14 | **v0.0.15** | cato_wifi_ssid + passkeys var, lag_min_links fix, provider pin |
| terraform-cato-socket-bulk-sites | v0.0.13 local (0.0.14 on registry) | **v0.0.15** | wifi passthrough, allow_empty_sites, internet_only parity, README overhaul — **skip 0.0.14**, already published |
| terraform-cato-network-ranges-bulk | v0.0.12 | *(no release — unchanged)* | |
| bulk-lan-fw-rules, bulk-app-control-rules, bulk-amfh-rules, bulk-ao-rules, bulk-atr-rules, bulk-cc-rules, bulk-dia-rules, bulk-private-access, bulk-rpf-rules, bulk-sb-rules, bulk-ssia-rules, bulk-st-rules, bulk-swp-rules, bulk-ts-rules | *(never tagged)* | **v0.0.1** | first release; also first-time Terraform Registry publish (registry picks up the repo once the tag exists and the module is added via the registry UI/GitHub app) |

Per-repo flow (after your review):

```bash
cd /Users/Shared/github.com/catonetworks/<repo>
git add -A && git commit -m "release <version>"   # your commit, your message style
git tag v<version>
git push origin main --tags                       # (merge feat/* first where applicable)
```

Caveat: `bulk-swp-rules` v0.0.1 ships sections-only in practice — `cato_site_web_proxy_rule`
is held back upstream pending the API addRule fix (noted in its changelog/README).

## 3. cato-cli

- Version bumped in `catocli/__init__.py`: **3.2.4 → 3.3.0** (changelog entry added).
- Currently on branch `feat/bulk-policy-families` — merge per your normal flow.
- Publish per the repo's usual PyPI process (`pip3 install catocli` consumers pick up
  the 8 new export subcommands, filters, merge mode, wifi support).

## 4. Reference repo follow-ups (after modules are on the registry)

1. `0-sites/main.tf`: restore the registry source + pin `version = "0.0.15"` for
   `catonetworks/socket-bulk-sites/cato` (the local-path source + commented registry
   lines are in place for exactly this swap).
2. Bump the four `2-*` folders' module pins when you want the `allow_empty_policy`
   guard active there (if 0.0.9, wf 0.0.10, wnw 0.0.2, tls 0.0.2), and set
   `allow_empty_policy = false` explicitly if you want it visible in config.
3. Provider GA has shipped (`2.0.0`): all six folders now pin `= 2.0.0`.
4. Remove `~/.terraform.d/mirror/.../1.5.0-prerelease.1` only after the registry path
   is confirmed working in `tfsync prod` mode (the mirror copy IS the published
   artifact, so there's no rush).

## 5. Apply/adoption sequence for <source-profile> (your runs, any time)

```bash
cd 1-dependencies && terraform plan   # expect: 26 to import, 0/0/0
terraform apply -parallelism=1        # then delete migration.tf

cd ../0-sites && terraform plan       # expect: 88 to import + known provider-gap diffs
                                      # (see docs/provider-issues-draft.md: 3 replaces,
                                      #  3 cosmetic changes — review before applying)
terraform apply -parallelism=1        # then delete migration.tf
```

## 6. profile-auth + data-sources + docs batch (prepared 2026-11-09)

This batch sits **on top of** the consolidated build in section 1. It is a second
wave of provider/SDK/CLI work done against the replication pipeline; everything
is uncommitted in local working trees as usual. **Do not tag/publish the provider
until the regression blocker below is cleared.**

### Branch topology (verified 2026-11-09)

- **Provider** is on `feat/profile-auth`, which is **4 commits ahead of
  `feat/bulk-policy-families-consolidated` and 0 behind** (a clean linear
  descendant — merge-base `20caa031f`). So the port is NOT a cross-branch
  cherry-pick: commit the uncommitted work on `feat/profile-auth`, then the
  consolidated release branch fast-forwards (or `feat/profile-auth` becomes the
  release line). The 4 already-committed deltas are: `fix: clear removed split
  tunnel DNS exclusions`, `test: account for empty source on import`, `fix:
  preserve configured empty policy filters`, `fix: allow full resource registry
  length`.
- **cato-go-sdk** is on `fix/socketlan-access-selection` (28 files modified,
  last tag `v1.1.4`). ⚠ Branch-hygiene: the subscription-group query trim below
  lives in these uncommitted edits but on a socketlan-named branch — decide the
  correct home branch before committing.
- **cato-cli** is on `feat/bulk-policy-families-consolidated` (174 files, last
  tag `v3.2.4`; section 3 already calls for `3.3.0`).

### Provider — uncommitted content to land (`feat/profile-auth`, builds as `1.5.0-prerelease.3`)

- **catocli-profile authentication** — `profile` provider attribute +
  `CATO_PROFILE`/`CATO_CREDENTIALS_FILE` resolution (the whole reason this branch
  is named profile-auth); documented in the regenerated `docs/index.md`.
- **9 read-only per-type data sources** (new packages under
  `internal/provider/*/data_sources/`): `cato_sites`, `cato_users` (+
  `import_type_filter`, OneOf INTEGRATION/LDAP/MANUAL/SCIM), `cato_users_group`
  (+ OneOf incl. DYNAMIC), `cato_subscription_groups`, `cato_mailing_lists`,
  `cato_global_ip_ranges`, `cato_custom_applications`, `cato_fqdn_container`,
  `cato_ip_address_range_container` — all registered in `provider.go`,
  live-verified against <source-profile> (sites 14, users 460, user_groups 25, sub_groups
  4, mailing_lists 1, global_ip_ranges 9, custom_apps 13, fqdn 2, ip 1).
  - `cato_sites` **must paginate** (`limit=1000` + `from`): the API returns 0 for
    `entityLookup(type=site, limit=0)` while host treats 0 as "all" — do not
    "simplify" it back to the host pattern.
- **`cato_static_host` accepts `site_name`** (ExactlyOneOf with `site_id`;
  entityLookup resolve; site_id Computed) — tenant-portable host adoption.
- **`cato_socket_site` strips `*_AND_VRRP`** LAN interface types on create (API
  rejects them pre-HA); native-range path only — laninterface gap still open.
- **tfplugindocs support** — `make docs` (pinned `tfplugindocs@v0.25.0`) already
  existed; added `make docs-validate` (+ `.PHONY`), examples for all 9 data
  sources under `examples/data-sources/`, removed `subcategory` from
  `templates/index.md.tmpl` (validate disallows it on the provider index page).
  `make docs-validate` passes clean. Regenerate with `make docs` (NOT a PATH
  `tfplugindocs` — a different version churns cosmetic import-boilerplate across
  ~14 resource docs).

### cato-go-sdk — uncommitted content (consumed via `replace` → local path)

- Trimmed the `integration.webhookList{auth,headers,body,…}` subtree from the
  `notificationSubscriptionGroupList` + `NotificationDocument` List queries
  (both `sources/*.gql` and generated `client.go`) — the API returns "Response
  is not valid" for that subtree on tenants with webhook integrations (same root
  issue as the catocli `deleteSubscriptionGroup` fix). Without this,
  `cato_subscription_groups` errors.
- **Release dependency:** the provider pins the SDK via `replace =>` the local
  clone + `require v1.1.4`. For a REGISTRY provider release the SDK must be
  tagged (e.g. `v1.1.5`), the provider's `go.mod` bumped, and the `replace`
  removed. Until then the provider only builds from the local mirror.

### cato-cli — uncommitted content (branch `feat/bulk-policy-families-consolidated`)

- `deleteSubscriptionGroup` operationFieldExclusions fix (scoped to the broken
  call; applied via the auto-regen path, `schema/catolib.py --schema-only`).
- `export_static_hosts --site-type {physical,cloud,all}` filter.
- `scim_sync` user-query export source (`--source query`, full importType
  coverage) — reference-repo tooling, not catocli itself.
- Section 3's `3.2.4 → 3.3.0` bump covers this wave too.

### Release sequence for this batch (your runs)

1. **Fix the regression blocker first** (see `docs/prerelease3-regression-findings.md`
   / memory): null/state_code inconsistent-after-apply ×6, group data-source
   merge gap, connection_origins enum drift. Caught by the provider-tests
   regression suite (tftest-prod <TEST_ACCOUNT_ID>). **Do not tag `1.5.0-prerelease.3` until
   these are resolved.**
2. Commit SDK trims on the right branch → tag `cato-go-sdk v1.1.5` → bump
   provider `go.mod` + drop the local `replace`.
3. Commit provider `feat/profile-auth` work → fast-forward
   `feat/bulk-policy-families-consolidated` → tag/publish `1.5.0-prerelease.3`
   (registry).
4. Commit + release cato-cli `3.3.0`.
5. Reference repo: the 18 policy folders + `0-sites`/`1-dependencies`/`2-groups`
   now pin `= 2.0.0` (GA, published to the Terraform Registry).
