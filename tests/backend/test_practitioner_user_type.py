"""A practitioner profile created without a user_type (Django admin) labels its user."""

import pytest

from core.models import JheUser, Practitioner


@pytest.mark.django_db
def test_profile_created_directly_sets_empty_user_type():
    user = JheUser.objects.create(email="svc@example.com")
    assert not user.user_type
    Practitioner.objects.create(jhe_user=user)
    user.refresh_from_db()
    assert user.user_type == "practitioner"


@pytest.mark.django_db
def test_existing_user_type_is_not_overwritten():
    user = JheUser.objects.create(email="odd@example.com", user_type="patient")
    Practitioner.objects.create(jhe_user=user)
    user.refresh_from_db()
    assert user.user_type == "patient"
