# Provider 1.5.0-prerelease.2 revalidation — 2026-11-05

All 20 folders bumped from `= 1.5.0-prerelease.1` to `= 1.5.0-prerelease.2`
(plus the `terraform-cato-socket-bulk-sites` and `terraform-cato-socket`
module pins, which must move in lockstep — two different exact prerelease
pins are unsatisfiable together). Every folder was then
`terraform init -upgrade` / `validate` / `plan`'d against <source-profile> via
`scripts/bulk_apply.py --profile <source-profile> --mode plan`, under a clean
`TF_CLI_CONFIG_FILE` so the **published registry build** was exercised —
not the local dev_overrides binary in `~/.terraformrc`.

**Headline: 20/20 folders init, validate, and plan against the published
prerelease.2.** The six formerly provider-gated folders (always-on, AMFH,
ATR, client-connectivity, DIA, private-access) no longer need a local
provider build.

## Plan results (after refreshing exports — see below)

| Folder | add | chg | del | import | Classification |
|---|---|---|---|---|---|
| 1-dependencies | 0 | 0 | 0 | 26 | clean declarative adoption, unchanged from prerelease.1 baseline |
| 0-sites | 4 | 3 | 3 | 88 | known provider import-hydration gaps (docs/provider-issues-draft.md); counts ≈ prior baseline |
| 2-always-on | 2 | 2 | 0 | 2 | adds = new prerelease.2 toggle/mover; changes = hydration normalization |
| 2-anti-malware-file-hash | 2 | 0 | 0 | 0 | empty family; adds = toggle/mover only |
| 2-app-tenant-restriction | 2 | 0 | 0 | 3 | clean |
| 2-application-control | 2 | 44 | 0 | 50 | changes fully characterized: 44× `at` position (`LAST_IN_POLICY→LAST_IN_SECTION`+ref — position isn't hydrated on import) + 17× `content_profile` null-vs-absent. Zero semantic drift. |
| 2-client-connectivity | 2 | 0 | 0 | 6 | clean |
| 2-dynamic-ip-allocation | 2 | 0 | 0 | 1 | clean |
| 2-internet-firewall | 43 | 1 | 0 | 0 | state is stale (tenant rebuilt — known); plans as full create. Needs re-adoption (fresh migration.tf) before any <source-profile> apply. |
| 2-lan-firewall | 2 | 15 | 0 | 18 | hydration-normalization class (same as APC) |
| 2-private-access | 2 | 1 | 0 | 3 | hydration normalization |
| 2-remote-port-forward | 2 | 0 | 0 | 1 | clean |
| 2-sdp-static-ip-allocation | 2 | 0 | 0 | 1 | clean |
| 2-site-web-proxy | 2 | 0 | 0 | 1 | clean |
| 2-socket-bypass | 2 | 5 | 0 | 5 | hydration normalization |
| 2-split-tunnel | 2 | 1 | 0 | 4 | hydration normalization |
| 2-terminal-server | 2 | 0 | 0 | 0 | empty family; toggle/mover only |
| 2-tls-inspection | 12 | 0 | 0 | 0 | 11 live rules + policy resource, no state yet — all-adds until adopted |
| 2-wan-firewall | 26 | 0 | 0 | 0 | state stale (as IFW) — full create |
| 2-wan-network | 27 | 0 | 0 | 0 | state stale (as IFW) — full create |

The ubiquitous "2 add" on the policy-family folders is the prerelease.2
`*_policy` enable/disable toggle + bulk move-rule resource — new resources
that have no live counterpart to import.

## Export currency

`scripts/export_all_configs.py` against <source-profile> found 7 differing files.
Classification:

- **Genuine tenant edits since the last capture** (accepted into
  `config_data/`): APC rule "Allow False Positive Credit Card Upload"
  gained group "Demo Users"; Always-On platforms expanded to
  +IOS/ANDROID/CHROMEOS; IFW "Block QUIC services" gained the "Bypassed
  Applications" exception + sub_policies/rule-ordering evolved; one PA and
  one TLS rule edited.
- **Export-format improvements from catocli 3.3.0** (also accepted):
  `section.id` now populated, `activePeriod` expanded to full object.
- **`socket_sites.json` NOT overwritten**: fresh export differs only in
  volatile ids/audit; the committed file carries hand-filled fields the
  API omits (see 0-sites/README.md).

`scripts/validate_config_data.py`: **PASS** (0 errors, 51 pre-existing
warning-class items).

## Follow-ups

1. IFW / WAN FW / WAN NW / TLS re-adoption: **`migration.tf` authored in all
   four folders** (data-driven from the current exports, same pattern as the
   other 2-* folders). TLS is plan-verified already: `11 to import, 1 to add
   (mover), 0/0`. The other three are gated on a manual step — their hollow
   pre-rebuild state must be retired first (exact `mv` commands are in each
   migration.tf header); then plan should read 43/25/26 to import + 1 mover
   add each.
2. The `at`-position hydration gap is provider-issue material
   (docs/provider-issues-draft.md candidate #6): imported rules always
   diff on `at` until first apply repositions them.
3. Provider GA (`2.0.0`) has shipped: the repo now pins `= 2.0.0` everywhere
   (tracked in docs/release-instructions.md).
