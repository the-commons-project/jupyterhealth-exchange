# FHIR R4 → R5 Import

Ingests **FHIR R4** resources, converts them to **R5**, and stores them through the existing
write path. A small dict-based interpreter runs the official HL7 cross-version FML StructureMaps
(the `hl7.fhir.uv.xver` pack) in the R4 → R5 direction.

> **This conversion is best-effort and lossy by design.** The HL7 maps drop R4 fields that have no
> R5 home, and several map features are approximated (see [Limitations](#known-limitations)). The
> gate that keeps output honest is **R5 validation on the normal create path** — anything the
> transform cannot produce cleanly is rejected there with a 400.

## Endpoint

A single view, [`FHIRImportView`](core/views/fhir_import.py) (a subclass of `FHIRResourceView`),
mounted in [core/urls.py](core/urls.py):

| Method & path                     | Body                          | Behaviour                                                              |
| --------------------------------- | ----------------------------- | --------------------------------------------------------------------- |
| `POST /fhir-import/R4/<resource>` | one R4 resource               | convert → **normal create routing**                                   |
| `POST /fhir-import/R4`            | an R4 `Bundle` of resources   | convert & create each entry                                           |

It is **create-only** — `GET`/`PUT`/`PATCH`/`DELETE` return `405`.

After conversion, each resource is handed to the inherited `FHIRResourceView._create`, so the R4
path **reuses everything downstream unchanged**:

- **mapped-vs-aux routing** — an OMH-coded `Observation` writes the `Observation` model; every
  other resource (and every non-OMH `Observation`) lands in `FhirAuxResource`
  (see [fhir-engine doc](../jupyterhealth-software-documentation-tcp/jhe/fhir/fhir-engine.md));
- **`X-JHE-FHIR-Source-ID` header** — required, exactly as for a normal write;
- **R5 validation** against `fhir.resources`, and **JHE provenance stamping** on the aux row.

### Response: always a Bundle with per-entry outcomes

Because import is a **lossy conversion, not a pure create**, *both* endpoints always return a
`200` `batch-response` **Bundle** (a single-resource POST yields a one-entry Bundle). Every entry
carries an `OperationOutcome` at `response.outcome`:

- **success, no loss** → `response.status: "201 Created"` + the created resource + an
  `information` outcome (`"Converted R4 -> R5 with no detected field loss."`);
- **success, with loss** → `201 Created` + the created resource + one `warning` issue **per
  dropped R4 path** (see [Data-loss reporting](#data-loss-reporting));
- **failure** (unsupported type, invalid R5, a required field dropped, …) → an error status and an
  `error` outcome (plus any drop `warning`s — a dropped *required* field is usually the cause of the
  failure), no `resource`.

The `X-JHE-FHIR-Source-ID` header gates the **whole request** (one header for the batch), so a
missing / unknown / forbidden source is a request-level `400` / `403`, not a per-entry outcome.

Entries are processed **independently (batch semantics)** even if the request Bundle declares
`type: transaction` — the conversion is not atomic.

### Data-loss reporting

The lossy nature of the conversion (fields with no R5 home are dropped — see
[Limitations](#known-limitations)) is surfaced rather than silent. After converting a resource,
[`dropped_field_paths`](core/fhir/cross_version.py) diffs the R4 input against the R5 output by
**scalar leaf value**: any R4 leaf whose value appears nowhere in the output is reported (a
fully-dropped subtree collapses to its highest path). Because it matches on *value*, a genuine
R4 → R5 **rename that preserves the data is not flagged** — only real loss is. Each dropped path
becomes a `warning` issue in the entry's outcome (`expression: ["<Resource>.<path>"]`) **and** a
server-side `logger.warning`. It is a heuristic: a dropped value that happens to duplicate a
surviving value elsewhere is missed.

## Architecture

The engine is three flat modules under `core/fhir/`, completely decoupled from the view — its
public surface is one function:

```python
from core.fhir.cross_version import transform_to_r5
r5_body = transform_to_r5("Observation", r4_body_camelcase_dict)   # -> R5 dict
```

| File                                                                    | Responsibility                                                                                                       |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| [cross_version_maps.py](core/fhir/cross_version_maps.py)                | Loads the `*4to5` StructureMaps + ConceptMaps into group/default-group/translate registries (cached singleton).      |
| [cross_version_type_index.py](core/fhir/cross_version_type_index.py)    | Recovers element → type info by introspecting the installed **R5 `fhir.resources`** models (no external download).    |
| [cross_version.py](core/fhir/cross_version.py)                          | The FML executor: walks a resource group's rules, applies transforms, recurses into datatype groups.                 |
| [views/fhir_import.py](core/views/fhir_import.py)                       | The endpoint (single + Bundle) that camelizes, converts, and delegates to the normal create routing.                |

### The map package

The maps are the HL7 `hl7.fhir.uv.xver` pack in
[data/fhir/fhir-cross-version-package](data/fhir/fhir-cross-version-package), obtained from
<https://build.fhir.org/ig/HL7/fhir-cross-version/package.tgz>. The pack has been
**pruned to only what R4 → R5 ingestion of the configured `aux_resources` needs** — the `*4to5`
StructureMaps for each aux resource type plus every datatype/infrastructure map they recurse into
(72 files; the ~1200 other-direction, non-aux-resource, ConceptMap, and index/metadata files were
removed). Override the directory with the `FHIR_XVER_PACKAGE_DIR` Django setting.

> **If you add a resource type to `aux_resources`**, re-add its `StructureMap-<Type>4to5.json`
> (and any datatype map it introduces) from the upstream `hl7.fhir.uv.xver` pack.

Only **`*4to5`** StructureMaps are loaded — the R4B `*4Bto5` variants are deliberately skipped.

### Local patches

Some official maps have gaps — a missing rule, or a rule that predates an R5 restructure. Rather
than edit the upstream package (which we never touch, so it keeps tracking upstream), JHE-local
corrections live in a **separate** directory,
[data/fhir/fhir-cross-version-patches](data/fhir/fhir-cross-version-patches) (override with
`FHIR_XVER_PATCHES_DIR`).

**How merging works.** After the official pack loads, each patch file is merged over the official
groups **rule-by-rule** ([`_apply_patches` / `_merge_rules`](core/fhir/cross_version_maps.py)): a
patch is a StructureMap-shaped JSON whose group rules **replace the official rule of the same
`name`** (or are appended if new). A patch group with no official counterpart is registered
wholesale. Everything else in each official map is left untouched — only the named rules are
overridden.

**Design decision — why rule-level merge, not a whole-group override.** A whole-group override is a
simpler loader, but each overridden group becomes a full fork that stops receiving upstream fixes.
Rule-level merge keeps a patch down to the handful of rules that are actually wrong, so the rest of
the map still tracks the HL7 pack.

**Shipped patch — `StructureMap-MedicationRequest4to5-patch.json`.** The official
`MedicationRequest4to5` map has two gaps that R5's remodelling introduced:

- it maps R4 `reasonCode`/`reasonReference` to `reason` via the anonymous default group, but R5
  merged them into a single `reason` **`CodeableReference`** — and these two source rules carry **no
  `type` hint**, so the engine's source-type fallback (below) can't type them. The patch overrides
  both rules to call the HL7 conversion groups **by explicit name**
  (`CodeableConcept2CodeableReference` → nests under `.concept`, `Reference2CodeableReference` →
  under `.reference`), which sidesteps type inference entirely.
- it has **no rule for `dosageInstruction`** at all (the string never appears in the map), so all
  dosing/sig was silently dropped. `dosageInstruction` is unchanged (still `Dosage`) in R5, so the
  patch just adds the one missing rule; the engine then dispatches it to the stock `Dosage` group.

> `medication[x] → medication` (also became a `CodeableReference`) needs **no** patch — its source
> rules *do* carry a `type` hint, so the engine handles it generically (see the dispatch refinements
> in [How the engine works](#how-the-engine-works)).

**Design decision — patch now vs. wait for a newer pack.** HL7 may add `dosageInstruction` in a
later build. We ship the ~5-line local patch now (it is self-contained and unblocks dosing
immediately); if a pack bump later supplies the rule, the patch simply re-asserts the same mapping
and can be removed. Re-evaluating a pack bump is a standing follow-up.

**Adding a patch.** Drop a `StructureMap-<Anything>.json` into the patches dir with a `group[]`;
each rule you include upserts by `name` into the official group of the matching name. A patch group
whose `name` has no official counterpart is added as a new group.

## How the engine works

FML (FHIR Mapping Language) StructureMaps are executable transforms. Running them faithfully is
tractable here because, across all R4 → R5 maps, the feature surface is tiny:

- **Transforms used:** `create`, `copy`, `translate` (and `evaluate`, which appears only in
  `Subscription` and is skipped). No `listMode`, no `check`.
- **Conditions** are almost all trivial equality (`code = 'Patient'`).
- **Datatype recursion** happens via `dependent` group calls.

The executor operates directly on **camelCased JSON dicts** (not the typed `fhir.resources` object
tree). Key mechanics:

1. **Resource group + `extends` chain.** Each resource has a group (e.g. `Observation`) whose rules
   map its elements. The group `extends: DomainResource` → `Resource`, so those parent groups run
   first to carry `id` / `meta` / `text` / `extension`.

2. **Source & target variable pools.** The generated maps reuse one variable name (e.g. `vvv`) for
   both the selected source element and the created target element. The engine keeps **separate
   `svars` / `tvars` pools**; a `dependent` resolves each positional argument against the pool
   matching the called group's input `mode` (`source`/`target`). This is what makes
   `Group(vvv, vvv)` mean `Group(sourceVar, targetVar)`.

3. **Choice types (`value[x]`).** FML addresses a choice element by base name + a `type` hint
   (`effective` + `dateTime`); FHIR JSON flattens it into one key (`effectiveDateTime`). The engine
   computes the key as `base + TitleCase(type)` on both read and write — no external type data
   needed for this case.

4. **Typed datatype dispatch.** An anonymous `dependent`
   (`DefaultMappingGroupAnonymousAlias(code, code)`) must resolve to the group for the element's
   **runtime type** (`code` → the `CodeableConcept` group). The maps don't carry that type, so the
   engine recovers it by introspecting the **R5 `fhir.resources`** model of the current type
   (`Observation.code` → `CodeableConcept`). A field annotation ending in `Type` is a complex type
   (→ recurse into its group); anything else is a **primitive** (→ copy the scalar directly, since
   FHIR JSON primitives are bare scalars, not objects). Backbone elements
   (`ObservationComponent`) dispatch to the group whose name matches the type.

   Two refinements handle elements **renamed or retyped between R4 and R5**:
   - **source-type fallback** — when R5 introspection finds no such field (the element was renamed,
     e.g. R4 `medication[x]` → R5 `medication`), the engine falls back to the map rule's explicit
     source `type` hint to type the value;
   - **(source, target)-pair dispatch** — the default group is chosen by *both* the source and the
     created target type, so a type change (R4 `Reference` → R5 `CodeableReference`) selects the HL7
     conversion group (`Reference2CodeableReference` nests under `.reference`,
     `CodeableConcept2CodeableReference` under `.concept`) instead of the same-type group.

   Together these convert `MedicationRequest.medication[x]` → the R5 `medication` `CodeableReference`
   **generically, without touching the official maps** — the fix applies to every `X → CodeableReference`
   element across all resources. **Design decision:** the maps already ship the correct conversion
   groups, so the engine *selects an existing HL7 group by (source, target) type* rather than
   hand-coding a Reference/CodeableConcept "wrap"; there is no bespoke per-type nesting logic to
   maintain. The refinement needs the source type, which it gets from the rule's `type` hint — so
   elements that were renamed **and** carry no hint (e.g. `MedicationRequest.reasonCode`/
   `reasonReference`, `Coverage.payor`) are *not* covered here; the first two are addressed by the
   [local patch](#local-patches) and `payor` remains a reported drop.

5. **Value-set `translate`.** `translate` maps a code through a named ConceptMap. The per-element
   value-set ConceptMaps (e.g. `Observation.status-R4toR5`) are **not bundled** in the pack — only
   the structural `resources`/`types`/`elements` maps are — so an unbundled ConceptMap **passes the
   code through unchanged** (R5 validation catches a genuinely invalid code).

6. **Pruning & robustness.** Empty containers left by a `create` whose dependent produced nothing
   are pruned. Every rule runs under a `try/except` that **logs and skips** on error rather than
   failing the whole conversion, and recursion is depth-guarded — consistent with the best-effort
   contract.

## Known limitations

These are the deliberate lossy edges (all consistent with "best-effort, R5-validated"). Note that
field loss is **reported, not silent** — see [Data-loss reporting](#data-loss-reporting):

- **Dropped fields.** R4 elements with no R5 mapping are dropped by the HL7 maps themselves; the
  import surfaces them as `warning` outcomes and a server log. (If a *required* R5 field is what
  was dropped, the resource fails R5 validation and becomes an `error` entry instead.)
- **Unbundled value-set ConceptMaps** → codes pass through untranslated (see §5). If a code is
  invalid in R5, the create fails validation.
- **R5-introspection assumption.** Element types come from the R5 model. A **renamed** element is
  handled when the map rule carries a source `type` hint (choice elements like `medication[x]`) or a
  patch supplies an explicit dependent; a renamed element with *neither* (e.g. `Coverage.payor`,
  which has no type hint upstream) still drops rather than mis-shaping, and is reported.
- **Conditions** beyond simple `=` / `!=` equality (FHIRPath `.all()`, `as` casts) are unsupported;
  such a rule is skipped.
- **`evaluate`** (FHIRPath expression transform, `Subscription`-only) is not implemented.
- **Primitive extensions** (the `_field` sibling objects) are not carried.
- **Bundles are not atomic** (batch semantics; per-entry success/failure).
- **Resource `id` length.** FHIR caps `id` at 64 chars and `fhir.resources` enforces it, so an
  over-long upstream id (e.g. some Epic ids) is rejected by R5 validation — expected. Move it into an
  `identifier` before import (the id space is JHE's own UUIDs anyway).

## Testing

[tests/backend/test_fhir_r4_import.py](tests/backend/test_fhir_r4_import.py):

- **Engine shape** (no DB): choice elements, repeating backbone elements, repeating *primitive*
  elements (lists must not collapse), and R5 validity for `Observation` / `Patient` / `Condition`.
- **Data-loss detection** (no DB): `dropped_field_paths` flags genuine loss but not value-preserving
  renames, and collapses a fully-dropped subtree to its root path.
- **CodeableReference + patches** (no DB): `MedicationRequest.medication[x]` / `reasonCode` /
  `reasonReference` convert to correctly-nested R5 `CodeableReference`, `dosageInstruction` survives
  via the patch, and the patch loader is verified to merge rules over the official group.
- **Endpoint**: single-resource POST returns a one-entry Bundle and stores the aux row with
  provenance; a dropped optional field (`CarePlan.activity.detail`) yields a `warning` outcome with
  the resource still created; an **error** entry (`Coverage.payor`) still carries the drop warning;
  request-level source header → `400`; unsupported type → error entry; `GET` → `405`; Bundle with
  per-entry success + error outcomes, each carrying an `OperationOutcome`.

A crash-smoke over all 27 configured aux resource types confirms every type converts without error.

## Adding / changing coverage

- **New resource type:** if the pack has a `StructureMap-<Type>4to5.json`, it works automatically —
  no code change. (The type still needs to be a configured aux or mapped resource to be *stored*.)
- **Different map pack / version:** set `FHIR_XVER_PACKAGE_DIR`.
- The engine is map-driven; extend it only to support a new FML feature (e.g. a richer condition
  grammar), in [cross_version.py](core/fhir/cross_version.py).
