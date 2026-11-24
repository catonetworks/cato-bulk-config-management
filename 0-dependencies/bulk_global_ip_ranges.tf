# Global IP Ranges -- fed from config_data/global_ip_ranges.json.
# Singleton: one resource instance owns the account's entire global IP range set.

locals {
  # var.global_ip_ranges_exclude: per-target opt-out by name (e.g. a range the
  # API WAF will not accept, created by hand in the CMA instead). Remove the
  # name from the list once the range exists, or the singleton (which owns the
  # account's whole range set) will plan to delete it.
  global_ip_ranges = [
    for r in jsondecode(file("${var.config_data_dir}/global_ip_ranges.json")) : r
    if !contains(var.global_ip_ranges_exclude, r.name)
  ]
}

# NOTE (2026-11-08): the <source-profile> AzureVDI-2 range's real IP (169.254.169.254, the cloud
# IMDS address) is blocked by the Cato API WAF (Incapsula SSRF signature) on
# createGlobalIpRangeBulk, so config_data/global_ip_ranges.json carries a placeholder IP
# for it as a dev-tenant stopgap. Restore the real IP once the WAF rule is tuned. See:
# terraform-provider-cato-tests/failed_tests/WAF-403-RETRY-GLOBAL-IP-RANGES.md
# Declared only when there are ranges to manage. With none (an account that has no global IP ranges), the
# provider plans an empty `ranges` as absent and Terraform stops with "Provider produced invalid plan ...
# planned for absence but config wants existence". If a deployment that has ranges later has none to manage,
# the singleton is destroyed, which removes the ranges it owned: the same thing an empty list would mean.
resource "cato_global_ip_ranges" "this" {
  count = length(local.global_ip_ranges) > 0 ? 1 : 0

  ranges = [
    for r in local.global_ip_ranges : {
      name        = r.name
      ip_range    = r.ipRange
      description = try(r.description, null)
    }
  ]
}

# State from before `count`: the same singleton, now instance [0]. No change in the account.
moved {
  from = cato_global_ip_ranges.this
  to   = cato_global_ip_ranges.this[0]
}

locals {
  managed_global_ip_ranges = try(cato_global_ip_ranges.this[0].ranges, [])
}
