# BLOCKED: this family's bulk-move resource ships in the provider's
# feat/bulk-policy-families branch — init/validate fails against published
# 1.5.0-prerelease.1 until the next provider release.
#
# `cato_bulk_ao_move_rule` (declared by module.ao_rules) is not a known
# resource type in 1.5.0-prerelease.1. See README.md "Blocked on provider
# release" for the exact `terraform validate` error captured against this
# config.

provider "cato" {
  # baseurl    = "https://api.catonetworks.com/api/v1/graphql2"
  # token      = var.cato_token
  # account_id = var.account_id
  profile = var.cato_profile
}

module "ao_rules" {
  # Local path while the module's resources (cato_bulk_ao_move_rule) only
  # exist on the provider's unmerged feat/bulk-policy-families branch.
  source  = "catonetworks/bulk-ao-rules/cato"
  version = "0.0.1"
  # Local dev override (uncomment to use a local module clone):
  # source = "../../../../catonetworks/terraform-cato-bulk-ao-rules"

  # Switch to the registry source once a provider release ships the
  # cato_bulk_ao_move_rule resource:
  # version = "0.0.1"

  ao_rules_json_file_path = "${var.config_data_dir}/all_ao_rules_and_sections.json"

  # Carry the source policy's on/off state across (null = leave unmanaged).
  policy_enabled     = try(one(values(jsondecode(file("${var.config_data_dir}/all_ao_rules_and_sections.json")).data.policy)).policy.enabled, null)
  allow_empty_policy = true
}
