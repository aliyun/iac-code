import base64
import json
from copy import deepcopy

import pytest
from a2a.utils.signing import ProtectedHeader, create_agent_card_signer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from iac_code.a2a.signing import (
    ASYMMETRIC_SIGNATURE_ALGORITHM,
    SIGNATURE_ALGORITHM,
    _agent_card_from_dict,
    _agent_card_to_dict,
    canonicalize_agent_card,
    sign_agent_card_dict,
    verify_agent_card_dict,
)

SECRET = "s" * 32
OTHER_SECRET = "d" * 32


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _base64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _rsa_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _rsa_public_jwk(private_key, kid: str) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": ASYMMETRIC_SIGNATURE_ALGORITHM,
        "use": "sig",
        "n": _base64url_uint(numbers.n),
        "e": _base64url_uint(numbers.e),
    }


def _sign_with_rsa(card: dict[str, object], private_key, *, kid: str, jku: str | None = None) -> dict[str, object]:
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    protected_header: ProtectedHeader = {
        "alg": ASYMMETRIC_SIGNATURE_ALGORITHM,
        "typ": "JOSE",
        "kid": kid,
        "jku": jku,
    }
    signer = create_agent_card_signer(signing_key=private_pem, protected_header=protected_header)
    return _agent_card_to_dict(signer(_agent_card_from_dict(card)))


def test_canonicalize_agent_card_is_key_order_stable() -> None:
    left = {"name": "iac-code", "skills": [{"id": "iac"}], "version": "1"}
    right = {"version": "1", "skills": [{"id": "iac"}], "name": "iac-code"}

    assert canonicalize_agent_card(left) == canonicalize_agent_card(right)


def test_sign_and_verify_agent_card_dict() -> None:
    card = {"name": "iac-code", "version": "1"}

    signed = sign_agent_card_dict(card, secret=SECRET, key_id="local")
    result = verify_agent_card_dict(signed, secret=SECRET)

    assert result.valid is True
    assert result.reason == "valid"
    assert signed["signatures"][0]["protected"]
    assert "header" not in signed["signatures"][0]


def test_signature_uses_jws_protected_header() -> None:
    signed = sign_agent_card_dict({"name": "iac-code", "version": "1"}, secret=SECRET, key_id="local")

    signature = signed["signatures"][0]
    protected = signature["protected"]
    protected_header = json.loads(_base64url_decode(protected).decode("utf-8"))

    assert protected
    assert protected_header["alg"] == SIGNATURE_ALGORITHM
    assert protected_header["typ"] == "JOSE"
    assert protected_header["kid"] == "local"
    assert "alg" not in signature.get("header", {})


def test_verify_rejects_tampered_protected_header() -> None:
    signed = sign_agent_card_dict({"name": "iac-code", "version": "1"}, secret=SECRET, key_id="local")
    signed["signatures"][0]["protected"] = ""

    result = verify_agent_card_dict(signed, secret=SECRET)

    assert result.valid is False
    assert result.reason == "malformed-signature"


def test_verify_unsigned_card_is_allowed_by_default() -> None:
    result = verify_agent_card_dict({"name": "unsigned"}, secret=SECRET)

    assert result.valid is True
    assert result.reason == "unsigned"


def test_verify_unsigned_card_can_be_strict() -> None:
    result = verify_agent_card_dict({"name": "unsigned"}, secret=SECRET, require_signature=True)

    assert result.valid is False
    assert result.reason == "missing-signature"


def test_verify_rejects_mismatched_signature() -> None:
    signed = sign_agent_card_dict({"name": "iac-code"}, secret=SECRET, key_id="local")

    result = verify_agent_card_dict(signed, secret=OTHER_SECRET)

    assert result.valid is False
    assert result.reason == "signature-mismatch"


def test_verify_selects_secret_by_key_id() -> None:
    signed = sign_agent_card_dict({"name": "iac-code"}, secret=SECRET, key_id="local")

    result = verify_agent_card_dict(
        signed,
        secrets={"old": OTHER_SECRET, "local": SECRET},
        require_signature=True,
    )

    assert result.valid is True
    assert result.key_id == "local"


def test_verify_rejects_unknown_key_id() -> None:
    signed = sign_agent_card_dict({"name": "iac-code"}, secret=SECRET, key_id="local")

    result = verify_agent_card_dict(signed, secrets={"other": OTHER_SECRET}, require_signature=True)

    assert result.valid is False
    assert result.reason == "unknown-key"
    assert result.key_id == "local"
    assert result.message == "unknown-key: kid=local"


def test_verify_reports_unsupported_algorithm_detail() -> None:
    signed = sign_agent_card_dict({"name": "iac-code"}, secret=SECRET, key_id="local")
    protected = json.loads(_base64url_decode(signed["signatures"][0]["protected"]).decode("utf-8"))
    protected["alg"] = "ES256"
    signed["signatures"][0]["protected"] = base64.urlsafe_b64encode(json.dumps(protected).encode()).decode().rstrip("=")

    result = verify_agent_card_dict(signed, secret=SECRET, require_signature=True)

    assert result.valid is False
    assert result.reason == "unsupported-algorithm"
    assert result.message == "unsupported-algorithm: alg=ES256"


def test_verify_uses_oct_jwks_key() -> None:
    signed = sign_agent_card_dict({"name": "iac-code"}, secret=SECRET, key_id="local")
    jwks = {
        "keys": [
            {
                "kty": "oct",
                "kid": "local",
                "k": base64.urlsafe_b64encode(SECRET.encode()).decode().rstrip("="),
            }
        ]
    }

    result = verify_agent_card_dict(signed, jwks=jwks, require_signature=True)

    assert result.valid is True
    assert result.key_id == "local"


def test_verify_uses_rsa_jwks_key() -> None:
    private_key = _rsa_private_key()
    signed = _sign_with_rsa({"name": "iac-code"}, private_key, kid="rsa-current")
    jwks = {"keys": [_rsa_public_jwk(private_key, "rsa-current")]}

    result = verify_agent_card_dict(signed, jwks=jwks, require_signature=True)

    assert result.valid is True
    assert result.key_id == "rsa-current"


def test_verify_selects_rotated_rsa_jwks_key_by_key_id() -> None:
    old_private_key = _rsa_private_key()
    current_private_key = _rsa_private_key()
    signed = _sign_with_rsa({"name": "iac-code"}, current_private_key, kid="rsa-current")
    jwks = {
        "keys": [
            _rsa_public_jwk(old_private_key, "rsa-old"),
            _rsa_public_jwk(current_private_key, "rsa-current"),
        ]
    }

    result = verify_agent_card_dict(signed, jwks=jwks, require_signature=True)

    assert result.valid is True
    assert result.key_id == "rsa-current"


def test_verify_rejects_ambiguous_unsigned_key_id_with_multiple_keys() -> None:
    signed = sign_agent_card_dict({"name": "iac-code"}, secret=SECRET, key_id="local")
    unsigned_kid = deepcopy(signed)
    protected = json.loads(_base64url_decode(unsigned_kid["signatures"][0]["protected"]).decode("utf-8"))
    protected.pop("kid")
    unsigned_kid["signatures"][0]["protected"] = (
        base64.urlsafe_b64encode(json.dumps(protected).encode()).decode().rstrip("=")
    )

    result = verify_agent_card_dict(unsigned_kid, secrets={"one": SECRET, "two": OTHER_SECRET}, require_signature=True)

    assert result.valid is False
    assert result.reason == "ambiguous-key"


def test_verify_does_not_mask_unexpected_verifier_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    signed = sign_agent_card_dict({"name": "iac-code"}, secret=SECRET, key_id="local")

    def explode(card):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("iac_code.a2a.signing._agent_card_from_dict", explode)

    with pytest.raises(RuntimeError, match="unexpected"):
        verify_agent_card_dict(signed, secret=SECRET)
