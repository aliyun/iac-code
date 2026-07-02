use std::env;
use std::process;

fn main() {
    process::exit(iac_code_cli::run_cli(env::args().skip(1)));
}
