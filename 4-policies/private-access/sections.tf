# Private Access sections, fed from the export's policy.sections[].
#
# The bulk-private-access module manages rules and their order only ("1st
# phase", no sections), so the source's sections were never replicated. The
# provider has cato_private_access_section; build each section in source
# order at the end of the policy.

locals {
  pa_sections = try(one(values(jsondecode(file("${var.config_data_dir}/all_pa_rules_and_sections.json")).data.policy)).policy.sections, [])
}

resource "cato_private_access_section" "this" {
  for_each = { for s in local.pa_sections : s.section_name => s }

  at      = { position = "LAST_IN_POLICY" }
  section = { name = each.value.section_name }

  depends_on = [module.pa_rules]
}
