"""Convert existing step-count Observations to the canonical OMH unit_value shape.

Background: PR #9 (OW polling pipeline) replaced JHE's non-canonical
``schema-omh_step-count_3-0.json`` with the upstream OMH version. The old
schema accepted ``body.step_count`` as a plain integer; the canonical schema
requires ``body.step_count`` to be a ``{value, unit}`` dict with
``unit == "steps"``.

This migration walks every existing ``omh:step-count:3.0`` Observation and
rewrites the JSONB body in place::

    {"body": {"step_count": 20000, ...}}
        ->
    {"body": {"step_count": {"value": 20000, "unit": "steps"}, ...}}

Records that already have the unit_value shape (e.g. seeded by the polling
pipeline after the schema swap) are left untouched. Reverse migration
restores the plain-integer form.
"""

from django.db import migrations


def _migrate_one(body: dict, *, to_canonical: bool) -> bool:
    """Mutates ``body`` in place. Returns True if it was changed."""
    sc = body.get("step_count")
    if to_canonical:
        if isinstance(sc, dict):
            return False  # already canonical
        if isinstance(sc, (int, float)):
            body["step_count"] = {"value": int(sc), "unit": "steps"}
            return True
    else:  # back to plain integer
        if isinstance(sc, int):
            return False  # already plain
        if isinstance(sc, dict) and "value" in sc:
            body["step_count"] = int(sc["value"])
            return True
    return False


def _walk_observations(apps, *, to_canonical: bool):
    Observation = apps.get_model("core", "Observation")
    qs = Observation.objects.filter(codeable_concept__coding_code="omh:step-count:3.0")
    converted = 0
    for obs in qs.iterator():
        data = obs.value_attachment_data
        if not isinstance(data, dict):
            continue
        body = data.get("body")
        if not isinstance(body, dict):
            continue
        if _migrate_one(body, to_canonical=to_canonical):
            obs.value_attachment_data = data
            # Bypass model.clean() validation here — the schema files on disk
            # may not yet be loaded the way the model expects during a migration
            # context. We're rewriting the JSONB blob directly with a known-good
            # transformation, so .save() with update_fields is sufficient.
            type(obs).objects.filter(pk=obs.pk).update(value_attachment_data=data)
            converted += 1
    return converted


def to_canonical(apps, schema_editor):
    n = _walk_observations(apps, to_canonical=True)
    print(f"  migration 0021: rewrote {n} step-count Observation(s) to canonical OMH shape")


def to_plain_integer(apps, schema_editor):
    n = _walk_observations(apps, to_canonical=False)
    print(f"  migration 0021 reverse: rewrote {n} step-count Observation(s) to plain integer")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_owpollevent_owpollstatus"),
    ]

    operations = [
        migrations.RunPython(to_canonical, to_plain_integer),
    ]
