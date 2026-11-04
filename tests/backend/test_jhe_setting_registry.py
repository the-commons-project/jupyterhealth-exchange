"""
The JheSetting registry: a key an app registers (core.services.jhe_settings.register_setting)
is validated on both edit paths, the REST API and the Django admin form; unregistered keys are
unchanged. Saving a row drops its get_setting() cache entry.
"""

import json

import pytest
from django.contrib.admin.sites import site
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from rest_framework.test import APIClient

from core.admin import JheSettingAdmin
from core.jhe_settings_specs import CORE_SETTINGS
from core.models import JheSetting
from core.services.jhe_settings import (
    SettingSpec,
    SettingValueType,
    get_setting,
    get_spec,
    register_setting,
    registered_settings,
    unregister_setting,
)


@pytest.fixture
def superuser_client(superuser):
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(superuser)
    return client


@pytest.fixture
def saved(db):
    def _saved(key: str, value_type: str, value) -> JheSetting:
        setting = JheSetting(key=key, value_type=value_type)
        setting.set_value(value_type, value)
        setting.save()
        return setting

    return _saved


@pytest.fixture
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def _even(value: object) -> None:
    if not isinstance(value, int) or value % 2:
        raise ValueError("must be even")


@pytest.fixture
def even_key():
    spec = register_setting(
        SettingSpec(
            key="test.even", value_type=SettingValueType.INT, help="An even number.", example="2", validate=_even
        )
    )
    yield spec
    unregister_setting(spec.key)


# ---------------------------------------------------------------- registry


def test_value_types_mirror_the_model():
    assert {t.value for t in SettingValueType} == set(JheSetting.JHE_SETTING_VALUE_TYPES)


def test_core_keys_are_registered_at_startup():
    assert {spec.key for spec in CORE_SETTINGS} <= set(registered_settings())


def test_registering_the_same_spec_twice_is_a_noop(even_key):
    assert register_setting(even_key) is even_key
    assert get_spec("test.even") == even_key


def test_registering_a_different_spec_for_a_key_is_an_error(even_key):
    changed = SettingSpec(key="test.even", value_type=SettingValueType.STRING, help="x", example="x")
    with pytest.raises(ValueError, match="already registered"):
        register_setting(changed)


@pytest.mark.parametrize("spec", CORE_SETTINGS, ids=lambda s: s.key)
def test_every_registered_example_validates(spec):
    setting = JheSetting(key=spec.key, value_type=spec.value_type)
    example = json.loads(spec.example) if spec.value_type == SettingValueType.JSON else spec.example
    setting.set_value(spec.value_type, example)
    setting.clean()  # raises if the example is not acceptable


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not a url", "absolute http or https URL"),
        ("ftp://jhe.example.org", "absolute http or https URL"),
        ("https://jhe.example.org/", "must not end with a slash"),
    ],
)
def test_site_url_rejects(value, message):
    setting = JheSetting(key="site.url", value_type="string")
    setting.set_value("string", value)
    with pytest.raises(ValidationError) as excinfo:
        setting.clean()
    assert message in excinfo.value.message_dict["value_string"][0]


@pytest.mark.parametrize("value", ["https://jhe.example.org", "http://localhost:8000", "https://jhe.example.org:8443"])
def test_site_url_accepts(value):
    setting = JheSetting(key="site.url", value_type="string")
    setting.set_value("string", value)
    setting.clean()


# ---------------------------------------------------------------- REST API


def test_api_rejects_a_bad_value_for_a_registered_key(superuser_client, saved):
    setting = saved("auth.patient.invitation_expiration_days", "int", 7)
    r = superuser_client.patch(f"/api/v1/jhe_settings/{setting.id}", {"value": 0})
    assert r.status_code == 400, r.text
    assert "1 or more" in r.json()["value"][0]
    setting.refresh_from_db()
    assert setting.value_int == 7


def test_api_rejects_the_wrong_type_for_a_registered_key(superuser_client, saved):
    setting = saved("auth.patient.invitation_expiration_days", "int", 7)
    r = superuser_client.patch(f"/api/v1/jhe_settings/{setting.id}", {"valueType": "string", "value": "7"})
    assert r.status_code == 400, r.text
    assert "is a int setting" in r.json()["valueType"][0]  # the API renders camelCase


def test_api_accepts_a_good_value_for_a_registered_key(superuser_client, saved):
    setting = saved("site.url", "string", "http://localhost:8000")
    r = superuser_client.patch(f"/api/v1/jhe_settings/{setting.id}", {"value": "https://jhe.example.org"})
    assert r.status_code == 200, r.text
    assert r.json()["resolvedValue"] == "https://jhe.example.org"


def test_api_create_validates_a_registered_key(superuser_client, db):
    r = superuser_client.post("/api/v1/jhe_settings", {"key": "site.url", "valueType": "string", "value": "nope"})
    assert r.status_code == 400, r.text
    assert "value" in r.json()
    assert not JheSetting.objects.filter(key="site.url").exists()


def test_api_leaves_unregistered_keys_alone(superuser_client, saved):
    setting = saved("free.form", "string", "anything")
    r = superuser_client.patch(f"/api/v1/jhe_settings/{setting.id}", {"value": "still anything"})
    assert r.status_code == 200, r.text
    assert r.json()["help"] is None and r.json()["example"] is None


def test_api_uncoercible_value_is_a_400_not_a_500(superuser_client, saved):
    # set_value used to raise jsonschema's ValidationError, which DRF does not translate.
    setting = saved("free.form.int", "int", 1)
    r = superuser_client.patch(f"/api/v1/jhe_settings/{setting.id}", {"value": "seven"})
    assert r.status_code == 400, r.text
    assert r.json()["value"] == ["Invalid int"]


def test_api_duplicate_key_is_a_400(superuser_client, saved):
    saved("free.form", "string", "a")
    r = superuser_client.post("/api/v1/jhe_settings", {"key": "free.form", "valueType": "string", "value": "b"})
    assert r.status_code == 400, r.text
    assert "key" in r.json()


def test_api_exposes_help_and_example_for_registered_keys(superuser_client, saved):
    setting = saved("site.url", "string", "http://localhost:8000")
    r = superuser_client.get(f"/api/v1/jhe_settings/{setting.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["help"] == get_spec("site.url").help
    assert body["example"] == "https://jhe.example.org"


# ---------------------------------------------------------------- Django admin form


def _admin_form(setting: JheSetting, superuser, **overrides):
    request = RequestFactory().get("/admin/core/jhesetting/")
    request.user = superuser
    form_class = JheSettingAdmin(JheSetting, site).get_form(request, setting)
    data = {
        "key": setting.key,
        "value_type": setting.value_type,
        "value_string": setting.value_string or "",
        "value_int": "" if setting.value_int is None else setting.value_int,
        "value_float": "" if setting.value_float is None else setting.value_float,
        "value_json": "" if setting.value_json is None else json.dumps(setting.value_json),
    }
    if setting.value_bool:
        data["value_bool"] = "on"
    data.update(overrides)
    return form_class(data=data, instance=setting)


def test_admin_form_rejects_a_bad_value_for_a_registered_key(saved, superuser):
    setting = saved("auth.patient.invitation_expiration_days", "int", 7)
    form = _admin_form(setting, superuser, value_int=0)
    assert not form.is_valid()
    assert "1 or more" in form.errors["value_int"][0]


def test_admin_form_rejects_a_missing_value_for_a_registered_key(saved, superuser):
    setting = saved("auth.patient.invitation_expiration_days", "int", 7)
    form = _admin_form(setting, superuser, value_int="")
    assert not form.is_valid()
    assert "needs a value" in form.errors["value_int"][0]


def test_admin_form_accepts_a_good_value_and_unregistered_keys(saved, superuser):
    assert _admin_form(saved("auth.patient.invitation_expiration_days", "int", 7), superuser, value_int=30).is_valid()
    assert _admin_form(saved("free.form", "string", "x"), superuser, value_string="anything").is_valid()


def test_admin_form_uses_a_custom_validator(saved, superuser, even_key):
    setting = saved("test.even", "int", 2)
    assert not _admin_form(setting, superuser, value_int=3).is_valid()
    assert _admin_form(setting, superuser, value_int=4).is_valid()


# ---------------------------------------------------------------- cache invalidation


def test_save_drops_the_cached_value(saved, clear_cache):
    setting = saved("free.form", "string", "before")
    assert get_setting("free.form") == "before"
    setting.set_value("string", "after")
    setting.save()
    assert get_setting("free.form") == "after"


def test_delete_drops_the_cached_value(saved, clear_cache):
    setting = saved("free.form", "string", "before")
    assert get_setting("free.form") == "before"
    setting.delete()
    assert get_setting("free.form", "default") == "default"
