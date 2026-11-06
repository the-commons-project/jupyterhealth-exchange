"""Secrets at rest: Fernet over the EHR token, the PKCE verifier and the spool key.

A list of keys so they can rotate: the first encrypts, any decrypts (MultiFernet). To rotate,
prepend a new key, keep the old one until in-flight attempts and jobs have drained, then drop it.
Settled jobs keep nothing encrypted.
"""

from __future__ import annotations

from collections.abc import Sequence

from cryptography.fernet import Fernet, MultiFernet
from pydantic import SecretStr

from .conf import ConfigError


class Crypto:
    def __init__(self, keys: Sequence[SecretStr]) -> None:
        if not keys:
            raise ConfigError("PATIENT_ACCESS: ENCRYPTION_KEYS is empty")
        try:
            self._fernet = MultiFernet([Fernet(k.get_secret_value().encode()) for k in keys])
        except (TypeError, ValueError) as e:
            raise ConfigError(f"PATIENT_ACCESS: ENCRYPTION_KEYS: {e}") from None

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
