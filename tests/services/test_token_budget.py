import pytest

from iac_code.services.token_budget import TokenBudget


class TestTokenBudget:
    def test_create_with_total(self):
        budget = TokenBudget(total=10000)
        assert budget.total == 10000
        assert budget.remaining == 10000
        assert budget.used == 0

    def test_consume(self):
        budget = TokenBudget(total=10000)
        budget.consume(3000)
        assert budget.remaining == 7000
        assert budget.used == 3000

    def test_is_exhausted(self):
        budget = TokenBudget(total=100)
        assert budget.is_exhausted is False
        budget.consume(100)
        assert budget.is_exhausted is True

    def test_usage_percent(self):
        budget = TokenBudget(total=10000)
        budget.consume(2500)
        assert budget.usage_percent == pytest.approx(25.0)

    def test_parse_shorthand(self):
        assert TokenBudget.parse_shorthand("500k") == 500_000
        assert TokenBudget.parse_shorthand("1m") == 1_000_000
        assert TokenBudget.parse_shorthand("1M") == 1_000_000
        assert TokenBudget.parse_shorthand("50000") == 50000
        assert TokenBudget.parse_shorthand("+200k") == 200_000

    def test_parse_shorthand_invalid(self):
        with pytest.raises(ValueError):
            TokenBudget.parse_shorthand("abc")

    def test_unlimited_budget(self):
        budget = TokenBudget.unlimited()
        assert budget.is_exhausted is False
        budget.consume(999_999_999)
        assert budget.is_exhausted is False

    def test_from_shorthand(self):
        budget = TokenBudget.from_shorthand("100k")
        assert budget.total == 100_000
