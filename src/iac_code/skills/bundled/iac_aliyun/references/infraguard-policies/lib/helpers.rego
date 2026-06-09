# Package for InfraGuard helper functions
# These functions are built-in to InfraGuard and available in all policy files
# Usage: import data.infraguard.helpers
package infraguard.helpers

import rego.v1

# ============================================================================
# Value Checking Helpers
# ============================================================================

# Check if a value is true (handles string "true" as well)
is_true(v) if {
	v == true
}

is_true(v) if {
	v == "true"
}

# Check if a value is false (handles string "false" as well)
is_false(v) if {
	v == false
}

is_false(v) if {
	v == "false"
}

# Check if a value is in a list
includes(list, elem) if {
	list[_] == elem
}

# ============================================================================
# Network Helpers
# ============================================================================

# Check if CIDR is public IPv4 (0.0.0.0/0)
is_public_cidr(cidr) if {
	cidr == "0.0.0.0/0"
}

# Check if CIDR is public IPv6 (::/0)
is_public_cidr(cidr) if {
	cidr == "::/0"
}

# Private IPv4 CIDR ranges (RFC 1918 and others)
private_cidrs := [
	"10.0.0.0/8", # Class A private
	"172.16.0.0/12", # Class B private
	"192.168.0.0/16", # Class C private
	"127.0.0.0/8", # Loopback
	"169.254.0.0/16", # Link-local
	"100.64.0.0/10", # Carrier-grade NAT
]

# Check if a CIDR/IP is within private ranges (not public internet)
# Returns true if the CIDR is private (not routable on public internet)
is_private_cidr(cidr) if {
	some private_range in private_cidrs
	net.cidr_contains(private_range, cidr)
}

# Check if a CIDR/IP is a public internet address
# Returns true if the CIDR contains public internet IPs
is_internet_cidr(cidr) if {
	# First check if it's a valid CIDR format (has /)
	contains(cidr, "/")

	# If it's 0.0.0.0/0 or ::/0, it includes public internet
	is_public_cidr(cidr)
}

is_internet_cidr(cidr) if {
	# Check if it contains public internet IPs (not in any private range)
	contains(cidr, "/")
	not is_private_cidr(cidr)
	not is_public_cidr(cidr)
}

is_internet_cidr(cidr) if {
	# Handle single IP address (no CIDR notation)
	not contains(cidr, "/")

	# Add /32 suffix and check
	cidr_with_mask := concat("/", [cidr, "32"])
	not is_private_cidr(cidr_with_mask)
}

# ============================================================================
# Port Helpers
# ============================================================================

# Parse port range string (e.g., "22/22", "1/65535", "-1/-1") into [start, end]
parse_port_range(port_range) := [start, end] if {
	parts := split(port_range, "/")
	count(parts) == 2
	start := to_number(parts[0])
	end := to_number(parts[1])
}

# Check if a specific port is within a port range
port_in_range(port, port_range) if {
	[start, end] := parse_port_range(port_range)
	start != -1
	end != -1
	port >= start
	port <= end
}

# Check if port range is all ports (-1/-1)
is_all_ports(port_range) if {
	port_range == "-1/-1"
}

# ============================================================================
# Resource Helpers
# ============================================================================

# Get all resources of a specific type as a map (name -> resource)
resources_by_type(resource_type) := resources if {
	resources := {name: resource |
		some name, resource in input.Resources
		resource.Type == resource_type
	}
}

# Get all resources of multiple types as a map (name -> resource)
resources_by_types(resource_types) := resources if {
	resources := {name: resource |
		some name, resource in input.Resources
		resource.Type in resource_types
	}
}

# Get all resource names of a specific type
resource_names_by_type(resource_type) := [name |
	some name, res in input.Resources
	res.Type == resource_type
]

# Count resources of a specific type
count_resources_by_type(resource_type) := count(resources_by_type(resource_type))

# Check if a resource type exists in the template
resource_exists(resource_type) if {
	count_resources_by_type(resource_type) > 0
}

# Check if a resource type does NOT exist in the template
resource_not_exists(resource_type) if {
	count_resources_by_type(resource_type) == 0
}

# ============================================================================
# Property Helpers
# ============================================================================

# Check if property exists and is not null
has_property(resource, prop) if {
	resource.Properties[prop] != null
}

# Get property with default value
get_property(resource, prop, default_value) := value if {
	has_property(resource, prop)
	value := resource.Properties[prop]
} else := default_value

# ============================================================================
# Reference Helpers
# ============================================================================

# Resolve a value (handles Ref)
# If v is {"Ref": "Name"}, returns "Name"
# Otherwise returns v
resolve_ref(v) := name if {
	is_object(v)
	name := v.Ref
} else := v

# Check if a value refers to a specific resource (by logical ID)
# target_id is the Logical ID of the resource
is_referencing(val, target_id) if {
	resolve_ref(val) == target_id
}

# Resolve Fn::GetAtt reference
# If v is {"Fn::GetAtt": ["ResourceName", "PropertyName"]}, returns "ResourceName"
# Otherwise returns v
resolve_get_att(v) := name if {
	is_object(v)
	name := v["Fn::GetAtt"][0]
} else := v

# Check if a value is a Fn::GetAtt reference to a specific resource
# target_id is the Logical ID of the resource
is_get_att_referencing(val, target_id) if {
	resolve_get_att(val) == target_id
}

# Check if a value matches a resource's identity (Logical ID or its name property)
# resource_id is the Logical ID of the resource
# name_prop is the property name that contains the actual resource name (e.g., "UserName")
matches_resource_id(val, resource_id, name_prop) if {
	is_referencing(val, resource_id)
}

matches_resource_id(val, resource_id, name_prop) if {
	res := input.Resources[resource_id]
	actual_name := get_property(res, name_prop, resource_id)
	val == actual_name
}

# Check if a resource is referenced by another resource's property
# target_id: Logical ID of the resource to check if it's referenced
# ref_resource_type: Type of resource that might reference it (e.g., "ALIYUN::ECS::DiskAttachment")
# property_path: Property path to check, supports paths like ["DiskId"]
is_referenced_by_property(target_id, ref_resource_type, property_path) if {
	some name, ref_resource in resources_by_type(ref_resource_type)
	count(property_path) == 1
	prop := property_path[0]
	has_property(ref_resource, prop)
	prop_value := ref_resource.Properties[prop]
	is_referencing(prop_value, target_id)
}

is_referenced_by_property(target_id, ref_resource_type, property_path) if {
	some name, ref_resource in resources_by_type(ref_resource_type)
	count(property_path) == 1
	prop := property_path[0]
	has_property(ref_resource, prop)
	prop_value := ref_resource.Properties[prop]
	is_get_att_referencing(prop_value, target_id)
}

# ============================================================================
# Tag Helpers
# ============================================================================

# Get tags from resource, handling different tag property names
# Different resources may use different property names for tags:
# - Most resources: "Tags" (array of {Key, Value})
# - Some resources: "Tag" (single object or array)
# - OSS Bucket: "Tags" (array of {Key, Value})
get_resource_tags(resource) := tags if {
	# Standard Tags property (array of {Key, Value})
	has_property(resource, "Tags")
	tags := resource.Properties.Tags
	is_array(tags)
	count(tags) > 0
}

get_resource_tags(resource) := tags if {
	# Alternative Tag property (array of {Key, Value})
	has_property(resource, "Tag")
	tags := resource.Properties.Tag
	is_array(tags)
	count(tags) > 0
}

# Check if resource has any tags
has_tags(resource) if {
	get_resource_tags(resource) != null
}

# Check if resource has a specific tag key
has_tag_key(resource, tag_key) if {
	tags := get_resource_tags(resource)
	some tag in tags
	tag.Key == tag_key
}

# Get tag value by key
get_tag_value(resource, tag_key) := tag.Value if {
	tags := get_resource_tags(resource)
	some tag in tags
	tag.Key == tag_key
}

# Check if resource has a specific tag key-value pair
has_tag_key_value(resource, tag_key, tag_value) if {
	tags := get_resource_tags(resource)
	some tag in tags
	tag.Key == tag_key
	tag.Value == tag_value
}

# Check if resource has a specific tag key-value pair (supports wildcards)
# tag_value_pattern can contain * and ? wildcards
# * matches any sequence of characters
# ? matches any single character
matches_tag_value(resource, tag_key, tag_value_pattern) if {
	actual_value := get_tag_value(resource, tag_key)

	# Use regex matching for wildcard support
	# Convert * to .* and ? to . for regex
	regex_pattern := replace_wildcards(tag_value_pattern)
	regex.match(regex_pattern, actual_value)
}

# Convert wildcard pattern to regex pattern
# * -> .*
# ? -> .
replace_wildcards(pattern) := regex if {
	# Replace * with .* and ? with .
	regex := replace(replace(pattern, "*", ".*"), "?", ".")
}

# Check if resource has all specified tags
# required_tags is an array of tag objects with Key and Value
has_all_tags(resource, required_tags) if {
	# Check that all required tags exist and match
	count(required_tags) == count([tag |
		some tag in required_tags
		has_tag_key_value(resource, tag.Key, tag.Value)
	])
}

# Check if resource has at least one of the specified tags
# tag_patterns is an array of tag objects with Key and Value (supports wildcards in Value)
has_any_tag(resource, tag_patterns) if {
	some tag in tag_patterns
	matches_tag_value(resource, tag.Key, tag.Value)
}
