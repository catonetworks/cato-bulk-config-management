output "global_ip_range_ids" {
  description = "Map of global IP range name => Cato range ID."
  value       = { for r in local.managed_global_ip_ranges : r.name => r.id }
}

output "global_ip_range_names" {
  description = "Names of all managed global IP ranges."
  value       = sort([for r in local.managed_global_ip_ranges : r.name])
}

output "custom_application_ids" {
  description = "Map of custom application name => Cato application ID."
  value       = { for name, a in cato_custom_application.this : name => a.id }
}

output "custom_application_names" {
  description = "Names of all managed custom applications."
  value       = sort(keys(cato_custom_application.this))
}

output "fqdn_container_ids" {
  description = "Map of FQDN container name => Cato container ID."
  value       = { for name, c in cato_fqdn_container.this : name => c.id }
}

output "ip_range_container_ids" {
  description = "Map of IP address range container name => Cato container ID."
  value       = { for name, c in cato_ip_address_range_container.this : name => c.id }
}

output "mailing_list_ids" {
  description = "Map of mailing list name => Cato mailing list ID."
  value       = { for name, m in cato_mailing_list.this : name => m.id }
}

output "subscription_group_ids" {
  description = "Map of subscription group name => Cato subscription group ID."
  value       = { for name, s in cato_subscription_group.this : name => s.id }
}

output "webhook_ids" {
  description = "Map of webhook name => Cato webhook ID."
  value       = { for name, w in cato_webhook.this : name => w.id }
}

output "webhooks_using_placeholder_auth" {
  description = "Webhooks deployed with the interim webhook_default_auth placeholder (no real webhook_secrets entry supplied) -- supply one in a gitignored tfvars for production auth."
  value       = sort([for w in local.webhooks : w.name if !contains(local.webhook_secret_names, w.name)])
}

output "dhcp_relay_group_ids" {
  description = "Map of DHCP relay group name => Cato DHCP relay group ID."
  value       = { for name, d in cato_dhcp_relay_group.this : name => d.id }
}

output "deployment_summary" {
  description = "Counts of managed dependency objects."
  value = {
    total_global_ip_ranges    = length(local.managed_global_ip_ranges)
    total_custom_applications = length(cato_custom_application.this)
    total_fqdn_containers     = length(cato_fqdn_container.this)
    total_ip_range_containers = length(cato_ip_address_range_container.this)
    total_mailing_lists       = length(cato_mailing_list.this)
    total_subscription_groups = length(cato_subscription_group.this)
    total_webhooks            = length(cato_webhook.this)
    total_dhcp_relay_groups   = length(cato_dhcp_relay_group.this)
    total_allocated_ips       = length(cato_allocated_ip.this)
  }
}

output "allocated_ip_map" {
  description = "Map of SOURCE allocation display name => this tenant's allocation (name/ip). The deploy-time policy remap input: scripts/remap_allocated_ips.py rewrites allocationIp rule refs from source names to these target names."
  value = { for source_name, a in cato_allocated_ip.this : source_name => {
    name = a.name
    ip   = a.ip
    id   = a.id
  } }
}
