# Changelog

This repository is a consumer package (not a published, semver-tagged Terraform
module), so entries are dated rather than versioned.

## 2026-11-24

### Changes
- Pinned the Cato provider to `2.0.0` (now GA on the Terraform Registry) across
  every folder — `0-dependencies`, `1-sites`, `2-app-connectors`, `3-groups`,
  and all `4-policies/*` — replacing the previous `2.0.0-prerelease.2` pin.
- Deleted the stale `.terraform.lock.hcl` files so they re-pin to `2.0.0` on the
  next `terraform init`.
- Docs: the policy families that were previously gated on an unreleased provider
  (always-on, anti-malware-file-hash, app-tenant-restriction,
  client-connectivity, dynamic-ip-allocation) are no longer blocked — their
  resources ship in GA `2.0.0`. Their "Blocked on provider release" sections are
  retitled to note the blocker is resolved, with the original captured logs kept
  as historical evidence.
- `private-access`: documented that its `applications` export-shape mismatch is
  fixed in the module (it now accepts both the wrapper-object and flat-list
  shapes); the folder is no longer blocked end-to-end.
- Refreshed provider-version references in `README.md`, `docs/release-instructions.md`,
  `docs/prerelease2-revalidation-2026-11-05.md`, and `docs/bulk-apply-runbook.md`
  to reflect GA. Dated verification logs and feature-introduction history were
  left intact as historical records.
