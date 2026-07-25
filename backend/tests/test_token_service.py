"""JWT creation/decoding: separate secrets, expiry, claims, tampering."""

from datetime import timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import create_token, decode_token


def test_create_and_decode_access_token_round_trip() -> None:
    token, jti = create_token("user-123", "access", extra_claims={"tenant_id": "tenant-456"})
    payload = decode_token(token, expected_type="access")

    assert payload["sub"] == "user-123"
    assert payload["type"] == "access"
    assert payload["tenant_id"] == "tenant-456"
    assert payload["jti"] == jti


def test_create_and_decode_refresh_token_round_trip() -> None:
    token, jti = create_token("user-123", "refresh")
    payload = decode_token(token, expected_type="refresh")
    assert payload["type"] == "refresh"
    assert payload["jti"] == jti


def test_access_and_refresh_tokens_use_different_secrets() -> None:
    settings = get_settings()
    assert settings.jwt_secret_key != settings.jwt_refresh_secret_key


def test_refresh_token_cannot_be_used_as_access_token() -> None:
    refresh_token, _ = create_token("user-123", "refresh")
    with pytest.raises(jwt.PyJWTError):
        decode_token(refresh_token, expected_type="access")


def test_access_token_cannot_be_used_as_refresh_token() -> None:
    access_token, _ = create_token("user-123", "access")
    with pytest.raises(jwt.PyJWTError):
        decode_token(access_token, expected_type="refresh")


def test_expired_token_is_rejected() -> None:
    token, _ = create_token("user-123", "access", expires_delta=timedelta(seconds=-1))
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(token, expected_type="access")


def test_tampered_token_is_rejected() -> None:
    token, _ = create_token("user-123", "access")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(jwt.PyJWTError):
        decode_token(tampered, expected_type="access")


def test_wrong_issuer_is_rejected() -> None:
    settings = get_settings()
    bad_issuer_token = jwt.encode(
        {"sub": "user-123", "type": "access", "iss": "not-sentinelx", "exp": 9999999999},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.InvalidIssuerError):
        decode_token(bad_issuer_token, expected_type="access")


def test_each_token_gets_a_unique_jti() -> None:
    _, jti_a = create_token("user-123", "access")
    _, jti_b = create_token("user-123", "access")
    assert jti_a != jti_b


def test_kid_header_is_distinct_per_token_type() -> None:
    settings = get_settings()
    access_token, _ = create_token("user-123", "access")
    refresh_token, _ = create_token("user-123", "refresh")

    access_header = jwt.get_unverified_header(access_token)
    refresh_header = jwt.get_unverified_header(refresh_token)

    assert access_header["kid"] == settings.jwt_access_key_id
    assert refresh_header["kid"] == settings.jwt_refresh_key_id
    assert access_header["kid"] != refresh_header["kid"]


def test_extra_claims_are_embedded() -> None:
    token, _ = create_token(
        "user-123", "access", extra_claims={"tenant_id": "t-1", "role": "owner", "ver": 3}
    )
    payload = decode_token(token, expected_type="access")
    assert payload["tenant_id"] == "t-1"
    assert payload["role"] == "owner"
    assert payload["ver"] == 3


def test_default_access_token_expiry_matches_settings() -> None:
    settings = get_settings()
    token, _ = create_token("user-123", "access")
    payload = decode_token(token, expected_type="access")
    lifetime_seconds = payload["exp"] - payload["iat"]
    assert lifetime_seconds == settings.jwt_access_token_expire_minutes * 60


def test_default_refresh_token_expiry_matches_settings() -> None:
    settings = get_settings()
    token, _ = create_token("user-123", "refresh")
    payload = decode_token(token, expected_type="refresh")
    lifetime_seconds = payload["exp"] - payload["iat"]
    assert lifetime_seconds == settings.jwt_refresh_token_expire_days * 86400
