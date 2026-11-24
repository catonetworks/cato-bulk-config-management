# IPSec PLACEHOLDER sites, fed from config_data/ipsec_sites.json
# (catocli export ipsec_sites). Creates the site + its endpoint publicCatoIp
# only -- tunnels (PSKs, peer IPs, IKE crypto) are added later/manually.
# Missing JSON file => empty for_each.
#
# Each endpoint's publicCatoIp (a Cato allocated IP, IPv4 destination) is
# resolved from an allocated-IP display NAME in ipsec_sites.json by
# ipsec_allocated_ip_resolution.tf (local.ipsec_public_cato_ip_id_by_name).
# Supplying public_cato_ip_id is required by the API for IPv4 endpoints even
# when no tunnels are defined yet.

locals {
  # var.ipsec_sites_exclude: per-target opt-out by site name (e.g. a site whose
  # PoP allocations the target cannot have, such as China PoPs).
  ipsec_sites_data = [
    for s in try(jsondecode(file("${var.config_data_dir}/ipsec_sites.json")).sites, []) : s
    if !contains(var.ipsec_sites_exclude, s.name)
  ]
}

resource "cato_ipsec_site" "this" {
  for_each = { for s in local.ipsec_sites_data : s.name => s }

  name                 = each.value.name
  description          = try(each.value.description, "") # Required by the resource
  site_type            = each.value.type
  native_network_range = each.value.native_range.subnet

  site_location = {
    country_code = each.value.site_location.countryCode
    timezone     = each.value.site_location.timezone
    state_code   = try(each.value.site_location.stateCode, "") != "" ? each.value.site_location.stateCode : null
    city         = try(each.value.site_location.city, "") != "" ? each.value.site_location.city : null
  }

  # primary is Required; secondary is Optional and only emitted when the site
  # defines it. Each endpoint sets destination_type + public_cato_ip_id (the
  # allocated IP resolved by name); tunnels are intentionally omitted (added
  # later/manually). An IPv4 endpoint without public_cato_ip_id is rejected by
  # the API ("publicCatoIp is a mandatory field for IPv4 destination type").
  #
  # An empty `ipsec` block in the export means the source site has no endpoints
  # at all (catocli builds it from the account snapshot's ipsec[] list). Such a
  # site gets `primary = {}`: the provider (v2.0.0 and its prereleases) then
  # creates the site and skips the endpoint call, matching the source.
  ipsec = merge(
    {
      primary = try(each.value.ipsec.primary, null) == null ? {
        destination_type  = null
        public_cato_ip_id = null
        } : {
        destination_type  = try(each.value.ipsec.primary.destination_type, "IPv4")
        public_cato_ip_id = try(local.ipsec_public_cato_ip_id_by_name[each.value.ipsec.primary.public_cato_ip_name], null)
      }
    },
    try(each.value.ipsec.secondary, null) != null ? {
      secondary = {
        destination_type  = try(each.value.ipsec.secondary.destination_type, "IPv4")
        public_cato_ip_id = try(local.ipsec_public_cato_ip_id_by_name[each.value.ipsec.secondary.public_cato_ip_name], null)
      }
    } : {}
  )

  # Fail at PLAN (not with a confusing API error at apply) when an endpoint would
  # be an IPv4 destination but has no resolvable publicCatoIp, so the API would
  # reject it with "publicCatoIp is a mandatory field for IPv4 destination type".
  # A site whose JSON has no ipsec key at all (an export from a catocli that
  # didn't capture endpoints) is refused too: it may have endpoints we can't see.
  lifecycle {
    precondition {
      condition = can(each.value.ipsec)
      error_message = format(
        "IPSec site %q has no ipsec block in config_data/ipsec_sites.json, so its endpoints are unknown. Export it again with a catocli that records them (scripts/export_source.py does).",
        each.key,
      )
    }
    precondition {
      condition = try(each.value.ipsec.primary, null) == null || !(
        try(each.value.ipsec.primary.destination_type, "IPv4") == "IPv4" &&
        try(local.ipsec_public_cato_ip_id_by_name[each.value.ipsec.primary.public_cato_ip_name], null) == null
      )
      error_message = format(
        "IPSec site %q: primary endpoint is an IPv4 destination with no resolvable publicCatoIp. Set ipsec.primary.public_cato_ip_name in config_data/ipsec_sites.json to an allocated-IP display name that exists on this tenant (create it in 0-dependencies / 0-allocated-ips first), or set ipsec.primary.destination_type to \"FQDN\".",
        each.key,
      )
    }
    precondition {
      condition = try(each.value.ipsec.secondary, null) == null || !(
        try(each.value.ipsec.secondary.destination_type, "IPv4") == "IPv4" &&
        try(local.ipsec_public_cato_ip_id_by_name[each.value.ipsec.secondary.public_cato_ip_name], null) == null
      )
      error_message = format(
        "IPSec site %q: secondary endpoint is an IPv4 destination with no resolvable publicCatoIp. Set ipsec.secondary.public_cato_ip_name in config_data/ipsec_sites.json to an allocated-IP display name that exists on this tenant, or set ipsec.secondary.destination_type to \"FQDN\".",
        each.key,
      )
    }
  }
}

output "ipsec_sites" {
  description = "Map of IPSec placeholder sites by name -> site id."
  value       = { for name, s in cato_ipsec_site.this : name => s.id }
}
