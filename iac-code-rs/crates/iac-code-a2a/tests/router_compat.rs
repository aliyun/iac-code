use iac_code_a2a::router::{A2ARoute, A2ARouteError, A2ARouter, RouteResolveOptions};

#[test]
fn router_selects_explicit_route_name_with_highest_priority() {
    let router = A2ARouter::new(vec![
        route("template", "http://template", &["iac_generation"], &["ros"]),
        route("review", "http://review", &["iac_review"], &["review"]),
    ]);

    let selected = router
        .resolve(RouteResolveOptions {
            name: Some("template"),
            skill: Some("iac_review"),
            prompt: Some("review"),
        })
        .expect("route");

    assert_eq!(selected.url, "http://template");
}

#[test]
fn router_selects_by_skill_or_prompt_tag_and_name() {
    let router = A2ARouter::new(vec![
        route("template", "http://template", &["iac_generation"], &["ros"]),
        route("review", "http://review", &["iac_review"], &["review"]),
        route("terraform", "http://tf", &[], &["terraform"]),
    ]);

    assert_eq!(
        router
            .resolve(RouteResolveOptions {
                skill: Some("iac_review"),
                ..RouteResolveOptions::default()
            })
            .expect("route")
            .name,
        "review"
    );
    assert_eq!(
        router
            .resolve(RouteResolveOptions {
                prompt: Some("convert this terraform module"),
                ..RouteResolveOptions::default()
            })
            .expect("route")
            .name,
        "terraform"
    );
    assert_eq!(
        router
            .resolve(RouteResolveOptions {
                prompt: Some("Template, please."),
                ..RouteResolveOptions::default()
            })
            .expect("route")
            .name,
        "template"
    );
}

#[test]
fn router_supports_custom_prompt_matcher() {
    let router = A2ARouter::new(vec![
        route("one", "http://one", &[], &[]),
        route("two", "http://two", &[], &[]),
    ]);

    let selected = router
        .resolve_with_matcher(
            RouteResolveOptions {
                prompt: Some("pick-two"),
                ..RouteResolveOptions::default()
            },
            |prompt, route| prompt == "pick-two" && route.name == "two",
        )
        .expect("route");

    assert_eq!(selected.url, "http://two");
}

#[test]
fn router_reports_ambiguous_matches_with_candidate_names() {
    let router = A2ARouter::new(vec![
        route("one", "http://one", &[], &["ros"]),
        route("two", "http://two", &[], &["ros"]),
    ]);

    let error = router
        .resolve(RouteResolveOptions {
            prompt: Some("build ros template"),
            ..RouteResolveOptions::default()
        })
        .expect_err("ambiguous");

    assert_eq!(
        error,
        A2ARouteError::Ambiguous("Ambiguous A2A route. Candidates: one, two".to_owned())
    );
    assert_eq!(
        error.to_string(),
        "Ambiguous A2A route. Candidates: one, two"
    );
}

#[test]
fn router_reports_missing_route_with_known_names() {
    let router = A2ARouter::new(vec![route("known", "http://known", &[], &[])]);

    let explicit = router
        .resolve(RouteResolveOptions {
            name: Some("missing"),
            ..RouteResolveOptions::default()
        })
        .expect_err("missing explicit route");
    assert_eq!(
        explicit.to_string(),
        "Unknown A2A route 'missing'. Known routes: known"
    );

    let unmatched = router
        .resolve(RouteResolveOptions {
            prompt: Some("no match"),
            ..RouteResolveOptions::default()
        })
        .expect_err("missing prompt route");
    assert_eq!(
        unmatched.to_string(),
        "No A2A route matched. Known routes: known"
    );
}

fn route(name: &str, url: &str, skills: &[&str], tags: &[&str]) -> A2ARoute {
    A2ARoute {
        name: name.to_owned(),
        url: url.to_owned(),
        skills: skills.iter().map(|value| (*value).to_owned()).collect(),
        tags: tags.iter().map(|value| (*value).to_owned()).collect(),
    }
}
