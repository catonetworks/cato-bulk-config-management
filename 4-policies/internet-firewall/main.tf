provider "cato" {
  # baseurl    = "https://api.catonetworks.com/api/v1/graphql2"
  # token      = var.cato_token
  # account_id = var.account_id
  profile = var.cato_profile
}

module "if_rules" {
  source  = "catonetworks/bulk-if-rules/cato"
  version = "0.0.9"
  ifw_rules_json_file_path = "${var.config_data_dir}/all_ifw_rules_and_sections.json"
}
