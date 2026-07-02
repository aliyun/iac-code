use iac_code_a2a::router::A2ARoute;

use super::cli_args::next_option_value;

#[derive(Clone, Debug, Default)]
pub(super) struct A2ARoutePreviewArgs {
    pub(super) routes: Vec<String>,
    pub(super) name: String,
    pub(super) skill: String,
    pub(super) prompt: String,
    pub(super) route_state_dir: String,
    pub(super) save_routes: bool,
}

pub(super) fn parse_a2a_route_preview_args(args: &[String]) -> Result<A2ARoutePreviewArgs, String> {
    let mut parsed = A2ARoutePreviewArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--route" => {
                parsed
                    .routes
                    .push(next_option_value(args, &mut index, "--route")?);
            }
            "--name" | "--route-name" => {
                let option = args[index].clone();
                parsed.name = next_option_value(args, &mut index, &option)?;
            }
            "--skill" => {
                parsed.skill = next_option_value(args, &mut index, "--skill")?;
            }
            "--prompt" => {
                parsed.prompt = next_option_value(args, &mut index, "--prompt")?;
            }
            "--route-state-dir" | "--persistence-dir" => {
                parsed.route_state_dir = next_option_value(args, &mut index, "--route-state-dir")?;
            }
            "--save-routes" => {
                parsed.save_routes = true;
                index += 1;
            }
            other => {
                return Err(format!("No such option: {other}"));
            }
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_route_spec(value: &str) -> Result<A2ARoute, String> {
    let parts = value
        .split(';')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    if parts.first().is_none_or(|part| !part.contains('=')) {
        return Err("A2A route must start with name=url.".to_owned());
    }

    let (name, url) = parts[0]
        .split_once('=')
        .expect("route part contains separator");
    let name = name.trim();
    let url = url.trim();
    if name.is_empty() || url.is_empty() {
        return Err("A2A route name and URL are required.".to_owned());
    }

    let mut skills = Vec::new();
    let mut tags = Vec::new();
    let mut legacy_parts = Vec::new();
    for part in parts.iter().skip(1) {
        let Some((key, raw)) = part.split_once('=') else {
            legacy_parts.push((*part).to_owned());
            continue;
        };
        let values = raw
            .split(',')
            .map(str::trim)
            .filter(|item| !item.is_empty())
            .map(ToOwned::to_owned)
            .collect::<Vec<_>>();
        match key {
            "skills" => skills = values,
            "tags" => tags = values,
            _ => {
                return Err(format!(
                    "Unknown A2A route segment '{}'. Expected skills or tags.",
                    key
                ));
            }
        }
    }
    if !legacy_parts.is_empty() && skills.is_empty() {
        skills = vec![legacy_parts[0].clone()];
    }
    if legacy_parts.len() > 1 && tags.is_empty() {
        tags = legacy_parts[1]
            .split(',')
            .map(str::trim)
            .filter(|item| !item.is_empty())
            .map(ToOwned::to_owned)
            .collect();
    }

    Ok(A2ARoute {
        name: name.to_owned(),
        url: url.to_owned(),
        skills,
        tags,
    })
}
