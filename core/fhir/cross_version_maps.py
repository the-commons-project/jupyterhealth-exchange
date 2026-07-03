"""Loads the HL7 ``hl7.fhir.uv.xver`` StructureMaps and ConceptMaps (R4 -> R5 only) into lookup
registries used by the engine. See ``fhir-r4-import.md`` (repo root).

Only ``*4to5`` StructureMaps are loaded (the R4B ``*4Bto5`` variants are deliberately skipped).
"""

import functools
import glob
import json
import logging
import os
import re

from django.conf import settings

logger = logging.getLogger(__name__)

# The engine dispatches an anonymous ``dependent`` call to the "default" group for a type -- the
# group declared with one of these typeModes. (Groups with typeMode ``types`` are conversion
# groups invoked only by explicit name, e.g. ``code2CodeableConcept``.)
_DEFAULT_TYPE_MODES = {"type", "type-and-types", "type-and-default"}

_VERSION_SUFFIX = re.compile(r"(R4B|R4|R5|R3|R2)$")


def _package_dir():
    return getattr(
        settings,
        "FHIR_XVER_PACKAGE_DIR",
        os.path.join(settings.BASE_DIR, "data", "fhir", "fhir-cross-version-package"),
    )


def _base_type(type_name):
    """``CodeableConceptR4`` -> ``CodeableConcept`` (strip the version suffix)."""
    if not type_name:
        return None
    return _VERSION_SUFFIX.sub("", type_name)


class XVerMaps:
    """Group and ConceptMap registries loaded from the cross-version package directory."""

    def __init__(self, package_dir=None):
        self.package_dir = package_dir or _package_dir()
        self.groups = {}  # group name -> group dict
        self.default_groups = {}  # source base type name -> group dict (typeMode type*)
        self.conceptmaps = {}  # ConceptMap url -> {source_code: target_code}
        self._missing_conceptmaps = set()
        self._load()

    def _load(self):
        for path in sorted(glob.glob(os.path.join(self.package_dir, "StructureMap-*4to5.json"))):
            # ``*4to5`` only -- skip the R4B ``*4Bto5`` maps.
            if path.endswith("4Bto5.json"):
                continue
            try:
                with open(path) as handle:
                    doc = json.load(handle)
            except (OSError, ValueError) as exc:
                logger.warning("cross_version: could not load %s: %s", path, exc)
                continue
            for group in doc.get("group", []):
                self.groups.setdefault(group["name"], group)
                if group.get("typeMode") in _DEFAULT_TYPE_MODES:
                    inputs = group.get("input", [])
                    src = next((i for i in inputs if i.get("mode") == "source"), None)
                    base = _base_type(src.get("type")) if src else None
                    if base:
                        self.default_groups.setdefault(base, group)

        for path in sorted(glob.glob(os.path.join(self.package_dir, "ConceptMap-*.json"))):
            try:
                with open(path) as handle:
                    doc = json.load(handle)
            except (OSError, ValueError):
                continue
            url = doc.get("url")
            if not url:
                continue
            table = {}
            for group in doc.get("group", []):
                for element in group.get("element", []):
                    targets = [t.get("code") for t in element.get("target", []) if t.get("code")]
                    if targets:
                        table[element["code"]] = targets[0]
            self.conceptmaps[url] = table

        logger.info(
            "cross_version: loaded %d groups (%d default), %d ConceptMaps from %s",
            len(self.groups),
            len(self.default_groups),
            len(self.conceptmaps),
            self.package_dir,
        )

    # -- lookups --

    def group_for(self, name):
        return self.groups.get(name)

    def default_group_for(self, type_name):
        return self.default_groups.get(type_name)

    def translate(self, url, code):
        """Map ``code`` through the ConceptMap at ``url``. Unknown ConceptMap or unmapped code
        passes the code through unchanged (the per-element value-set ConceptMaps are not bundled;
        R5 validation is the safety net)."""
        table = self.conceptmaps.get(url)
        if table is None:
            if url not in self._missing_conceptmaps:
                self._missing_conceptmaps.add(url)
                logger.debug("cross_version: ConceptMap %s not bundled; codes passed through", url)
            return code
        return table.get(code, code)


@functools.lru_cache(maxsize=1)
def get_maps():
    """Process-wide singleton (loaded once, then cached)."""
    return XVerMaps()
