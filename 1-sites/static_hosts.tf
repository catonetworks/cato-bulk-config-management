# Static hosts are site-scoped objects managed inline (same convention as
# 1-dependencies): one cato_static_host per element of
# config_data/static_hosts.json, keyed "<site name>:<host name>".
# Refresh the JSON with:  catocli export static_hosts -f json  (defaults to
# --site-type physical, matching socket_sites.json; use --site-type all|cloud to
# change scope. CSV output via -f csv is for human bulk-editing; JSON is the
# Terraform source of truth).

locals {
  # Only deploy hosts whose parent site is actually in this site export. The
  # static_hosts export can contain hosts for cloud/SASE sites that aren't in
  # socket_sites.json; referencing them by name would fail with "Site not found"
  # in a target tenant. Scoping the for_each here guarantees we never attempt a
  # host for a site we aren't deploying (defense-in-depth alongside the catocli
  # export's --site-type physical filter).
  # Socket sites plus the vSocket placeholder sites, whose LAN ranges are now
  # built by vsocket_network_ranges.tf (63 of Reference Arch's 78 static hosts
  # live on vSocket sites; export them with `catocli export static_hosts
  # --site-type all`).
  managed_site_names = toset(concat(
    [for s in jsondecode(file("${var.config_data_dir}/socket_sites.json")).sites : s.name],
    [for s in local.vsocket_sites_data : s.name],
  ))
  all_static_hosts     = jsondecode(file("${var.config_data_dir}/static_hosts.json"))
  static_hosts         = [for h in local.all_static_hosts : h if contains(local.managed_site_names, h.site_name)]
  skipped_static_hosts = [for h in local.all_static_hosts : "${h.site_name}:${h.name}" if !contains(local.managed_site_names, h.site_name)]
}

resource "cato_static_host" "this" {
  for_each = { for h in local.static_hosts : "${h.site_name}:${h.name}" => h }

  # A static host WITH a mac_address is a DHCP reservation, which the API refuses
  # unless the owning network range already has its DHCP range committed
  # ("You must define the DHCP range when defining the DHCP reservation").
  # We now reference the parent site by NAME (site_name), which the provider
  # resolves via entityLookup at apply -- that gives no implicit edge to the
  # sites/ranges, so the hosts would otherwise be created before the ranges'
  # DHCP is applied (and before the sites exist). Force the ordering.
  depends_on = [module.sites, cato_network_range.vsocket]

  # Reference the parent site by name; the provider resolves it to the live
  # site id (via entityLookup) and writes site_id back to state. Portable across
  # tenants (site ids differ, names are stable) and works for sites this repo
  # doesn't manage too -- any site that exists in the target account by name.
  site_name   = each.value.site_name
  name        = each.value.name
  ip          = each.value.ip
  mac_address = try(each.value.mac_address, null)
}

output "static_host_ids" {
  description = "Map of '<site name>:<host name>' => Cato host ID."
  value       = { for key, h in cato_static_host.this : key => h.id }
}

output "static_hosts_skipped_unmanaged_site" {
  description = "Hosts omitted because their site_name is not in socket_sites.json (cloud/unmanaged sites)."
  value       = local.skipped_static_hosts
}
