variable "cato_token" {
  description = "Cato API token with Edit permissions. Optional when cato_profile / CATO_PROFILE is used."
  type        = string
  sensitive   = true
  default     = null
}

variable "account_id" {
  description = "Cato account ID. Optional when cato_profile / CATO_PROFILE is used."
  type        = string
  default     = null
}

variable "cato_profile" {
  description = "Optional catocli profile name (from ~/.cato/credentials) to source token, account_id, and endpoint from. Explicit token/account_id values always win over profile-sourced values. May also be supplied via the CATO_PROFILE environment variable instead."
  type        = string
  default     = null
}

variable "config_data_dir" {
  description = "Directory holding the exported config_data/*.json files this folder reads. Defaults to the repo-relative path; override (or set TF_VAR_config_data_dir) to point at an external data set."
  type        = string
  default     = "../config_data"
}

variable "ipsec_sites_exclude" {
  description = "IPSec site names from ipsec_sites.json to leave out on THIS target (e.g. sites that terminate on PoPs the target is not entitled to). Default: none."
  type        = list(string)
  default     = []
}
