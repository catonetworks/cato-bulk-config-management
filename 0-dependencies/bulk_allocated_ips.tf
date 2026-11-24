# Allocated IPs -- ZTNA / private access infrastructure + PoP allocated IPs
# (added 2026-11-10), fed from config_data/allocated_ips.json. Policies
# reference these by name (via the remapped display name -- see
# scripts/remap_allocated_ips.py).
# Cato ASSIGNS the ip -- the (pop_location_name, description) pair is the
# cross-tenant identity. State is keyed by the SOURCE display name
# ("<pop> - <source-ip>") so outputs map source names to the target's new names
# for the deploy-time policy remap.

locals {
  allocated_ips = jsondecode(file("${var.config_data_dir}/allocated_ips.json"))
}

resource "cato_allocated_ip" "this" {
  # var.allocated_ips_exclude: per-target opt-out by source display name, e.g.
  # allocations on PoPs the target is not entitled to (China PoPs) or beyond
  # the target's allocated-IP licence.
  for_each = { for a in local.allocated_ips : a.name => a if !contains(var.allocated_ips_exclude, a.name) }

  pop_location_name = each.value.popLocation.name
  description       = try(each.value.description, null)
}
