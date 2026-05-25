"""Tests for api_hooks before_call decorator enhancement."""

from __future__ import annotations

from iac_code.tools.cloud.aliyun.api_hooks import _hooks, before_call, run_hooks


class TestBeforeCallList:
    def setup_method(self) -> None:
        self._saved = dict(_hooks)
        _hooks.clear()

    def teardown_method(self) -> None:
        _hooks.clear()
        _hooks.update(self._saved)

    def test_single_action_str(self) -> None:
        @before_call("ros", "ValidateTemplate")
        def hook(product, action, params):
            return None

        assert ("ros", "ValidateTemplate") in _hooks
        assert hook in _hooks[("ros", "ValidateTemplate")]

    def test_action_list(self) -> None:
        @before_call("ros", ["CreateStack", "UpdateStack"])
        def hook(product, action, params):
            return None

        assert hook in _hooks[("ros", "CreateStack")]
        assert hook in _hooks[("ros", "UpdateStack")]

    def test_action_list_single_fn_instance(self) -> None:
        @before_call("ros", ["CreateStack", "UpdateStack", "PreviewStack"])
        def hook(product, action, params):
            return None

        assert _hooks[("ros", "CreateStack")][0] is _hooks[("ros", "UpdateStack")][0]
        assert _hooks[("ros", "UpdateStack")][0] is _hooks[("ros", "PreviewStack")][0]

    def test_run_hooks_with_list_registered(self) -> None:
        from iac_code.tools.base import ToolResult

        @before_call("ros", ["ActionA", "ActionB"])
        def hook(product, action, params):
            if params.get("fail"):
                return ToolResult.error("blocked")
            return None

        result = run_hooks("ros", "ActionA", {"fail": True})
        assert result is not None
        assert result.is_error

        result = run_hooks("ros", "ActionB", {})
        assert result is None

        result = run_hooks("ros", "ActionC", {})
        assert result is None
