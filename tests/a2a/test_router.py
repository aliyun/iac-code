import pytest

from iac_code.a2a.router import A2ARoute, A2ARouter, AmbiguousA2ARouteError, MissingA2ARouteError


def test_router_selects_explicit_route_name() -> None:
    router = A2ARouter([A2ARoute(name="template", url="http://template", skills=["iac_generation"], tags=["ros"])])

    assert router.resolve(name="template").url == "http://template"


def test_router_selects_by_skill() -> None:
    router = A2ARouter(
        [
            A2ARoute(name="template", url="http://template", skills=["iac_generation"], tags=["ros"]),
            A2ARoute(name="review", url="http://review", skills=["iac_review"], tags=["review"]),
        ]
    )

    assert router.resolve(skill="iac_review").name == "review"


def test_router_selects_by_tag_from_prompt() -> None:
    router = A2ARouter([A2ARoute(name="terraform", url="http://tf", skills=[], tags=["terraform"])])

    assert router.resolve(prompt="convert this terraform module").name == "terraform"


def test_router_caches_default_prompt_terms() -> None:
    class CountingTag(str):
        calls = 0

        def lower(self):
            CountingTag.calls += 1
            return super().lower()

    router = A2ARouter([A2ARoute(name="terraform", url="http://tf", skills=[], tags=[CountingTag("terraform")])])
    assert CountingTag.calls == 1

    assert router.resolve(prompt="terraform please").name == "terraform"
    assert router.resolve(prompt="another terraform request").name == "terraform"
    assert CountingTag.calls == 1


def test_router_reports_ambiguous_matches() -> None:
    router = A2ARouter(
        [
            A2ARoute(name="one", url="http://one", skills=[], tags=["ros"]),
            A2ARoute(name="two", url="http://two", skills=[], tags=["ros"]),
        ]
    )

    with pytest.raises(AmbiguousA2ARouteError, match="one, two"):
        router.resolve(prompt="build ros template")


def test_router_reports_missing_route_with_known_names() -> None:
    router = A2ARouter([A2ARoute(name="known", url="http://known", skills=[], tags=[])])

    with pytest.raises(MissingA2ARouteError, match="known"):
        router.resolve(name="missing")
