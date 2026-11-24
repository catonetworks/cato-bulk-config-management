# Webhooks -- fed from config_data/webhooks.json (refresh via
# scripts/export_webhooks.py, which captures the non-secret structural fields:
# name, url, enabled, requestMethod, body, header, responseMapping, auth.authType,
# relatedWebhookId). Referenced BY NAME from subscription_groups' integration[],
# so webhooks must exist before subscription groups (see depends_on there).
#
# SECRETS: the export is credential-free (auth.authType only, never the secret
# subfields). So auth secrets come from the `webhook_secrets` tfvars map, keyed
# by webhook name. A webhook WITHOUT an entry falls back to
# var.webhook_default_auth (an interim placeholder custom header) -- the
# cato_webhook resource requires exactly one of basic/bearer/custom and has no
# NO_AUTH input, so NO_AUTH webhooks can't be expressed yet and get the
# placeholder until provider NO_AUTH support lands. `body` is a payload template,
# not a credential, so it comes straight from the export -- tfvars only ever
# carries the actual auth secret.

locals {
  webhooks = jsondecode(file("${var.config_data_dir}/webhooks.json"))
  # try(): a `catocli export all --strip-ids` capture has no webhook ids. Only
  # an id-bearing export (scripts/export_webhooks.py) can resolve
  # relatedWebhookId to a name; without ids the related link is dropped (null)
  # rather than failing the whole folder's plan with "Unsupported attribute".
  webhooks_by_id = { for w in local.webhooks : w.id => w.name if try(w.id, null) != null }

  # Webhook NAMES (keys) are not secret -- nonsensitive() lets them be used in
  # expressions driving resource arguments; only the auth VALUES stay sensitive.
  webhook_secret_names = nonsensitive(keys(var.webhook_secrets))
}

resource "cato_webhook" "this" {
  # Only manage webhooks whose export carries `body` (a REQUIRED cato_webhook
  # attribute). The stock `catocli export all` writes a MINIMAL webhooks.json
  # without body; only scripts/export_webhooks.py captures it. Rather than
  # hard-fail the whole plan on a minimal export, skip bodyless webhooks -- they
  # are simply unmanaged until a richer export (export_webhooks.py) is run. The
  # migration.tf import block applies the SAME filter so import targets match.
  for_each = { for w in local.webhooks : w.name => w if try(w.body, null) != null }

  name           = each.value.name
  url            = each.value.url
  enabled        = try(each.value.enabled, true)
  request_method = try(each.value.requestMethod, null)
  body           = each.value.body

  # Real secret if one was supplied for this webhook; otherwise the interim
  # placeholder default (see var.webhook_default_auth). Both branches are the
  # same {basic,bearer,custom} object shape so the ternary stays type-consistent.
  auth = contains(local.webhook_secret_names, each.key) ? {
    basic  = try(var.webhook_secrets[each.key].basic, null)
    bearer = try(var.webhook_secrets[each.key].bearer, null)
    custom = try(var.webhook_secrets[each.key].custom, null)
    } : {
    basic  = try(var.webhook_default_auth.basic, null)
    bearer = try(var.webhook_default_auth.bearer, null)
    custom = try(var.webhook_default_auth.custom, null)
  }

  # Non-secret structural fields from the export. Empty -> null (not []) to match
  # what the API returns on read (null), otherwise the plan perpetually diffs
  # null<->[]. response_mapping values are normalized to a leading "$." because
  # createWebhook stores them as JSONPaths ("result.sys_id" -> "$.result.sys_id"),
  # so config must carry the "$." form to converge against target state.
  header = length(try(each.value.header, [])) > 0 ? [
    for h in each.value.header : { name = h.name, value = h.value }
  ] : null
  response_mapping = length(try(each.value.responseMapping, [])) > 0 ? [
    for r in each.value.responseMapping : {
      key   = r.key
      value = startswith(r.value, "$.") ? r.value : "$.${r.value}"
    }
  ] : null

  # relatedWebhookId is a source-tenant id; resolve it to the referenced
  # webhook's NAME (portable across tenants) and reference by NAME so the
  # provider resolves it on the target. The referenced webhook is created in the
  # same apply; if it isn't resolved yet on first create, a single convergence
  # re-apply wires it.
  related_webhook_ref = try(local.webhooks_by_id[each.value.relatedWebhookId], null) != null ? {
    by    = "NAME"
    input = local.webhooks_by_id[each.value.relatedWebhookId]
  } : null
}
