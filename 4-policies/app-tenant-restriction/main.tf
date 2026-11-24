# This family's bulk-move resource `cato_bulk_atr_move_rule` (declared by
# module.atr_rules) is available in the Cato provider 2.0.0 (GA). This folder
# pins `= 2.0.0`, so init/validate/apply are no longer blocked.

provider "cato" {
  # baseurl    = "https://api.catonetworks.com/api/v1/graphql2"
  # token      = var.cato_token
  # account_id = var.account_id
  profile = var.cato_profile
}

module "atr_rules" {
  # Local path while the module's resources (cato_bulk_atr_move_rule) only
  # exist on the provider's unmerged feat/bulk-policy-families branch.
  source  = "catonetworks/bulk-atr-rules/cato"
  version = "0.0.1"
  # Local dev override (uncomment to use a local module clone):
  # source = "../../../../catonetworks/terraform-cato-bulk-atr-rules"

  # Switch to the registry source once a provider release ships the
  # cato_bulk_atr_move_rule resource:
  # version = "0.0.1"

  atr_rules_json_file_path = "${var.config_data_dir}/all_atr_rules_and_sections.json"

  # Carry the source policy's on/off state across (null = leave unmanaged).
  policy_enabled     = try(one(values(jsondecode(file("${var.config_data_dir}/all_atr_rules_and_sections.json")).data.policy)).policy.enabled, null)
  allow_empty_policy = true
}
