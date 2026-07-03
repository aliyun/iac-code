use crate::convert::SessionUpdate;
use crate::slash_registry::AcpSlashRegistry;

use super::model::{AcpAgent, AcpClient};

pub(super) fn dispatch_slash_command<A>(
    session_id: &str,
    prompt_text: &str,
    agent: &mut A,
    client: &mut dyn AcpClient,
) -> bool
where
    A: AcpAgent,
{
    let slash_registry = AcpSlashRegistry;
    if !slash_registry.is_slash_command(prompt_text) {
        return false;
    }

    let result = slash_registry.execute(prompt_text, agent);
    client.session_update(session_id, SessionUpdate::agent_message(result));
    true
}
