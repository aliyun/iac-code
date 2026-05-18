import pytest

from iac_code.a2a.types import A2A_ID_MAX_LENGTH, validate_protocol_id


@pytest.mark.parametrize("value", ["abc", "abc-123", "abc_123", "abc.123", "abc:123"])
def test_validate_protocol_id_accepts_safe_values(value: str) -> None:
    assert validate_protocol_id(value) == value


@pytest.mark.parametrize("value", ["", "space value", "../x", "x/y", "x" * (A2A_ID_MAX_LENGTH + 1)])
def test_validate_protocol_id_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid A2A id"):
        validate_protocol_id(value)
