use std::collections::BTreeSet;
use std::fmt;

type RouteMatcher<'a> = dyn Fn(&str, &A2ARoute) -> bool + 'a;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2ARoute {
    pub name: String,
    pub url: String,
    pub skills: Vec<String>,
    pub tags: Vec<String>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RouteResolveOptions<'a> {
    pub name: Option<&'a str>,
    pub skill: Option<&'a str>,
    pub prompt: Option<&'a str>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum A2ARouteError {
    Missing(String),
    Ambiguous(String),
}

impl fmt::Display for A2ARouteError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            A2ARouteError::Missing(message) | A2ARouteError::Ambiguous(message) => {
                formatter.write_str(message)
            }
        }
    }
}

impl std::error::Error for A2ARouteError {}

#[derive(Clone, Debug, PartialEq, Eq)]
struct RoutePromptTerms {
    route: A2ARoute,
    tags: BTreeSet<String>,
    names: BTreeSet<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2ARouter {
    routes: Vec<A2ARoute>,
    prompt_terms: Vec<RoutePromptTerms>,
}

impl A2ARouter {
    pub fn new(routes: Vec<A2ARoute>) -> Self {
        let prompt_terms = routes
            .iter()
            .map(|route| RoutePromptTerms {
                route: route.clone(),
                tags: route.tags.iter().map(|tag| tag.to_lowercase()).collect(),
                names: BTreeSet::from([route.name.to_lowercase()]),
            })
            .collect();
        Self {
            routes,
            prompt_terms,
        }
    }

    pub fn route_names(&self) -> Vec<String> {
        self.routes.iter().map(|route| route.name.clone()).collect()
    }

    pub fn resolve(&self, options: RouteResolveOptions<'_>) -> Result<A2ARoute, A2ARouteError> {
        self.resolve_inner(options, None)
    }

    pub fn resolve_with_matcher<F>(
        &self,
        options: RouteResolveOptions<'_>,
        match_fn: F,
    ) -> Result<A2ARoute, A2ARouteError>
    where
        F: Fn(&str, &A2ARoute) -> bool,
    {
        self.resolve_inner(options, Some(&match_fn))
    }

    fn resolve_inner(
        &self,
        options: RouteResolveOptions<'_>,
        match_fn: Option<&RouteMatcher<'_>>,
    ) -> Result<A2ARoute, A2ARouteError> {
        if let Some(name) = options.name.filter(|name| !name.is_empty()) {
            for route in &self.routes {
                if route.name == name {
                    return Ok(route.clone());
                }
            }
            return Err(A2ARouteError::Missing(format!(
                "Unknown A2A route '{}'. Known routes: {}",
                name.replace('\\', "\\\\").replace('\'', "\\'"),
                self.route_names().join(", ")
            )));
        }

        let mut matches = Vec::new();
        if let Some(skill) = options.skill.filter(|skill| !skill.is_empty()) {
            matches = self
                .routes
                .iter()
                .filter(|route| route.skills.iter().any(|candidate| candidate == skill))
                .cloned()
                .collect();
        }
        if matches.is_empty() {
            if let Some(prompt) = options.prompt.filter(|prompt| !prompt.is_empty()) {
                matches = if let Some(match_fn) = match_fn {
                    self.routes
                        .iter()
                        .filter(|route| match_fn(prompt, route))
                        .cloned()
                        .collect()
                } else {
                    let prompt_words = prompt_words(prompt);
                    self.prompt_terms
                        .iter()
                        .filter(|terms| {
                            !prompt_words.is_disjoint(&terms.tags)
                                || !prompt_words.is_disjoint(&terms.names)
                        })
                        .map(|terms| terms.route.clone())
                        .collect()
                };
            }
        }

        match matches.len() {
            1 => Ok(matches.pop().expect("single match")),
            len if len > 1 => Err(A2ARouteError::Ambiguous(format!(
                "Ambiguous A2A route. Candidates: {}",
                matches
                    .iter()
                    .map(|route| route.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            ))),
            _ => Err(A2ARouteError::Missing(format!(
                "No A2A route matched. Known routes: {}",
                self.route_names().join(", ")
            ))),
        }
    }
}

fn prompt_words(prompt: &str) -> BTreeSet<String> {
    prompt
        .to_lowercase()
        .replace([',', '.'], " ")
        .split_whitespace()
        .map(str::to_owned)
        .collect()
}
