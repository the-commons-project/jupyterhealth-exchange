"""Generic, config-driven engine that renders a Django model instance into a FHIR
resource dict using the data_mapping declared in jhe/fhir_config.json.

A mapping is a nested structure of dicts, lists and strings. Strings are small
expressions:

  * A single-quoted string is a *literal*: ``"'final'"`` -> ``"final"``.
  * A string whose first dotted segment matches the resource type (e.g.
    ``"Patient.name_family"`` or ``"Patient.jhe_user.email"``) is a *path*: the
    remaining segments are traversed as attributes on the instance.
  * Operands joined with ``+`` are *concatenated* as strings, e.g.
    ``"'Patient/' + Observation.subject_patient"`` -> ``"Patient/42"``.
  * Any other bare string is treated as a literal (lenient fallback).

Resolution of a path's final value:

  * a Django related manager / queryset, or a list of model instances, *fans out*
    into a list -- each row is serialised via its ``as_fhir_element()`` method if it
    defines one, otherwise from its concrete, non-relational fields (e.g.
    ``Patient.identifiers`` -> ``[{"system": ..., "value": ...}, ...]``);
  * a single model instance becomes its primary key (for FHIR references);
  * anything else is returned as-is.

Dicts and lists are templates rendered recursively. Empty leaves (``None``, ``""``,
``[]``, ``{}``) are pruned. A template object that references DB paths but resolves
none of them is dropped entirely, so e.g. a telecom entry whose only value comes
from an empty DB column does not appear in the output.
"""

from django.db.models import Manager, Model, QuerySet

# Sentinel for a path that could not be resolved (missing attribute / None ancestor).
_MISSING = object()


def _is_empty(value):
    if value is _MISSING or value is None:
        return True
    if isinstance(value, (str, list, dict)) and len(value) == 0:
        return True
    return False


def _is_literal_token(token):
    token = token.strip()
    return len(token) >= 2 and token[0] == "'" and token[-1] == "'"


def _token_is_path(token, resource_type):
    token = token.strip()
    if _is_literal_token(token):
        return False
    return token.split(".", 1)[0] == resource_type


def _string_contains_path(expr, resource_type):
    return any(_token_is_path(token, resource_type) for token in expr.split("+"))


def _contains_path(node, resource_type):
    """True if the mapping node contains at least one path reference."""
    if isinstance(node, str):
        return _string_contains_path(node, resource_type)
    if isinstance(node, dict):
        return any(_contains_path(v, resource_type) for v in node.values())
    if isinstance(node, list):
        return any(_contains_path(v, resource_type) for v in node)
    return False


def _serialize_related_row(instance):
    """Serialise a related model instance to a FHIR sub-element.

    Models may define ``as_fhir_element()`` to control their FHIR shape (e.g.
    CodeableConcept -> ``{"system", "code", "display"}``). Otherwise the concrete,
    non-relational fields are emitted (primary key and relations skipped, empties
    pruned), which yields ``{"system", "value"}`` for the identifier models.
    """
    if hasattr(instance, "as_fhir_element"):
        element = instance.as_fhir_element()
        return {k: v for k, v in element.items() if not _is_empty(v)}
    row = {}
    for field in instance._meta.concrete_fields:
        if field.primary_key or field.is_relation:
            continue
        value = getattr(instance, field.attname)
        if not _is_empty(value):
            row[field.name] = value
    return row


def _fan_out(rows):
    return [row for row in (_serialize_related_row(r) for r in rows) if not _is_empty(row)]


def _materialize(value):
    if isinstance(value, Manager):
        value = value.all()
    if isinstance(value, QuerySet):
        return _fan_out(value)
    if isinstance(value, (list, tuple)) and value and all(isinstance(v, Model) for v in value):
        return _fan_out(value)
    if isinstance(value, Model):
        return value.pk
    return value


def _resolve_path(instance, path):
    # Drop the leading resource-type segment, then walk the remaining attributes.
    current = instance
    for part in path.split(".")[1:]:
        if current is None:
            return _MISSING
        current = getattr(current, part, _MISSING)
        if current is _MISSING:
            return _MISSING
    return _materialize(current)


def _eval_token(token, instance, resource_type):
    token = token.strip()
    if _is_literal_token(token):
        return token[1:-1]
    if token.split(".", 1)[0] == resource_type:
        return _resolve_path(instance, token)
    return token  # bare literal (lenient fallback)


def _eval_string(expr, instance, resource_type):
    tokens = expr.split("+")
    if len(tokens) == 1:
        return _eval_token(tokens[0], instance, resource_type)
    # Concatenation: every operand is stringified; a missing path voids the whole expr.
    parts = []
    for token in tokens:
        value = _eval_token(token, instance, resource_type)
        if _is_empty(value):
            return _MISSING
        parts.append(str(value))
    return "".join(parts)


def _render(node, instance, resource_type):
    if isinstance(node, str):
        return _eval_string(node, instance, resource_type)
    if isinstance(node, list):
        rendered = [_render(item, instance, resource_type) for item in node]
        return [item for item in rendered if not _is_empty(item)]
    if isinstance(node, dict):
        out = {}
        has_path = False
        has_resolved_path = False
        for key, child in node.items():
            child_is_path = _contains_path(child, resource_type)
            rendered = _render(child, instance, resource_type)
            if child_is_path:
                has_path = True
                if not _is_empty(rendered):
                    has_resolved_path = True
            if not _is_empty(rendered):
                out[key] = rendered
        # A template object that references DB paths but resolved none of them is
        # dropped, so empty-sourced entries (e.g. a telecom with no value) vanish.
        if has_path and not has_resolved_path:
            return _MISSING
        return out
    return node  # literal scalar (number, bool)


def render_resource(instance, resource_type, mapping):
    """Render the configured mapping for one instance into a dict of FHIR fields."""
    rendered = {}
    for key, node in mapping.items():
        value = _render(node, instance, resource_type)
        if not _is_empty(value):
            rendered[key] = value
    return rendered


def build_fhir_resource(instance, resource_type, mapping, aux_data=None):
    """Build a FHIR resource dict from a model instance.

    ``aux_data`` (when the model carries any) supplies the base; config-mapped Django
    fields are overlaid on top at the field level (Django values win). ``resourceType``
    is set and ``id`` is coerced to a string per the FHIR spec. The result is not
    validated here -- callers validate against the appropriate fhir.resources model.
    """
    rendered = render_resource(instance, resource_type, mapping)
    result = dict(aux_data or {})
    result.update(rendered)  # field-level replace: Django-mapped fields take precedence
    result["resourceType"] = resource_type
    # The resource id is always the Django primary key, never anything from aux_data.
    result["id"] = str(instance.pk)
    return result
