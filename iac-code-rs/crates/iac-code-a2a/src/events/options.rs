use std::collections::BTreeSet;

use crate::artifacts::A2AArtifactStore;

use super::A2AExposureType;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PublishOptions {
    pub auto_approve_permissions: bool,
    pub exposure_types: BTreeSet<A2AExposureType>,
    pub artifact_store: Option<A2AArtifactStore>,
}

impl Default for PublishOptions {
    fn default() -> Self {
        Self {
            auto_approve_permissions: false,
            exposure_types: BTreeSet::from([A2AExposureType::ToolTrace]),
            artifact_store: None,
        }
    }
}

impl PublishOptions {
    pub fn with_exposure<I>(mut self, exposure_types: I) -> Self
    where
        I: IntoIterator<Item = A2AExposureType>,
    {
        self.exposure_types = exposure_types.into_iter().collect();
        self
    }

    pub fn with_artifact_store(mut self, artifact_store: A2AArtifactStore) -> Self {
        self.artifact_store = Some(artifact_store);
        self
    }
}
