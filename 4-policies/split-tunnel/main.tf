provider "cato" {
  # baseurl    = "https://api.catonetworks.com/api/v1/graphql2"
  # token      = var.cato_token
  # account_id = var.account_id
  profile = var.cato_profile
}

module "st_rules" {
  # Local clone while the module is pre-release; restore the registry pin
  # below once it's published.
  source  = "catonetworks/bulk-st-rules/cato"
  version = "0.0.1"
  # Local dev override (uncomment to use a local module clone):
  # source = "../../../../catonetworks/terraform-cato-bulk-st-rules"
  # version = "0.0.1"

  st_rules_json_file_path = "${var.config_data_dir}/all_st_rules_and_sections.json"

  # Carry the source policy's on/off state across (null = leave unmanaged).
  policy_enabled     = try(one(values(jsondecode(file("${var.config_data_dir}/all_st_rules_and_sections.json")).data.policy)).policy.enabled, null)
  allow_empty_policy = true
}
