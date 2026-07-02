use super::{AliyunApiTool, VERSION_MAP};

impl AliyunApiTool {
    pub fn resolve_version(
        &self,
        product: &str,
        explicit_version: Option<&str>,
    ) -> Result<String, String> {
        if let Some(version) = explicit_version.filter(|value| !value.is_empty()) {
            return Ok(version.to_owned());
        }
        VERSION_MAP
            .iter()
            .find(|(known, _)| known.eq_ignore_ascii_case(product))
            .map(|(_, version)| (*version).to_owned())
            .ok_or_else(|| {
                format!(
                    "No built-in version for product '{product}'. Please provide an explicit 'version' parameter."
                )
            })
    }

    pub(super) fn endpoint_url(&self, product: &str, region: &str) -> String {
        if let Some(endpoint) = self.endpoint_overrides.get(&product.to_ascii_lowercase()) {
            return endpoint.clone();
        }

        let endpoint = match product {
            "ros" => "ros.aliyuncs.com".to_owned(),
            "IaCService" => "iac.aliyuncs.com".to_owned(),
            "oss" if !region.is_empty() => format!("oss-{region}.aliyuncs.com"),
            _ if !region.is_empty() => format!("{}.{}.aliyuncs.com", product, region),
            _ => format!("{product}.aliyuncs.com"),
        };
        format!("https://{endpoint}/")
    }
}

pub(super) fn canonical_product(product: &str) -> String {
    VERSION_MAP
        .iter()
        .find(|(known, _)| known.eq_ignore_ascii_case(product))
        .map(|(known, _)| (*known).to_owned())
        .unwrap_or_else(|| product.to_owned())
}
