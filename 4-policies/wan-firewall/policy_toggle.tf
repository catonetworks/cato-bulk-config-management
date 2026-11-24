# WAN Firewall policy on/off switch, carried across from the export's policy.enabled.
#
# The bulk module has no policy_enabled input for this family and, until now,
# the provider had no toggle resource for it either, so a replicated policy was
# left in whatever state the target tenant started in (a new tenant ships with
# it DISABLED). REQUIRES a provider build with cato_wf_policy (added on
# terraform-provider-cato branch fix/group-allow-empty-members); use
# scripts/bulk_apply.py --mirror until that ships in a release.
resource "cato_wf_policy" "this" {
  enabled    = try(one(values(jsondecode(file("${var.config_data_dir}/all_wf_rules_and_sections.json")).data.policy)).policy.enabled, true)
  depends_on = [module.wf_rules]
}
