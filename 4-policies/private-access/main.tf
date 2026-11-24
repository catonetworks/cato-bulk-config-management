# This family's bulk resource `cato_private_access_rule_bulk` (declared by
# module.pa_rules) is available in the Cato provider 2.0.0 (GA), which this
# folder pins (`= 2.0.0`). Unlike the other families, Private Access was never
# gated on provider availability; a separate module/exporter `applications`
# schema mismatch still blocks end-to-end plan/apply — see README.md.

provider "cato" {
  # baseurl    = "https://api.catonetworks.com/api/v1/graphql2"
  # token      = var.cato_token
  # account_id = var.account_id
  profile = var.cato_profile
}

module "pa_rules" {
  # Local path while the module's resources (cato_private_access_rule_bulk)
  # only exist on the provider's unmerged feat/bulk-policy-families branch.
  source  = "catonetworks/bulk-private-access/cato"
  version = "0.0.1"
  # Local dev override (uncomment to use a local module clone):
  # source = "../../../../catonetworks/terraform-cato-bulk-private-access"

  # Switch to the registry source once a provider release ships the
  # cato_private_access_rule_bulk resource:
  # version = "0.0.1"

  pa_rules_json_file_path = "${var.config_data_dir}/all_pa_rules_and_sections.json"

  # Carry the source policy's on/off state across (null = leave unmanaged).
  policy_enabled     = try(one(values(jsondecode(file("${var.config_data_dir}/all_pa_rules_and_sections.json")).data.policy)).policy.enabled, null)
  allow_empty_policy = true
}
