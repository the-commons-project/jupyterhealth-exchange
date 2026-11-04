"""Runtime settings stored in ``JheSetting`` rows, plus the registry that describes them.

``get_setting`` is the read path every caller uses: one cache lookup, then the row. The registry
is optional metadata an app attaches to a key at startup (``register_setting`` from its
``AppConfig.ready``): the type the key must have, a validator for its value, and the help text
and example the settings page shows. ``JheSetting.clean`` consults it, so a bad value is refused
on both edit paths (the REST API and the Django admin). Keys nobody registered keep today's
behaviour: any type, any value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from django.core.cache import cache
from django.core.exceptions import ValidationError

DEFAULT_CACHE_TTL = 60  # seconds


class SettingValueType(StrEnum):
    """The typed columns a ``JheSetting`` row can hold; mirrors ``JheSetting.JHE_SETTING_VALUE_TYPES``."""

    STRING = "string"
    INT = "int"
    BOOL = "bool"
    FLOAT = "float"
    JSON = "json"

    @property
    def column(self) -> str:
        """The model column that stores a value of this type."""
        return f"value_{self.value}"


Validator = Callable[[Any], None]
"""Checks one value, already coerced to the key's type; raises ``ValueError`` with a message an operator can act on."""


@dataclass(frozen=True)
class SettingSpec:
    key: str
    value_type: SettingValueType
    help: str
    example: str
    validate: Validator | None = None


_registry: dict[str, SettingSpec] = {}


def register_setting(spec: SettingSpec) -> SettingSpec:
    """Attach a spec to a key. Registering the same spec twice is a no-op; a different spec for a
    registered key is a programming error, since two apps would be fighting over one key."""
    existing = _registry.get(spec.key)
    if existing is not None and existing != spec:
        raise ValueError(f"setting {spec.key!r} is already registered with a different spec")
    _registry[spec.key] = spec
    return spec


def unregister_setting(key: str) -> None:
    _registry.pop(key, None)


def get_spec(key: str) -> SettingSpec | None:
    return _registry.get(key)


def registered_settings() -> Mapping[str, SettingSpec]:
    return MappingProxyType(_registry)


def validate_setting(key: str, value_type: str, value: Any) -> None:
    """Raise Django's ``ValidationError`` if ``value`` is not acceptable for ``key``.

    Errors are keyed by model field (``value_type`` or the typed value column) so the Django admin's
    ModelForm can attach them; the REST serializer maps the column back to its ``value`` input.
    Unregistered keys always pass.
    """
    spec = _registry.get(key)
    if spec is None:
        return
    if value_type != spec.value_type:
        raise ValidationError({"value_type": f"{key} is a {spec.value_type} setting, not {value_type}"})
    if value is None:
        raise ValidationError({spec.value_type.column: f"{key} needs a value (example: {spec.example})"})
    if spec.validate is None:
        return
    try:
        spec.validate(value)
    except ValueError as e:
        raise ValidationError({spec.value_type.column: f"{key} {e} (example: {spec.example})"}) from e


def setting_cache_key(key: str) -> str:
    return f"jhe_setting:{key}"


def get_setting(key: str, default: Any = None) -> Any:
    cache_key = setting_cache_key(key)
    value = cache.get(cache_key)

    if value is not None:
        return value

    # Lazy import to avoid circular dependency with core.models
    from core.models import JheSetting

    try:
        setting = JheSetting.objects.get(key=key)
        value = setting.get_value()
    except JheSetting.DoesNotExist:
        value = default

    cache.set(cache_key, value, DEFAULT_CACHE_TTL)
    return value
