use serde::Serialize;
use uuid::Uuid;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LifecycleState {
    Stopped,
    Starting,
    Running,
    Quiescing,
    Stopping,
    Restarting,
    Updating,
    Recovering,
}

#[derive(Clone, Debug)]
pub struct LifecycleCoordinator {
    pub state: LifecycleState,
    pub operation_id: Uuid,
    pub bootstrap_operation_id: Uuid,
    pub bootstrap_compatible: bool,
    pub healthy_origin: Option<url::Url>,
    pub local_picker_bootstrap: Option<Uuid>,
    pub remote_picker: Option<RemotePickerOperation>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RemotePickerPhase {
    DialogOpen,
    CommitSent,
    Settling,
}

#[derive(Clone, Debug)]
pub struct RemotePickerOperation {
    pub picker_operation_id: Uuid,
    pub source_generation: u64,
    pub phase: RemotePickerPhase,
}

impl Default for LifecycleCoordinator {
    fn default() -> Self {
        Self {
            state: LifecycleState::Stopped,
            operation_id: Uuid::new_v4(),
            bootstrap_operation_id: Uuid::new_v4(),
            bootstrap_compatible: false,
            healthy_origin: None,
            local_picker_bootstrap: None,
            remote_picker: None,
        }
    }
}

impl LifecycleCoordinator {
    pub fn complete_bootstrap_check(
        &mut self,
        operation_id: Uuid,
        compatible: bool,
    ) -> Result<(), &'static str> {
        if operation_id != self.bootstrap_operation_id {
            return Err("stale bootstrap operation");
        }
        self.bootstrap_compatible = compatible;
        Ok(())
    }

    pub fn require_bootstrap(&self, operation_id: Uuid) -> Result<(), &'static str> {
        if operation_id != self.bootstrap_operation_id {
            return Err("stale bootstrap operation");
        }
        if !self.bootstrap_compatible {
            return Err("WebView compatibility check has not passed");
        }
        Ok(())
    }

    pub fn begin(&mut self, next: LifecycleState) -> Uuid {
        if next != LifecycleState::Running {
            self.remote_picker = match self.remote_picker.take() {
                Some(mut operation) if operation.phase == RemotePickerPhase::CommitSent => {
                    operation.phase = RemotePickerPhase::Settling;
                    Some(operation)
                }
                _ => None,
            };
        }
        self.state = next;
        self.operation_id = Uuid::new_v4();
        self.operation_id
    }

    pub fn begin_bootstrap(&mut self) -> Uuid {
        self.bootstrap_operation_id = Uuid::new_v4();
        self.bootstrap_compatible = false;
        self.local_picker_bootstrap = None;
        self.remote_picker = None;
        self.bootstrap_operation_id
    }

    pub fn begin_local_picker(&mut self, operation_id: Uuid) -> Result<(), &'static str> {
        self.require_bootstrap(operation_id)?;
        if self.local_picker_bootstrap.is_some() {
            return Err("a Desktop project picker is already active");
        }
        self.local_picker_bootstrap = Some(operation_id);
        Ok(())
    }

    pub fn finish_local_picker(&mut self, operation_id: Uuid) -> Result<(), &'static str> {
        if self.local_picker_bootstrap != Some(operation_id) {
            return Err("stale Desktop bootstrap picker completion");
        }
        self.local_picker_bootstrap = None;
        Ok(())
    }

    pub fn cancel_local_picker(&mut self, operation_id: Uuid) {
        if self.local_picker_bootstrap == Some(operation_id) {
            self.local_picker_bootstrap = None;
        }
    }

    pub fn begin_remote_picker(&mut self, source_generation: u64) -> Result<Uuid, &'static str> {
        if self.state != LifecycleState::Running {
            return Err("Desktop sidecar is not running");
        }
        if self.remote_picker.is_some() {
            return Err("a Desktop project picker is already active");
        }
        let picker_operation_id = Uuid::new_v4();
        self.remote_picker = Some(RemotePickerOperation {
            picker_operation_id,
            source_generation,
            phase: RemotePickerPhase::DialogOpen,
        });
        Ok(picker_operation_id)
    }

    pub fn commit_remote_picker(
        &mut self,
        picker_operation_id: Uuid,
        source_generation: u64,
    ) -> Result<(), &'static str> {
        let operation = self
            .remote_picker
            .as_mut()
            .ok_or("Desktop project picker is no longer active")?;
        if operation.picker_operation_id != picker_operation_id
            || operation.source_generation != source_generation
            || operation.phase != RemotePickerPhase::DialogOpen
            || self.state != LifecycleState::Running
        {
            return Err("stale Desktop project picker completion");
        }
        operation.phase = RemotePickerPhase::CommitSent;
        Ok(())
    }

    pub fn finish_remote_picker(
        &mut self,
        picker_operation_id: Uuid,
        source_generation: u64,
    ) -> Result<(), &'static str> {
        let operation = self
            .remote_picker
            .as_ref()
            .ok_or("Desktop project picker is no longer active")?;
        if operation.picker_operation_id != picker_operation_id
            || operation.source_generation != source_generation
        {
            return Err("stale Desktop project picker acknowledgement");
        }
        self.remote_picker = None;
        Ok(())
    }

    pub fn cancel_remote_picker(&mut self, picker_operation_id: Uuid, source_generation: u64) {
        if self.remote_picker.as_ref().is_some_and(|operation| {
            operation.picker_operation_id == picker_operation_id
                && operation.source_generation == source_generation
        }) {
            self.remote_picker = None;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_bootstrap_completion_never_unlocks_startup() {
        let mut coordinator = LifecycleCoordinator::default();
        let current = coordinator.bootstrap_operation_id;
        assert!(coordinator
            .complete_bootstrap_check(Uuid::new_v4(), true)
            .is_err());
        assert!(coordinator.require_bootstrap(current).is_err());
        coordinator.complete_bootstrap_check(current, true).unwrap();
        assert!(coordinator.require_bootstrap(current).is_ok());
    }

    #[test]
    fn local_picker_is_single_flight_and_bootstrap_scoped() {
        let mut coordinator = LifecycleCoordinator::default();
        let operation = coordinator.bootstrap_operation_id;
        coordinator
            .complete_bootstrap_check(operation, true)
            .unwrap();
        coordinator.begin_local_picker(operation).unwrap();
        assert!(coordinator.begin_local_picker(operation).is_err());
        let next = coordinator.begin_bootstrap();
        assert_ne!(operation, next);
        assert!(coordinator.finish_local_picker(operation).is_err());
        coordinator.complete_bootstrap_check(next, true).unwrap();
        coordinator.begin_local_picker(next).unwrap();
        coordinator.finish_local_picker(next).unwrap();
    }

    #[test]
    fn remote_picker_rejects_duplicates_and_stale_acknowledgements() {
        let mut coordinator = LifecycleCoordinator {
            state: LifecycleState::Running,
            ..LifecycleCoordinator::default()
        };
        let operation = coordinator.begin_remote_picker(7).unwrap();
        assert!(coordinator.begin_remote_picker(7).is_err());
        assert!(coordinator.commit_remote_picker(operation, 8).is_err());
        coordinator.commit_remote_picker(operation, 7).unwrap();
        assert_eq!(
            coordinator.remote_picker.as_ref().unwrap().phase,
            RemotePickerPhase::CommitSent
        );
        assert!(coordinator.finish_remote_picker(Uuid::new_v4(), 7).is_err());
        coordinator.finish_remote_picker(operation, 7).unwrap();
        assert!(coordinator.remote_picker.is_none());
    }

    #[test]
    fn lifecycle_transition_cancels_dialog_but_settles_sent_commit() {
        let mut coordinator = LifecycleCoordinator {
            state: LifecycleState::Running,
            ..LifecycleCoordinator::default()
        };
        coordinator.begin_remote_picker(1).unwrap();
        coordinator.begin(LifecycleState::Quiescing);
        assert!(coordinator.remote_picker.is_none());

        coordinator.state = LifecycleState::Running;
        let operation = coordinator.begin_remote_picker(2).unwrap();
        coordinator.commit_remote_picker(operation, 2).unwrap();
        coordinator.begin(LifecycleState::Quiescing);
        assert_eq!(
            coordinator.remote_picker.as_ref().unwrap().phase,
            RemotePickerPhase::Settling
        );
        coordinator.finish_remote_picker(operation, 2).unwrap();
    }
}
