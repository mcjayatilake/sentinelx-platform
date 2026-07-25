"""Argon2id password hashing, rehash detection, and password policy."""

import pytest

from app.core.config import get_settings
from app.core.security import (
    NullBreachedPasswordChecker,
    PasswordPolicyError,
    hash_password,
    hash_secret,
    needs_rehash,
    validate_password_policy,
    verify_password,
)


def test_hash_password_uses_argon2id() -> None:
    hashed = hash_password("Correct-Horse-Battery-Staple-9!")
    assert hashed.startswith("$argon2id$")


def test_hash_password_round_trip() -> None:
    hashed = hash_password("Correct-Horse-Battery-Staple-9!")
    assert verify_password("Correct-Horse-Battery-Staple-9!", hashed)
    assert not verify_password("wrong-password", hashed)


def test_hash_password_is_salted() -> None:
    a = hash_password("Correct-Horse-Battery-Staple-9!")
    b = hash_password("Correct-Horse-Battery-Staple-9!")
    assert a != b


def test_needs_rehash_false_for_current_parameters() -> None:
    hashed = hash_password("Correct-Horse-Battery-Staple-9!")
    assert needs_rehash(hashed) is False


def test_needs_rehash_true_for_weaker_parameters() -> None:
    from passlib.context import CryptContext

    weak_context = CryptContext(
        schemes=["argon2"], argon2__time_cost=1, argon2__memory_cost=8, argon2__parallelism=1
    )
    weak_hash = weak_context.hash("Correct-Horse-Battery-Staple-9!")
    assert needs_rehash(weak_hash) is True


def test_validate_password_policy_accepts_compliant_password() -> None:
    validate_password_policy("Correct-Horse-Battery-Staple-9!")


@pytest.mark.parametrize(
    ("password", "expected_violation_substring"),
    [
        ("Sh0rt!", "at least"),
        ("nouppercase1!", "uppercase"),
        ("NOLOWERCASE1!", "lowercase"),
        ("NoDigitsHere!", "digit"),
        ("NoSymbolsHere1", "symbol"),
    ],
)
def test_validate_password_policy_rejects_violations(
    password: str, expected_violation_substring: str
) -> None:
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password_policy(password)
    assert any(expected_violation_substring in v for v in exc_info.value.violations)


def test_validate_password_policy_reports_every_violation_at_once() -> None:
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password_policy("short")
    # Too short, no uppercase, no digit, no symbol — all four at once.
    assert len(exc_info.value.violations) == 4


def test_password_policy_is_configurable() -> None:
    settings = get_settings().model_copy(
        update={
            "password_min_length": 4,
            "password_require_uppercase": False,
            "password_require_digit": False,
            "password_require_symbol": False,
        }
    )
    validate_password_policy("abcd", settings)  # would fail against the default policy


async def test_null_breached_password_checker_always_false() -> None:
    checker = NullBreachedPasswordChecker()
    assert await checker.is_breached("password123") is False


def test_hash_secret_is_deterministic_and_not_reversible() -> None:
    digest_a = hash_secret("some-high-entropy-secret")
    digest_b = hash_secret("some-high-entropy-secret")
    assert digest_a == digest_b
    assert digest_a != "some-high-entropy-secret"
    assert len(digest_a) == 64  # SHA-256 hex digest


def test_hash_secret_differs_for_different_input() -> None:
    assert hash_secret("secret-a") != hash_secret("secret-b")
