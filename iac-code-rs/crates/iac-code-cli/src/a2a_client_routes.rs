use iac_code_a2a::persistence::{A2APersistenceStore, A2ARouteSnapshot};
use iac_code_a2a::router::{A2ARouter, RouteResolveOptions};

use crate::a2a_client_args::A2ACallArgs;
use crate::a2a_client_format::format_a2a_route_json;
use crate::a2a_client_route_args::{parse_a2a_route_preview_args, parse_a2a_route_spec};
use crate::cli_args::non_empty_str;

pub(super) fn resolve_a2a_call_url(args: &A2ACallArgs) -> Result<String, String> {
    if !args.url.is_empty() {
        return Ok(args.url.clone());
    }
    if args.routes.is_empty() && args.route_name.is_empty() {
        return Err(
            "url is required. Provide --url or url in --config, or configure --route/--route-name."
                .to_owned(),
        );
    }
    let routes = args
        .routes
        .iter()
        .map(|route| parse_a2a_route_spec(route))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(A2ARouter::new(routes)
        .resolve(RouteResolveOptions {
            name: non_empty_str(&args.route_name),
            skill: None,
            prompt: non_empty_str(&args.prompt),
        })
        .map_err(|error| error.to_string())?
        .url)
}

pub(super) fn run_a2a_client_route_preview(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_route_preview_args(args)?;
    let routes = args
        .routes
        .iter()
        .map(|route| parse_a2a_route_spec(route))
        .collect::<Result<Vec<_>, _>>()?;
    if routes.is_empty() {
        return Err("At least one --route is required.".to_owned());
    }

    if args.save_routes || !args.route_state_dir.is_empty() {
        if args.route_state_dir.is_empty() {
            return Err("--route-state-dir is required with --save-routes.".to_owned());
        }
        A2APersistenceStore::new(&args.route_state_dir)
            .save_routes(
                routes
                    .iter()
                    .map(|route| A2ARouteSnapshot {
                        name: route.name.clone(),
                        url: route.url.clone(),
                        skills: route.skills.clone(),
                        tags: route.tags.clone(),
                    })
                    .collect(),
            )
            .map_err(|error| error.to_string())?;
    }

    let resolved = A2ARouter::new(routes)
        .resolve(RouteResolveOptions {
            name: non_empty_str(&args.name),
            skill: non_empty_str(&args.skill),
            prompt: non_empty_str(&args.prompt),
        })
        .map_err(|error| error.to_string())?;
    Ok(format_a2a_route_json(&resolved))
}
