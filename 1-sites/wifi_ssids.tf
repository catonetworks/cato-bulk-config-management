# Wi-Fi SSIDs on socket sites, fed from socket_sites.json sites[].wifi_ssids[].
#
# The published socket-bulk-sites / socket modules (0.0.14) have no Wi-Fi
# support, and the 0.0.15 Wi-Fi work depends on an unpublished socket module,
# so the SSIDs are declared here directly with the native cato_wifi_ssid
# resource. State is keyed "<site name>:<ssid name>".
#
# PASSKEYS: Cato never returns Wi-Fi passkeys, so the export carries the
# placeholder "CHANGE_ME". Supply the real ones in a gitignored *.tfvars:
#   wifi_ssid_passkeys = { "Branch 1:Guest Wi-Fi" = "..." }
# A PSK SSID without a real passkey fails the plan (precondition) rather than
# going live with "CHANGE_ME".

variable "wifi_ssid_passkeys" {
  description = "Wi-Fi PSK passkeys keyed \"<site name>:<ssid name>\". Required for every PSK SSID in socket_sites.json."
  type        = map(string)
  default     = {}
  sensitive   = true
}

locals {
  wifi_ssids = merge({}, [
    for s in try(jsondecode(file("${var.config_data_dir}/socket_sites.json")).sites, []) : {
      for w in try(s.wifi_ssids, []) : "${s.name}:${w.name}" => merge(w, { site_name = s.name })
    }
  ]...)
  wifi_ssid_passkey_names = nonsensitive(keys(var.wifi_ssid_passkeys))
}

resource "cato_wifi_ssid" "this" {
  for_each = local.wifi_ssids

  site                      = { id = module.sites.socket_sites[each.value.site_name].id }
  name                      = each.value.name
  network_interface_id      = each.value.network_interface_id
  band                      = each.value.band
  category                  = each.value.category
  enabled                   = try(each.value.enabled, true)
  visible                   = try(each.value.visible, true)
  internet_only             = try(each.value.internet_only, false)
  subnet                    = each.value.subnet
  local_ip                  = each.value.local_ip
  mdns_enabled              = try(each.value.mdns_enabled, false)
  microsegmentation_enabled = try(each.value.microsegmentation_enabled, false)
  dhcp                      = try(each.value.dhcp.dhcp_subnet, null) != null ? { dhcp_subnet = each.value.dhcp.dhcp_subnet } : null

  security = {
    mode                 = each.value.security.mode
    auth_protocol        = each.value.security.auth_protocol
    track_authentication = try(each.value.security.track_authentication, null)
    psk                  = each.value.security.mode == "PSK" ? { passkey = lookup(var.wifi_ssid_passkeys, each.key, "CHANGE_ME") } : null
  }

  lifecycle {
    precondition {
      condition     = each.value.security.mode != "PSK" || contains(local.wifi_ssid_passkey_names, each.key)
      error_message = "Wi-Fi SSID ${each.key} is PSK but has no entry in var.wifi_ssid_passkeys. Cato never exports passkeys; supply one in a gitignored *.tfvars."
    }
  }
}

output "wifi_ssids" {
  description = "Wi-Fi SSIDs by \"<site>:<ssid>\" -> id."
  value       = { for k, w in cato_wifi_ssid.this : k => w.id }
}
