#[derive(Clone, Copy)]
pub(super) enum CommandOutput {
    Always,
    NonEmpty,
}

impl CommandOutput {
    pub(super) fn for_a2a_client_command(command: &str) -> Self {
        if command == "call" {
            Self::NonEmpty
        } else {
            Self::Always
        }
    }

    fn should_print(self, output: &str) -> bool {
        match self {
            Self::Always => true,
            Self::NonEmpty => !output.is_empty(),
        }
    }
}

pub(super) fn finish_command_result(
    result: Result<String, String>,
    output_policy: CommandOutput,
) -> Option<i32> {
    match result {
        Ok(output) => {
            if output_policy.should_print(&output) {
                println!("{output}");
            }
            Some(0)
        }
        Err(error) => {
            eprintln!("{error}");
            Some(1)
        }
    }
}
