mod cache;
mod probe;
mod settings;

use std::collections::BTreeSet;
use std::path::Path;
use std::time::Duration;

use crate::registry::{provider_descriptor, provider_keys};

pub use cache::AutoDetectCache;
pub use probe::probe_openapi_compatible;
use settings::load_settings_overrides;

pub const DEFAULT_FORMATS: &[&str] = &["image/png", "image/jpeg", "image/gif", "image/webp"];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MultiModalSpec {
    pub support_multimodal: bool,
    pub formats: Vec<String>,
    pub max_images_per_message: usize,
}

#[derive(Clone, Copy, Debug)]
pub struct MultimodalProbeOptions<'a> {
    pub settings_path: Option<&'a Path>,
    pub cache_path: Option<&'a Path>,
    pub provider_key: Option<&'a str>,
    pub base_url: Option<&'a str>,
    pub api_key: Option<&'a str>,
    pub timeout: Duration,
}

impl Default for MultimodalProbeOptions<'_> {
    fn default() -> Self {
        Self {
            settings_path: None,
            cache_path: None,
            provider_key: None,
            base_url: None,
            api_key: None,
            timeout: Duration::from_secs(5),
        }
    }
}

impl MultiModalSpec {
    fn no_images() -> Self {
        Self {
            support_multimodal: false,
            formats: default_formats(),
            max_images_per_message: 20,
        }
    }

    fn default_vl() -> Self {
        Self {
            support_multimodal: true,
            formats: default_formats(),
            max_images_per_message: 20,
        }
    }
}

pub fn builtin_multimodal_models() -> BTreeSet<String> {
    provider_keys()
        .iter()
        .filter_map(|key| provider_descriptor(key))
        .flat_map(|descriptor| descriptor.models)
        .filter(|model| model.support_multimodal)
        .map(|model| model.id)
        .collect()
}

pub fn get_multimodal_spec(model: &str, settings_path: Option<&Path>) -> MultiModalSpec {
    if let Some(spec) = settings_or_builtin_spec(model, settings_path) {
        return spec;
    }

    MultiModalSpec::no_images()
}

pub fn is_model_multimodal(model: &str, settings_path: Option<&Path>) -> bool {
    get_multimodal_spec(model, settings_path).support_multimodal
}

pub fn is_model_multimodal_with_probe(model: &str, options: MultimodalProbeOptions<'_>) -> bool {
    if let Some(spec) = settings_or_builtin_spec(model, options.settings_path) {
        return spec.support_multimodal;
    }

    if options.provider_key != Some("openapi_compatible") {
        return false;
    }
    let Some(base_url) = options.base_url else {
        return false;
    };
    let Some(cache_path) = options.cache_path else {
        return probe_openapi_compatible(base_url, options.api_key, model, options.timeout)
            .unwrap_or(false);
    };

    let mut cache = AutoDetectCache::new(cache_path);
    if let Some(cached) = cache.get(base_url, model) {
        return cached;
    }
    if let Some(result) =
        probe_openapi_compatible(base_url, options.api_key, model, options.timeout)
    {
        cache.set(base_url, model, result);
        let _ = cache.flush();
        return result;
    }
    false
}

fn settings_or_builtin_spec(model: &str, settings_path: Option<&Path>) -> Option<MultiModalSpec> {
    if let Some(spec) = settings_path
        .map(load_settings_overrides)
        .unwrap_or_default()
        .remove(model)
    {
        return Some(spec);
    }

    builtin_multimodal_models()
        .contains(model)
        .then(MultiModalSpec::default_vl)
}

fn default_formats() -> Vec<String> {
    DEFAULT_FORMATS
        .iter()
        .map(|format| (*format).to_owned())
        .collect()
}
