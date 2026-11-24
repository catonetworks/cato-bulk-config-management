terraform {
  required_version = ">= 1.7.0" # import blocks with for_each (static host adoption)

  required_providers {
    cato = {
      source = "catonetworks/cato"
      # 2.0.0 is PUBLISHED to the Terraform Registry (adds catocli
      # profile authentication on top of the 1.5.x policy-family/bulk-move
      # resources). Resolved directly from the registry -- no filesystem mirror.
      version = "= 2.0.0"
    }
  }
}
