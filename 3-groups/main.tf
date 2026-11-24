provider "cato" {
  # baseurl    = "https://api.catonetworks.com/api/v1/graphql2"
  # token      = var.cato_token
  # account_id = var.account_id
  profile = var.cato_profile
}

locals {
  groups_all = jsondecode(file("${var.config_data_dir}/groups.json"))

  # Scope groups to what exists in THIS tenant (same idea as static_hosts): a
  # group is deployable only if every SITE-SCOPED member resolves to a site in
  # socket_sites.json. Members on non-replicated (cloud/SASE) sites can't resolve
  # by name until those sites are replicated, so such groups are skipped (and
  # surfaced) instead of failing the apply. Non-site-scoped members (GLOBAL_IP_RANGE,
  # etc.) never gate the group. The site a site-scoped member belongs to is the
  # part of its name before the first " \ " (SITE members: the whole name).
  # This is interim -- once cloud sites are replicated, those groups auto-include.
  # Two tiers of site, because 0-sites provisions them differently:
  #  - SOCKET sites get the FULL bulk build (site + LAN/WAN interfaces + named
  #    network ranges + hosts), so EVERY site-scoped member type resolves.
  #  - vSocket/IPSec/interconnect sites are PLACEHOLDER shells (site + native
  #    range only -- see 0-sites/{vsocket,ipsec,interconnect}_sites.tf), so only
  #    a whole-SITE member reference resolves; a SITE_NETWORK_SUBNET / HOST /
  #    NETWORK_INTERFACE / FLOATING_SUBNET inside one does NOT exist yet.
  # A group is deployable only if each of its site-scoped members is satisfiable
  # under this rule; otherwise it's skipped (and surfaced) rather than failing
  # the apply. The site a site-scoped member belongs to is the part of its name
  # before the first " \ " (SITE members: the whole name). try()s keep this
  # clean on accounts lacking a given site-type export.
  managed_socket_site_names = toset([for s in jsondecode(file("${var.config_data_dir}/socket_sites.json")).sites : s.name])
  placeholder_site_names = toset(concat(
    [for s in try(jsondecode(file("${var.config_data_dir}/vsocket_sites.json")).sites, []) : s.name],
    [for s in try(jsondecode(file("${var.config_data_dir}/ipsec_sites.json")).sites, []) : s.name],
    [for s in try(jsondecode(file("${var.config_data_dir}/interconnect_sites.json")).sites, []) : s.name],
  ))
  site_scoped_types = ["SITE", "SITE_NETWORK_SUBNET", "HOST", "NETWORK_INTERFACE", "FLOATING_SUBNET"]

  # Static hosts that 1-sites builds on vSocket placeholder sites, as
  # "<site>|<host>". A HOST member "<site> \ <interface> \ <host>" on one of
  # those sites is satisfiable when the host itself is built.
  placeholder_site_host_keys = toset([
    for h in try(jsondecode(file("${var.config_data_dir}/static_hosts.json")), []) : "${h.site_name}|${h.name}"
    if contains(local.placeholder_site_names, h.site_name)
  ])

  # Ranges that 1-sites/vsocket_network_ranges.tf builds INSIDE the vSocket
  # placeholder sites, by their CMA name "<site> \ <interface> \ <range>". A
  # SITE_NETWORK_SUBNET member naming one of these is satisfiable even though
  # the site itself is only a placeholder.
  placeholder_site_subnet_names = toset(flatten([
    for s in try(jsondecode(file("${var.config_data_dir}/vsocket_sites.json")).sites, []) : [
      for nr in flatten([for li in try(s.lan_interfaces, []) : try(li.network_ranges, [])]) :
      "${s.name} \\ ${try(nr.interface_index, s.native_range.interface_name)} \\ ${nr.name}"
    ]
  ]))

  group_deployable = {
    for g in local.groups_all : g.name => alltrue([
      for m in try(g.members, []) :
      !contains(local.site_scoped_types, m.type) ? true : (
        # member's owning site
        contains(local.managed_socket_site_names, split(" \\ ", try(m.name, ""))[0]) ||
        # placeholder site: a whole-SITE reference, or a subnet that
        # 1-sites/vsocket_network_ranges.tf builds inside it
        (m.type == "SITE" && contains(local.placeholder_site_names, try(m.name, ""))) ||
        (m.type == "SITE_NETWORK_SUBNET" && contains(local.placeholder_site_subnet_names, try(m.name, ""))) ||
        (m.type == "HOST" && contains(local.placeholder_site_host_keys, "${split(" \\ ", try(m.name, ""))[0]}|${replace(element(split(" \\ ", try(m.name, "")), length(split(" \\ ", try(m.name, ""))) - 1), "/ \\([^)]*\\)$/", "")}"))
      )
    ])
  }
  groups         = [for g in local.groups_all : g if local.group_deployable[g.name]]
  skipped_groups = [for g in local.groups_all : g.name if !local.group_deployable[g.name]]
}

resource "cato_group" "this" {
  for_each = { for g in local.groups : g.name => g }

  name        = each.value.name
  description = try(each.value.description, null)

  # Reference every member by NAME for cross-tenant portability -- ids are
  # tenant-specific and don't survive replication. The provider resolves all
  # member types (SITE, SITE_NETWORK_SUBNET, HOST, NETWORK_INTERFACE,
  # FLOATING_SUBNET, GLOBAL_IP_RANGE, ...) by name. id is only a fallback for a
  # member that somehow has no name.
  members = [
    for m in try(each.value.members, []) : {
      type = m.type
      name = try(m.name, null)
      id   = try(m.name, null) == null ? try(m.id, null) : null
    }
  ]
}
