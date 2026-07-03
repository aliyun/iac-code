pub(super) fn handle_a2a_client_help(args: &[String]) -> bool {
    if args.first().map(String::as_str) != Some("a2a-client") {
        return false;
    }

    let mut index = 1usize;
    while index < args.len() {
        match args[index].as_str() {
            "--help" | "-h" => {
                print_a2a_client_help();
                return true;
            }
            "--config" => index += 2,
            option if option.starts_with('-') => return false,
            command => return handle_a2a_client_command_help(command, args, index),
        }
    }
    false
}

fn handle_a2a_client_command_help(command: &str, args: &[String], command_index: usize) -> bool {
    match command {
        "call" if command_help_requested(args, command_index) => {
            print_a2a_client_call_help();
            true
        }
        "discover" if command_help_requested(args, command_index) => {
            print_a2a_client_discover_help();
            true
        }
        "task-get" if command_help_requested(args, command_index) => {
            print_a2a_client_task_get_help();
            true
        }
        "task-list" if command_help_requested(args, command_index) => {
            print_a2a_client_task_list_help();
            true
        }
        "task-cancel" if command_help_requested(args, command_index) => {
            print_a2a_client_task_cancel_help();
            true
        }
        "task-subscribe" if command_help_requested(args, command_index) => {
            print_a2a_client_task_subscribe_help();
            true
        }
        "push-config-create" if command_help_requested(args, command_index) => {
            print_a2a_client_push_config_create_help();
            true
        }
        "push-config-get" if command_help_requested(args, command_index) => {
            print_a2a_client_push_config_get_help();
            true
        }
        "push-config-list" if command_help_requested(args, command_index) => {
            print_a2a_client_push_config_list_help();
            true
        }
        "push-config-delete" if command_help_requested(args, command_index) => {
            print_a2a_client_push_config_delete_help();
            true
        }
        "extended-card" if command_help_requested(args, command_index) => {
            print_a2a_client_extended_card_help();
            true
        }
        "route-preview" if command_help_requested(args, command_index) => {
            print_a2a_client_route_preview_help();
            true
        }
        _ => false,
    }
}

fn command_help_requested(args: &[String], command_index: usize) -> bool {
    let help_index = command_index + 1;
    args.len() == help_index + 1 && matches!(args[help_index].as_str(), "--help" | "-h")
}

fn print_a2a_client_help() {
    println!(
        "Usage: iac-code a2a-client [OPTIONS] COMMAND [ARGS]...\n\
\n\
Use iac-code as an A2A client.\n\
\n\
Options:\n\
      --config <TEXT>  YAML config file containing A2A client options\n\
  -h, --help           Show this message and exit\n\
\n\
Commands:\n\
  call                Send a prompt to an A2A JSON-RPC endpoint.\n\
  discover            Discover an A2A Agent Card.\n\
  task-get            Get an A2A task.\n\
  task-list           List A2A tasks.\n\
  task-cancel         Cancel an A2A task.\n\
  task-subscribe      Subscribe to an A2A task event stream.\n\
  push-config-create  Create an A2A task push notification config.\n\
  push-config-get     Get an A2A task push notification config.\n\
  push-config-list    List A2A task push notification configs.\n\
  push-config-delete  Delete an A2A task push notification config.\n\
  extended-card       Get an authenticated extended A2A Agent Card.\n\
  route-preview       Preview A2A route resolution."
    );
}

fn print_a2a_client_call_help() {
    println!(
        "Usage: iac-code a2a-client call [OPTIONS]\n\
\n\
Send a prompt to an A2A JSON-RPC endpoint.\n\
\n\
Options:\n\
      --url <TEXT>               A2A JSON-RPC endpoint URL\n\
      --route <TEXT>             Route spec: name=url;skills=skill1,skill2;tags=tag1,tag2\n\
      --route-name <TEXT>        Named A2A route to call\n\
  -p, --prompt <TEXT>            Prompt to send\n\
      --cwd <TEXT>               Working directory metadata to send with the request [default: .]\n\
      --context-id <TEXT>        A2A context ID to continue\n\
      --model <TEXT>             Model metadata to send with the request\n\
      --token <TEXT>             Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>    Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>    Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>           API key for A2A HTTP requests\n\
      --api-key-header <TEXT>    HTTP header name for A2A API key [default: X-API-Key]\n\
      --verify-card-secret <TEXT>     Secret used to verify the A2A Agent Card\n\
      --verify-card-jwks-url <TEXT>   Remote JWKS URL used to verify the A2A Agent Card\n\
      --require-card-signature        Require a valid A2A Agent Card signature\n\
      --timeout <FLOAT>         A2A call timeout in seconds [default: 30.0]\n\
      --stream                  Use A2A streaming message delivery\n\
  -h, --help                    Show this message and exit"
    );
}

fn print_a2a_client_discover_help() {
    println!(
        "Usage: iac-code a2a-client discover [OPTIONS]\n\
\n\
Discover an A2A Agent Card.\n\
\n\
Options:\n\
      --url <TEXT>                     A2A server base URL or Agent Card URL\n\
      --token <TEXT>                   Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>          Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>          Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>                 API key for A2A HTTP requests\n\
      --api-key-header <TEXT>          HTTP header name for A2A API key [default: X-API-Key]\n\
      --verify-card-secret <TEXT>      Secret used to verify the A2A Agent Card\n\
      --verify-card-jwks-url <TEXT>    Remote JWKS URL used to verify the A2A Agent Card\n\
      --require-card-signature         Require a valid A2A Agent Card signature\n\
  -h, --help                           Show this message and exit"
    );
}

fn print_a2a_client_task_get_help() {
    println!(
        "Usage: iac-code a2a-client task-get [OPTIONS]\n\
\n\
Get an A2A task.\n\
\n\
Options:\n\
      --url <TEXT>                     A2A JSON-RPC endpoint URL\n\
      --task-id <TEXT>                 A2A task ID\n\
      --history-length <INTEGER>       Maximum task history items to return\n\
      --token <TEXT>                   Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>          Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>          Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>                 API key for A2A HTTP requests\n\
      --api-key-header <TEXT>          HTTP header name for A2A API key [default: X-API-Key]\n\
  -h, --help                           Show this message and exit"
    );
}

fn print_a2a_client_task_list_help() {
    println!(
        "Usage: iac-code a2a-client task-list [OPTIONS]\n\
\n\
List A2A tasks.\n\
\n\
Options:\n\
      --url <TEXT>                     A2A JSON-RPC endpoint URL\n\
      --context-id <TEXT>              Filter by A2A context ID\n\
      --status <TEXT>                  Filter by A2A task state\n\
      --page-size <INTEGER>            Maximum tasks to return\n\
      --page-token <TEXT>              Pagination token\n\
      --include-artifacts              Include task artifacts\n\
      --output <TEXT>                  Output format: table or json [default: table]\n\
      --token <TEXT>                   Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>          Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>          Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>                 API key for A2A HTTP requests\n\
      --api-key-header <TEXT>          HTTP header name for A2A API key [default: X-API-Key]\n\
  -h, --help                           Show this message and exit"
    );
}

fn print_a2a_client_task_cancel_help() {
    println!(
        "Usage: iac-code a2a-client task-cancel [OPTIONS]\n\
\n\
Cancel an A2A task.\n\
\n\
Options:\n\
      --url <TEXT>                     A2A JSON-RPC endpoint URL\n\
      --task-id <TEXT>                 A2A task ID\n\
      --token <TEXT>                   Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>          Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>          Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>                 API key for A2A HTTP requests\n\
      --api-key-header <TEXT>          HTTP header name for A2A API key [default: X-API-Key]\n\
  -h, --help                           Show this message and exit"
    );
}

fn print_a2a_client_task_subscribe_help() {
    println!(
        "Usage: iac-code a2a-client task-subscribe [OPTIONS]\n\
\n\
Subscribe to an A2A task event stream.\n\
\n\
Options:\n\
      --url <TEXT>             A2A JSON-RPC endpoint URL\n\
      --task-id <TEXT>         A2A task ID\n\
      --token <TEXT>           Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>  Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>  Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>         API key for A2A HTTP requests\n\
      --api-key-header <TEXT>  HTTP header name for A2A API key [default: X-API-Key]\n\
  -h, --help                   Show this message and exit"
    );
}

fn print_a2a_client_push_config_create_help() {
    println!(
        "Usage: iac-code a2a-client push-config-create [OPTIONS]\n\
\n\
Create an A2A task push notification config.\n\
\n\
Options:\n\
      --url <TEXT>                    A2A JSON-RPC endpoint URL\n\
      --task-id <TEXT>                A2A task ID\n\
      --config-id <TEXT>              Push config ID\n\
      --callback-url <TEXT>           Push callback URL\n\
      --notification-token <TEXT>     Notification verification token\n\
      --auth-scheme <TEXT>            Callback authentication scheme\n\
      --auth-credentials <TEXT>       Callback authentication credentials\n\
      --token <TEXT>                  Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>         Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>         Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>                API key for A2A HTTP requests\n\
      --api-key-header <TEXT>         HTTP header name for A2A API key [default: X-API-Key]\n\
  -h, --help                          Show this message and exit"
    );
}

fn print_a2a_client_push_config_get_help() {
    println!(
        "Usage: iac-code a2a-client push-config-get [OPTIONS]\n\
\n\
Get an A2A task push notification config.\n\
\n\
Options:\n\
      --url <TEXT>             A2A JSON-RPC endpoint URL\n\
      --task-id <TEXT>         A2A task ID\n\
      --config-id <TEXT>       Push config ID\n\
      --token <TEXT>           Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>  Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>  Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>         API key for A2A HTTP requests\n\
      --api-key-header <TEXT>  HTTP header name for A2A API key [default: X-API-Key]\n\
  -h, --help                   Show this message and exit"
    );
}

fn print_a2a_client_push_config_list_help() {
    println!(
        "Usage: iac-code a2a-client push-config-list [OPTIONS]\n\
\n\
List A2A task push notification configs.\n\
\n\
Options:\n\
      --url <TEXT>             A2A JSON-RPC endpoint URL\n\
      --task-id <TEXT>         A2A task ID\n\
      --page-size <INTEGER>    Maximum configs to return\n\
      --page-token <TEXT>      Pagination token\n\
      --token <TEXT>           Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>  Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>  Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>         API key for A2A HTTP requests\n\
      --api-key-header <TEXT>  HTTP header name for A2A API key [default: X-API-Key]\n\
  -h, --help                   Show this message and exit"
    );
}

fn print_a2a_client_push_config_delete_help() {
    println!(
        "Usage: iac-code a2a-client push-config-delete [OPTIONS]\n\
\n\
Delete an A2A task push notification config.\n\
\n\
Options:\n\
      --url <TEXT>             A2A JSON-RPC endpoint URL\n\
      --task-id <TEXT>         A2A task ID\n\
      --config-id <TEXT>       Push config ID\n\
      --token <TEXT>           Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>  Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>  Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>         API key for A2A HTTP requests\n\
      --api-key-header <TEXT>  HTTP header name for A2A API key [default: X-API-Key]\n\
  -h, --help                   Show this message and exit"
    );
}

fn print_a2a_client_extended_card_help() {
    println!(
        "Usage: iac-code a2a-client extended-card [OPTIONS]\n\
\n\
Get an authenticated extended A2A Agent Card.\n\
\n\
Options:\n\
      --url <TEXT>             A2A JSON-RPC endpoint URL\n\
      --token <TEXT>           Bearer token for A2A HTTP requests\n\
      --basic-username <TEXT>  Basic auth username for A2A HTTP requests\n\
      --basic-password <TEXT>  Basic auth password for A2A HTTP requests\n\
      --api-key <TEXT>         API key for A2A HTTP requests\n\
      --api-key-header <TEXT>  HTTP header name for A2A API key [default: X-API-Key]\n\
  -h, --help                   Show this message and exit"
    );
}

fn print_a2a_client_route_preview_help() {
    println!(
        "Usage: iac-code a2a-client route-preview [OPTIONS]\n\
\n\
Preview A2A route resolution.\n\
\n\
Options:\n\
      --route <TEXT>                   Route spec: name=url;skills=skill1,skill2;tags=tag1,tag2\n\
      --name <TEXT>                    Route name to resolve\n\
      --skill <TEXT>                   Skill ID to resolve\n\
      --prompt <TEXT>                  Prompt text used for tag/name route matching\n\
      --route-state-dir <TEXT>         Directory for persisted A2A routes\n\
      --persistence-dir <TEXT>         Directory for persisted A2A routes\n\
      --save-routes                    Save the provided routes as a route snapshot\n\
  -h, --help                           Show this message and exit"
    );
}
