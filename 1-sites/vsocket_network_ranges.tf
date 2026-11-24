# Network ranges INSIDE the vSocket placeholder sites, fed from
# vsocket_sites.json sites[].lan_interfaces[].network_ranges[].
#
# The placeholder site carries only its native range, so any group member or
# policy rule that names a subnet inside a vSocket site ("<site> \ LAN1 \ <range>")
# could not resolve on a greenfield target. The export already has these
# ranges; this builds them on the placeholder site so those references work.
# State is keyed "<site name>:<range name>".

locals {
  vsocket_network_ranges = merge({}, [
    for s in local.vsocket_sites_data : {
      for nr in flatten([for li in try(s.lan_interfaces, []) : try(li.network_ranges, [])]) :
      "${s.name}:${nr.name}" => merge(nr, { site_name = s.name })
      # A range flagged native_range is the site's own native range, which
      # cato_socket_site.vsocket already creates. Some exports list it here too.
      if try(nr.native_range, false) != true
    }
  ]...)
}

resource "cato_network_range" "vsocket" {
  for_each = local.vsocket_network_ranges

  site_id         = cato_socket_site.vsocket[each.value.site_name].id
  interface_index = try(each.value.interface_index, null)
  name            = each.value.name
  range_type      = each.value.range_type
  subnet          = each.value.subnet
  gateway         = try(each.value.gateway, "") != "" ? each.value.gateway : null
  local_ip        = try(each.value.local_ip, "") != "" ? each.value.local_ip : null
  vlan            = try(each.value.vlan, null) != null && try(each.value.vlan, "") != "" ? tonumber(each.value.vlan) : null
  # identity translation (translated == subnet) is a no-op and is rejected on
  # tenants with Static Range Translation disabled
  translated_subnet = try(each.value.translated_subnet, "") != "" && try(each.value.translated_subnet, "") != each.value.subnet ? each.value.translated_subnet : null
  internet_only     = try(each.value.internet_only, null)
  # the provider rejects mdns_reflector on Routed ranges, even when false
  mdns_reflector = each.value.range_type == "Routed" ? null : try(each.value.mdns_reflector, null)
  dhcp_settings = try(each.value.dhcp_settings.dhcp_type, null) != null ? {
    dhcp_type              = each.value.dhcp_settings.dhcp_type
    ip_range               = try(each.value.dhcp_settings.ip_range, null)
    relay_group_id         = try(each.value.dhcp_settings.relay_group_id, null)
    relay_group_name       = try(each.value.dhcp_settings.relay_group_name, null)
    dhcp_microsegmentation = try(each.value.dhcp_settings.dhcp_microsegmentation, null)
  } : null
}

output "vsocket_network_ranges" {
  description = "Network ranges inside vSocket placeholder sites, \"<site>:<range>\" -> id."
  value       = { for k, r in cato_network_range.vsocket : k => r.id }
}
