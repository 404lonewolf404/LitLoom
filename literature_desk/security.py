"""Small, dependency-free security primitives for LitLoom team authentication."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets


PASSWORD_ITERATIONS = 310_000
PASSWORD_MIN_LENGTH = 12
SESSION_TOKEN_BYTES = 32
BOOTSTRAP_TOKEN_BYTES = 32
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,39}$")


def validate_username(value: str) -> str:
    username = str(value or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("用户名需为 3-40 位字母、数字、点、下划线或连字符")
    return username


def validate_password(value: str) -> str:
    password = str(value or "")
    if len(password) < PASSWORD_MIN_LENGTH or len(password) > 256:
        raise ValueError("密码长度必须为 12-256 个字符")
    if not any(char.isalpha() for char in password) or not any(char.isdigit() for char in password):
        raise ValueError("密码必须同时包含字母和数字")
    return password


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    password = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, expected = str(encoded or "").split("$", 3)
        iterations = int(raw_iterations)
        salt = bytes.fromhex(raw_salt)
        expected_bytes = bytes.fromhex(expected)
    except (TypeError, ValueError):
        return False
    if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000 or len(salt) < 16:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected_bytes)


def new_token(size: int = SESSION_TOKEN_BYTES) -> str:
    return secrets.token_urlsafe(size)


def token_digest(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def constant_time_token_match(candidate: str, expected: str) -> bool:
    return bool(candidate and expected and hmac.compare_digest(str(candidate), str(expected)))
