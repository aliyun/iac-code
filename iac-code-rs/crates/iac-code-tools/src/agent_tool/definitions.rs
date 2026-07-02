use super::model::AgentDefinition;

pub fn builtin_agent_definitions() -> Vec<AgentDefinition> {
    vec![
        AgentDefinition {
            agent_type: "general-purpose".into(),
            when_to_use: "Use for complex, multi-step tasks that require research, code changes, or coordinating multiple operations.".into(),
            tools: Some(vec!["*".into()]),
            disallowed_tools: vec!["agent".into()],
            max_turns: 100,
            model: "inherit".into(),
        },
        AgentDefinition {
            agent_type: "explore".into(),
            when_to_use: "Use to quickly find files, search code, or answer questions about the codebase. Read-only; cannot modify files.".into(),
            tools: Some(vec![
                "read_file".into(),
                "glob".into(),
                "grep".into(),
                "list_files".into(),
                "bash".into(),
            ]),
            disallowed_tools: vec!["write_file".into(), "edit_file".into(), "agent".into()],
            max_turns: 30,
            model: "inherit".into(),
        },
        AgentDefinition {
            agent_type: "plan".into(),
            when_to_use: "Use to plan implementation strategy, review architecture, or design solutions. Read-only, no execution.".into(),
            tools: Some(vec![
                "read_file".into(),
                "glob".into(),
                "grep".into(),
                "list_files".into(),
            ]),
            disallowed_tools: vec!["bash".into(), "write_file".into(), "edit_file".into(), "agent".into()],
            max_turns: 20,
            model: "inherit".into(),
        },
    ]
}

pub fn get_agent_definition(agent_type: &str) -> Option<AgentDefinition> {
    builtin_agent_definitions()
        .into_iter()
        .find(|definition| definition.agent_type == agent_type)
}
