"""Client authentication at the token endpoint: what a registration may use, and the two plugs
that hold the material.

A registration lists `AuthMethod`s in preference order; each names what it needs by reference (a
`kid` in the deployment's `SigningKeys`, a secret name in its `SecretStore`). Rows record only the
method and kid used; the material never leaves the plugs, and `sign` returns a token so a KMS
implementation never exports the key.

The partner build registers public clients only and wires `PUBLIC_ONLY`. The confidential
methods and the plugs are kept so adding a key later is configuration, not code (full-build
backlog row 5).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol, Self

from pydantic import model_validator

from .models import JsonObject, Value


class AuthMethodName(StrEnum):
    PUBLIC = "public"
    CLIENT_SECRET_BASIC = "client_secret_basic"
    CLIENT_SECRET_POST = "client_secret_post"
    PRIVATE_KEY_JWT = "private_key_jwt"


class CredentialError(Exception):
    """A method names material the deployment does not have. Safe to log, not to show patients."""


class SigningKeys(Protocol):
    def kids(self) -> frozenset[str]: ...

    def public_jwks(self) -> JsonObject:
        """{"keys": [...]}: the public halves, for the host to serve at its JWK Set URL."""
        ...

    def sign(self, kid: str, claims: Mapping[str, str | int], alg: str) -> str:
        """A compact JWT over `claims`. Raises CredentialError for an unknown kid."""
        ...


class SecretStore(Protocol):
    def has(self, name: str) -> bool: ...

    def get(self, name: str) -> str:
        """Raises CredentialError when missing."""
        ...


class AuthMethod(Value):
    method: AuthMethodName
    kid: str | None = None  # private_key_jwt
    alg: str = "RS384"
    secret: str | None = None  # client_secret_*: a SecretStore name

    @model_validator(mode="after")
    def _needs(self) -> Self:
        if self.method is AuthMethodName.PRIVATE_KEY_JWT and not self.kid:
            raise ValueError("private_key_jwt needs a kid")
        if self.method in (AuthMethodName.CLIENT_SECRET_BASIC, AuthMethodName.CLIENT_SECRET_POST) and not self.secret:
            raise ValueError(f"{self.method} needs a secret name")
        return self

    def usable(self, keys: SigningKeys, secrets: SecretStore) -> bool:
        match self.method:
            case AuthMethodName.PRIVATE_KEY_JWT:
                return self.kid in keys.kids()
            case AuthMethodName.CLIENT_SECRET_BASIC | AuthMethodName.CLIENT_SECRET_POST:
                return secrets.has(self.secret or "")
            case AuthMethodName.PUBLIC:
                return True


PUBLIC = AuthMethod(method=AuthMethodName.PUBLIC)


class Keyring:
    """The deployment's signing keys and secrets, passed together to the flow."""

    def __init__(self, keys: SigningKeys, secrets: SecretStore) -> None:
        self.keys = keys
        self.secrets = secrets

    def choose(self, methods: tuple[AuthMethod, ...]) -> AuthMethod:
        """The first method this deployment can satisfy."""
        for m in methods:
            if m.usable(self.keys, self.secrets):
                return m
        raise CredentialError(f"none of {[str(m.method) for m in methods]} is usable with this deployment's keys")


class NoSigningKeys:
    def kids(self) -> frozenset[str]:
        return frozenset()

    def public_jwks(self) -> JsonObject:
        return {"keys": []}

    def sign(self, kid: str, claims: Mapping[str, str | int], alg: str) -> str:
        raise CredentialError(f"no signing key {kid!r}: this deployment has none")


class NoSecrets:
    def has(self, name: str) -> bool:
        return False

    def get(self, name: str) -> str:
        raise CredentialError(f"no secret {name!r}: this deployment has none")


PUBLIC_ONLY = Keyring(NoSigningKeys(), NoSecrets())
