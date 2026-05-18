from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


class MissingA2ARouteError(ValueError):
    pass


class AmbiguousA2ARouteError(ValueError):
    pass


@dataclass(frozen=True)
class A2ARoute:
    name: str
    url: str
    skills: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


RouteMatcher = Callable[[str, A2ARoute], bool]


@dataclass(frozen=True)
class _RoutePromptTerms:
    route: A2ARoute
    tags: frozenset[str]
    names: frozenset[str]


def _prompt_words(prompt: str) -> set[str]:
    return set(prompt.lower().replace(",", " ").replace(".", " ").split())


def _default_prompt_matcher(prompt: str, route: A2ARoute) -> bool:
    """Default prompt matcher for callers that use the standalone function.

    This is a simple keyword overlap strategy — it does NOT perform semantic
    or fuzzy matching.  For more advanced routing, supply a custom *match_fn*
    to :meth:`A2ARouter.resolve`.
    """
    prompt_words = _prompt_words(prompt)
    tag_match = prompt_words.intersection({tag.lower() for tag in route.tags})
    name_match = prompt_words.intersection({route.name.lower()})
    return bool(tag_match or name_match)


class A2ARouter:
    def __init__(self, routes: list[A2ARoute]) -> None:
        self._routes = routes
        self._prompt_terms = [
            _RoutePromptTerms(
                route=route,
                tags=frozenset(tag.lower() for tag in route.tags),
                names=frozenset({route.name.lower()}),
            )
            for route in routes
        ]

    @property
    def route_names(self) -> list[str]:
        return [route.name for route in self._routes]

    def resolve(
        self,
        *,
        name: str | None = None,
        skill: str | None = None,
        prompt: str | None = None,
        match_fn: RouteMatcher | None = None,
    ) -> A2ARoute:
        """Resolve a route by exact name, skill id, or prompt matching.

        Args:
            name: Exact route name lookup (highest priority).
            skill: Match routes containing this skill id.
            prompt: Match routes via keyword overlap or custom *match_fn*.
            match_fn: Optional custom matcher ``(prompt, route) -> bool``.
                      Falls back to simple keyword intersection when not provided.
        """
        if name:
            for route in self._routes:
                if route.name == name:
                    return route
            raise MissingA2ARouteError(f"Unknown A2A route {name!r}. Known routes: {', '.join(self.route_names)}")

        matches: list[A2ARoute] = []
        if skill:
            matches = [route for route in self._routes if skill in route.skills]
        if not matches and prompt:
            if match_fn is None:
                prompt_words = _prompt_words(prompt)
                matches = [
                    terms.route
                    for terms in self._prompt_terms
                    if prompt_words.intersection(terms.tags) or prompt_words.intersection(terms.names)
                ]
            else:
                matches = [route for route in self._routes if match_fn(prompt, route)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousA2ARouteError(
                f"Ambiguous A2A route. Candidates: {', '.join(route.name for route in matches)}"
            )
        raise MissingA2ARouteError(f"No A2A route matched. Known routes: {', '.join(self.route_names)}")
