from __future__ import annotations

import base64
import binascii
import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from a2a.types import AgentCard
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

SIGNATURE_ALGORITHM = "HS256"
ASYMMETRIC_SIGNATURE_ALGORITHM = "RS256"
SUPPORTED_SIGNATURE_ALGORITHMS = (SIGNATURE_ALGORITHM, ASYMMETRIC_SIGNATURE_ALGORITHM)


@dataclass(frozen=True)
class AgentCardSignature:
    valid: bool
    reason: str
    key_id: str | None = None
    detail: str = ""

    @property
    def message(self) -> str:
        if self.detail:
            return f"{self.reason}: {self.detail}"
        if self.key_id and self.reason in {"unknown-key", "signature-mismatch"}:
            return f"{self.reason}: kid={self.key_id}"
        return self.reason


def _without_signature(card: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(card)
    data.pop("signatures", None)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("iac_code_signature", None)
        if not metadata:
            data.pop("metadata", None)
    return data


def canonicalize_agent_card(card: dict[str, Any]) -> bytes:
    data = _without_signature(card)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_agent_card_dict(card: dict[str, Any], *, secret: str, key_id: str = "default") -> dict[str, Any]:
    from a2a.utils.signing import ProtectedHeader, create_agent_card_signer

    agent_card = _agent_card_from_dict(card)
    protected_header: ProtectedHeader = {"alg": SIGNATURE_ALGORITHM, "typ": "JOSE", "kid": key_id, "jku": None}
    signer = create_agent_card_signer(
        signing_key=secret,
        protected_header=protected_header,
    )
    signed = signer(agent_card)
    return _agent_card_to_dict(signed)


def verify_agent_card_dict(
    card: dict[str, Any],
    *,
    secret: str | None = None,
    secrets: Mapping[str, str] | None = None,
    jwks: Mapping[str, Any] | None = None,
    require_signature: bool = False,
) -> AgentCardSignature:
    signatures = card.get("signatures")
    signature_data = signatures[0] if isinstance(signatures, list) and signatures else None
    if not isinstance(signature_data, dict):
        reason = "missing-signature" if require_signature else "unsigned"
        return AgentCardSignature(valid=not require_signature, reason=reason)

    protected_header = _decode_protected_header(signature_data.get("protected"))
    if protected_header is None:
        return AgentCardSignature(valid=False, reason="malformed-signature")
    algorithm = protected_header.get("alg")
    if algorithm not in SUPPORTED_SIGNATURE_ALGORITHMS:
        detail = f"alg={algorithm}" if isinstance(algorithm, str) else "alg=<missing>"
        return AgentCardSignature(valid=False, reason="unsupported-algorithm", detail=detail)
    if not isinstance(signature_data.get("signature"), str):
        return AgentCardSignature(valid=False, reason="malformed-signature")

    raw_key_id = protected_header.get("kid")
    key_id = raw_key_id if isinstance(raw_key_id, str) else None
    verification_key = _select_verification_key(
        secret=secret,
        secrets=secrets,
        jwks=jwks,
        key_id=key_id,
        algorithm=algorithm,
    )
    if isinstance(verification_key, AgentCardSignature):
        return verification_key

    from a2a.utils.signing import InvalidSignaturesError, create_signature_verifier

    verifier = create_signature_verifier(
        key_provider=lambda _kid, _jku: verification_key,
        algorithms=[algorithm],
    )
    try:
        verifier(_agent_card_from_dict(card))
    except InvalidSignaturesError:
        return AgentCardSignature(valid=False, reason="signature-mismatch", key_id=key_id)
    except (ParseError, TypeError, ValueError):
        return AgentCardSignature(valid=False, reason="malformed-signature", key_id=key_id)
    return AgentCardSignature(valid=True, reason="valid", key_id=key_id)


def agent_card_signature_jwks_url(card: dict[str, Any]) -> str | None:
    signatures = card.get("signatures")
    signature_data = signatures[0] if isinstance(signatures, list) and signatures else None
    if not isinstance(signature_data, dict):
        return None
    protected_header = _decode_protected_header(signature_data.get("protected"))
    if protected_header is None:
        return None
    jku = protected_header.get("jku")
    return jku if isinstance(jku, str) and jku else None


def _select_verification_key(
    *,
    secret: str | None,
    secrets: Mapping[str, str] | None,
    jwks: Mapping[str, Any] | None,
    key_id: str | None,
    algorithm: str,
) -> Any | AgentCardSignature:
    key_map: dict[str, Any] = {}
    if secrets:
        key_map.update({str(kid): value for kid, value in secrets.items()})
    key_map.update(_jwks_verification_keys(jwks, algorithm=algorithm))

    if not key_map:
        if secret is None:
            return AgentCardSignature(valid=False, reason="missing-key", key_id=key_id)
        return secret

    if key_id:
        selected = key_map.get(key_id)
        if selected is None:
            return AgentCardSignature(valid=False, reason="unknown-key", key_id=key_id)
        return selected

    if len(key_map) == 1:
        return next(iter(key_map.values()))
    return AgentCardSignature(valid=False, reason="ambiguous-key", key_id=key_id)


def _jwks_verification_keys(jwks: Mapping[str, Any] | None, *, algorithm: str) -> dict[str, Any]:
    if not jwks:
        return {}
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        return {}

    decoded: dict[str, Any] = {}
    for item in keys:
        if not isinstance(item, Mapping):
            continue
        kid = item.get("kid")
        if not isinstance(kid, str):
            continue
        jwk_alg = item.get("alg")
        if isinstance(jwk_alg, str) and jwk_alg != algorithm:
            continue
        if item.get("kty") == "oct":
            if algorithm != SIGNATURE_ALGORITHM:
                continue
            key = item.get("k")
            if not isinstance(key, str):
                continue
            try:
                padding = "=" * (-len(key) % 4)
                decoded[kid] = base64.urlsafe_b64decode(f"{key}{padding}".encode("ascii")).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            continue
        if algorithm == SIGNATURE_ALGORITHM:
            continue
        try:
            from jwt import PyJWK
            from jwt.exceptions import PyJWKError

            decoded[kid] = PyJWK.from_dict(dict(item))
        except (PyJWKError, TypeError, ValueError):
            continue
    return decoded


def _agent_card_from_dict(card: dict[str, Any]) -> AgentCard:
    agent_card = AgentCard()
    ParseDict(card, agent_card, ignore_unknown_fields=True)
    return agent_card


def _agent_card_to_dict(card: AgentCard) -> dict[str, Any]:
    data = MessageToDict(card, preserving_proto_field_name=False)
    if not isinstance(data, dict):
        raise ValueError("A2A Agent Card must serialize to a JSON object")
    return data


def _decode_protected_header(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from jwt.utils import base64url_decode

        header = json.loads(base64url_decode(value.encode("utf-8")).decode("utf-8"))
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return header if isinstance(header, dict) else None
