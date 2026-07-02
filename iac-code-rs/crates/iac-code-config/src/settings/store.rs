use std::collections::BTreeMap;

use crate::paths::ConfigPaths;
use crate::simple_yaml::{self, YamlValue};
use crate::{ConfigError, ConfigResult};

pub(super) fn load_settings(paths: &ConfigPaths) -> ConfigResult<BTreeMap<String, YamlValue>> {
    simple_yaml::load_yaml_map(&paths.settings_path).map_err(ConfigError::from)
}
