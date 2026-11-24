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
resource "cato_global_ip_ranges" "this" {
  ranges = [
    for r in local.global_ip_ranges : {
      name        = r.name
      ip_range    = r.ipRange
      description = try(r.description, null)
    }
  ]
}
