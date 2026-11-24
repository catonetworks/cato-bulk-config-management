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

variable "webhook_secrets" {
  description = <<-EOT
    Auth secrets for webhooks, keyed by webhook name (matching config_data/webhooks.json).
    The webhook export is credential-free, so the secret auth material is supplied here
    (set in a gitignored *.tfvars). NO_AUTH webhooks need NO entry. Provide exactly one of
    basic / bearer / custom per entry, matching the webhook's authType:
      webhook_secrets = {
        "My Basic Hook"  = { basic  = { username = "u", password = "p" } }
        "My Bearer Hook" = { bearer = { bearer_token = "t" } }
        "My Custom Hook" = { custom = { name = "x-api-key", value = "secret" } }
      }
    A non-NO_AUTH webhook without an entry is skipped (see webhooks_skipped_no_secret output).
  EOT
  type = map(object({
    basic  = optional(object({ username = string, password = string }))
    bearer = optional(object({ bearer_token = string }))
    custom = optional(object({ name = string, value = string }))
  }))
  default   = {}
  sensitive = true
}

variable "webhook_default_auth" {
  description = <<-EOT
    INTERIM placeholder auth for webhooks that have no webhook_secrets entry.
    The cato_webhook resource requires exactly one of basic/bearer/custom and has
    no NO_AUTH input (NoAuth is an output-only type), so NO_AUTH webhooks cannot
    be expressed today -- they (and any webhook lacking a real secret) are created
    with this placeholder custom header until the provider gains NO_AUTH support.
    NOTE: this means a NO_AUTH webhook will send this extra header on every call;
    override it or supply a real webhook_secrets entry for production use.
  EOT
  type = object({
    basic  = optional(object({ username = string, password = string }))
    bearer = optional(object({ bearer_token = string }))
    custom = optional(object({ name = string, value = string }))
  })
  default   = { custom = { name = "X-Cato-Webhook-Placeholder", value = "placeholder" } }
  sensitive = true
}

variable "config_data_dir" {
  description = "Directory holding the exported config_data/*.json files this folder reads. Defaults to the repo-relative path; override (or set TF_VAR_config_data_dir) to point at an external data set."
  type        = string
  default     = "../config_data"
}

variable "global_ip_ranges_exclude" {
  description = "Names from global_ip_ranges.json to leave out on THIS target (e.g. a 169.254.169.254 range the Cato API WAF rejects, created by hand instead). Default: none."
  type        = list(string)
  default     = []
}

variable "allocated_ips_exclude" {
  description = "Source display names from allocated_ips.json to leave out on THIS target (e.g. PoPs the target is not entitled to, or more allocations than its licence allows). Default: none."
  type        = list(string)
  default     = []
}
