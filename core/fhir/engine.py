"""Generic, config-driven engine that renders a Django model instance into a FHIR
resource dict using the mapped_resources declared in core/fhir/fhir_config.json.

A mapping is a nested structure of dicts, lists and strings. Strings are small
expressions:

  * A single-quoted string is a *literal*: ``"'final'"`` -> ``"final"``.
  * A string whose first dotted segment matches the *source Django model* the resource is
    mapped to (e.g. ``"Patient.name_family"`` for Patient, or ``"DataSource.name"`` for a
    Device backed by DataSource) is a *path*: the remaining segments are traversed as
    attributes on the instance. The model name -- not the FHIR resourceType -- is what is
    matched, so a Device (resourceType ``Device``, model ``DataSource``) renders exactly
    like Patient and Observation, whose model name happens to equal their resourceType.
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


def _is_annotation(key):
    # Mapping keys starting with "__" are annotations (e.g. "__criteria"), not FHIR fields:
    # they are never rendered into output nor treated as columns on the way in.
    return isinstance(key, str) and key.startswith("__")


def _is_literal_token(token):
    token = token.strip()
    return len(token) >= 2 and token[0] == "'" and token[-1] == "'"


def _token_is_path(token, source_model):
    token = token.strip()
    if _is_literal_token(token):
        return False
    return token.split(".", 1)[0] == source_model


def _string_contains_path(expr, source_model):
    return any(_token_is_path(token, source_model) for token in expr.split("+"))


def _contains_path(node, source_model):
    """True if the mapping node contains at least one path reference."""
    if isinstance(node, str):
        return _string_contains_path(node, source_model)
    if isinstance(node, dict):
        return any(_contains_path(v, source_model) for k, v in node.items() if not _is_annotation(k))
    if isinstance(node, list):
        return any(_contains_path(v, source_model) for v in node)
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


def _eval_token(token, instance, source_model):
    token = token.strip()
    if _is_literal_token(token):
        return token[1:-1]
    if token.split(".", 1)[0] == source_model:
        return _resolve_path(instance, token)
    return token  # bare literal (lenient fallback)


def _eval_string(expr, instance, source_model):
    tokens = expr.split("+")
    if len(tokens) == 1:
        return _eval_token(tokens[0], instance, source_model)
    # Concatenation: every operand is stringified; a missing path voids the whole expr.
    parts = []
    for token in tokens:
        value = _eval_token(token, instance, source_model)
        if _is_empty(value):
            return _MISSING
        parts.append(str(value))
    return "".join(parts)


def _render(node, instance, source_model):
    if isinstance(node, str):
        return _eval_string(node, instance, source_model)
    if isinstance(node, list):
        rendered = [_render(item, instance, source_model) for item in node]
        return [item for item in rendered if not _is_empty(item)]
    if isinstance(node, dict):
        out = {}
        has_path = False
        has_resolved_path = False
        for key, child in node.items():
            if _is_annotation(key):
                continue
            child_is_path = _contains_path(child, source_model)
            rendered = _render(child, instance, source_model)
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


def render_resource(instance, source_model, mapping):
    """Render the configured mapping for one instance into a dict of FHIR fields.

    ``source_model`` is the Django model class name the mapping's paths are prefixed with
    (e.g. ``"DataSource"`` for a Device, ``"Patient"`` for a Patient); a token whose first
    dotted segment equals it is resolved against the instance, otherwise it is a literal.
    """
    rendered = {}
    for key, node in mapping.items():
        if _is_annotation(key):
            continue
        value = _render(node, instance, source_model)
        if not _is_empty(value):
            rendered[key] = value
    return rendered


def build_fhir_resource(instance, resource_type, mapping, aux_data=None):
    """Build a FHIR resource dict from a model instance.

    ``resource_type`` is the FHIR resourceType (e.g. ``"Device"``); it can differ from the
    Django model that backs it (``DataSource``). Path matching is driven off the *model*
    name (``instance._meta.object_name``), so ``"DataSource.name"`` resolves for a Device
    exactly as ``"Patient.name_family"`` does for a Patient. ``aux_data`` (when the model
    carries any) supplies the base; config-mapped Django fields are overlaid on top at the
    field level (Django values win). ``resourceType`` is set and ``id`` is coerced to a
    string per the FHIR spec. The result is not validated here -- callers validate against
    the appropriate fhir.resources model.
    """
    rendered = render_resource(instance, instance._meta.object_name, mapping)
    result = dict(aux_data or {})
    result.update(rendered)  # field-level replace: Django-mapped fields take precedence
    result["resourceType"] = resource_type
    # The resource id is always the Django primary key, never anything from aux_data.
    result["id"] = str(instance.pk)
    return result


# ---------------------------------------------------------------------------
# Reverse mapping: FHIR resource dict -> (model columns, leftover aux_fhir_data)
# ---------------------------------------------------------------------------
#
# The forward engine renders a model instance into FHIR using the config. The
# reverse direction, used on write, walks the same config alongside an incoming
# FHIR resource and pulls values back out for the directly-mapped model columns;
# everything the config does not claim at the top level is returned as the leftover
# ``aux_fhir_data`` blob. This keeps writes config-driven in the same way reads are.


def _reverse_walk(mapping_node, fhir_value, source_model, columns):
    """Walk a mapping node alongside the matching FHIR value, collecting columns.

    Only *directly assignable* columns are recovered: a leaf that is a single path of
    exactly two segments (``Model.field``). Multi-hop paths (``Patient.jhe_user.email``),
    concatenations and literals are not reversible and are skipped; multi-template lists
    (e.g. telecom's phone/email pair) are ambiguous and skipped too. The primary key is
    never assigned from the payload.
    """
    if fhir_value is None:
        return
    if isinstance(mapping_node, str):
        token = mapping_node.strip()
        if "+" in token or _is_literal_token(token):
            return  # concatenation / literal: not reversible
        parts = token.split(".")
        if parts[0] != source_model or len(parts) != 2:
            return  # wrong model, or a multi-hop path we cannot invert
        field = parts[1]
        if field == "id":
            return  # the pk is server-assigned, never taken from the payload
        columns[field] = fhir_value
    elif isinstance(mapping_node, dict):
        if not isinstance(fhir_value, dict):
            return
        for key, child in mapping_node.items():
            if not _is_annotation(key) and key in fhir_value:
                _reverse_walk(child, fhir_value[key], source_model, columns)
    elif isinstance(mapping_node, list):
        # A single-element template list maps element-wise onto the FHIR array's first
        # entry; multi-element templates are ambiguous to invert and are left alone.
        if len(mapping_node) == 1 and isinstance(fhir_value, list) and fhir_value:
            _reverse_walk(mapping_node[0], fhir_value[0], source_model, columns)


def split_resource(resource, source_model, mapping):
    """Split an incoming FHIR resource dict into (columns, aux_fhir_data).

    ``source_model`` is the Django model class name the mapping's paths are prefixed with
    (for a writable resource this equals the resourceType). ``columns`` is a dict of
    directly-mapped model field values recovered from the payload via the config.
    ``aux_fhir_data`` is everything the config does not claim at the top level (minus
    ``resourceType``/``id``) -- the inverse of the read path, where mapped top-level fields
    overlay the aux blob. Sub-fields of a partially-mapped top-level structure are not
    preserved, mirroring that wholesale field-level replace.
    """
    columns = {}
    _reverse_walk(mapping, resource, source_model, columns)
    aux_fhir_data = {
        key: value for key, value in resource.items() if key not in mapping and key not in ("resourceType", "id")
    }
    return columns, aux_fhir_data


# ---------------------------------------------------------------------------
# Annotations (``__criteria``, ``__interaction``)
# ---------------------------------------------------------------------------
#
# A mapping may carry ``__`` annotations -- metadata about the resource rather than FHIR
# fields. ``__criteria`` is a predicate on the incoming resource used by the write path to
# decide whether the mapped (column) path applies. ``__interaction`` is an allow-list of
# the FHIR interactions the resource exposes. Annotations are never rendered into output.

# The FHIR interactions an endpoint can expose, and how they map to the usual Django/DRF
# actions: search->list, read->retrieve, create->create, update->update/partial_update,
# delete->destroy.
ALL_INTERACTIONS = ("create", "read", "update", "search", "delete")


def _find_annotation(mapping, name):
    """Return the value of the ``name`` annotation declared anywhere in a mapping, or None."""
    if isinstance(mapping, dict):
        if name in mapping:
            return mapping[name]
        for value in mapping.values():
            found = _find_annotation(value, name)
            if found is not None:
                return found
    elif isinstance(mapping, list):
        for value in mapping:
            found = _find_annotation(value, name)
            if found is not None:
                return found
    return None


def get_mapping_criteria(mapping):
    """Return the ``__criteria`` annotation declared anywhere in a mapping, or ``None``."""
    return _find_annotation(mapping, "__criteria")


def get_mapping_interactions(mapping):
    """Return the ``__interaction`` allow-list for a mapping, or ``None`` (all allowed).

    ``None`` means the annotation is absent, so every interaction is permitted by default.
    """
    return _find_annotation(mapping, "__interaction")


def matches_criteria(resource, criteria):
    """Evaluate a ``__criteria`` expression against an incoming FHIR resource dict.

    Supported form: ``"<param>=<system>|<code>"``. Currently only ``param == "code"`` is
    understood, matching a ``code.coding[*]`` whose ``system`` (and ``code``, when given)
    equals the expression's. An unrecognised expression matches (the mapped path applies),
    so a misconfigured criteria never silently diverts data to the aux blob.
    """
    param, _, value = criteria.partition("=")
    if param.strip() != "code":
        return True
    system, _, code = value.partition("|")
    for coding in ((resource.get("code") or {}).get("coding")) or []:
        if system and coding.get("system") != system:
            continue
        if code and coding.get("code") != code:
            continue
        return True
    return False
