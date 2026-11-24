provider "cato" {
  # baseurl    = "https://api.catonetworks.com/api/v1/graphql2"
  # token      = var.cato_token
  # account_id = var.account_id
  profile = var.cato_profile
}

module "wnw_rules" {
  source  = "catonetworks/bulk-wnw-rules/cato"
  version = "0.0.2"
  # Local dev override (uncomment to use a local module clone):
  # source = "../../../../catonetworks/terraform-cato-bulk-wnw-rules"
  wnw_rules_json_file_path = var.wnw_rules_json_file_path
  # Carry the source policy's on/off state across (null = leave unmanaged).
  policy_enabled     = try(one(values(jsondecode(file(var.wnw_rules_json_file_path)).data.policy)).policy.enabled, null)
  allow_empty_policy = true
  # Dynamic allocated-IP resolution by (description, popLocation) -- see
  # allocated_ip_resolution.tf. Replaces the scripts/remap_allocated_ips.py
  # staging-copy flow for this family.
  allocated_ip_id_map = local.wnw_allocated_ip_id_map
}
