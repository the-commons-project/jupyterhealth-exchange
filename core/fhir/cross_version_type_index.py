"""Element -> type resolution for the R4 -> R5 engine, sourced from the installed
``fhir.resources`` (R5) models.

The cross-version StructureMaps dispatch datatype ``dependent`` calls by the *runtime type* of
the element being mapped (e.g. ``Observation.code`` -> the ``CodeableConcept`` group). The maps
do not carry that type, so we recover it by introspecting the R5 ``fhir.resources`` model
classes:

- a field annotation whose class name ends in ``Type`` (e.g. ``CodeableConceptType``) is a
  **complex** type; its model class is the annotation name minus the ``Type`` suffix
  (``CodeableConcept``), looked up in the model registry;
- any other annotation (``Code``, ``String``, ``DateTime`` ...) is a **primitive** -- the engine
  copies its scalar value directly instead of recursing into a datatype group.

Choice elements (``value[x]``) are already flattened in FHIR JSON (``valueQuantity``), so the
flattened key is looked up directly. Introspecting the *R5* model for an *R4* body assumes the
two shapes agree for the element; where they do not, the field resolves to ``None`` and is
handled as a primitive/passthrough (a documented lossy edge).
"""

import functools
import importlib
import pkgutil
import typing

import fhir.resources as _fr
from fhir.resources.core.fhirabstractmodel import FHIRAbstractModel


@functools.lru_cache(maxsize=1)
def _registry():
    """All ``fhir.resources`` model classes keyed by class name (resources + datatypes +
    backbone elements). Built once by walking the package."""
    reg = {}
    for module_info in pkgutil.iter_modules(_fr.__path__):
        if module_info.name in ("core", "fhirtypes", "fhirtypesvalidators"):
            continue
        try:
            module = importlib.import_module(f"fhir.resources.{module_info.name}")
        except Exception:
            continue
        for attr in dir(module):
            obj = getattr(module, attr)
            if isinstance(obj, type) and issubclass(obj, FHIRAbstractModel):
                reg.setdefault(obj.__name__, obj)
    return reg


def model_for(type_name):
    """R5 model class for a FHIR type/resource name, or ``None`` if unknown (a primitive)."""
    if not type_name:
        return None
    return _registry().get(type_name)


def _innermost(annotation):
    """Peel ``Optional`` / ``List`` / ``Union`` down to the first concrete inner class."""
    args = typing.get_args(annotation)
    if not args:
        return annotation
    for arg in args:
        if arg is type(None):
            continue
        return _innermost(arg)
    return annotation


def _field(parent_cls, json_key):
    if parent_cls is None:
        return None
    return getattr(parent_cls, "__fields__", {}).get(json_key)


def has_field(parent_cls, json_key):
    """Does ``parent_cls`` declare ``json_key`` directly (i.e. it is not a flattened choice)?"""
    return _field(parent_cls, json_key) is not None


def child_type(parent_cls, json_key):
    """``(model_cls_or_None, type_name)`` for ``parent_cls.<json_key>``.

    ``model_cls`` is ``None`` for a primitive-typed child (copy the scalar) or an unknown field.
    ``type_name`` is the complex model name (``CodeableConcept``) or the primitive annotation name
    (``DateTime``); ``None`` when the field is unknown.
    """
    field = _field(parent_cls, json_key)
    if field is None:
        return None, None
    inner = _innermost(field.annotation)
    name = getattr(inner, "__name__", None)
    if name and name.endswith("Type"):
        base = name[:-4]
        return model_for(base), base
    return None, name


def _contains_list(annotation):
    if typing.get_origin(annotation) is list:
        return True
    return any(_contains_list(arg) for arg in typing.get_args(annotation))


def is_list(parent_cls, json_key):
    """Is ``parent_cls.<json_key>`` a repeating (list) element?"""
    field = _field(parent_cls, json_key)
    if field is None:
        return False
    return _contains_list(field.annotation)
