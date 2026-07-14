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
[data/fhir/fhir-cross-version-package](data/fhir/fhir-cross-version-package). We ship only the
**compiled StructureMap JSON**, not the FML source, and only the subset R4 → R5 ingestion of the
configured `aux_resources` actually needs: the `*4to5` map for each aux resource type plus every
datatype/infrastructure map they recurse into (**72 files**; the other-direction, non-aux-resource,
ConceptMap and index/metadata files are not carried). Override the directory with the
`FHIR_XVER_PACKAGE_DIR` Django setting.

Only **`*4to5`** StructureMaps are loaded — the R4B `*4Bto5` variants are deliberately skipped.

#### Provenance — why we compile it ourselves

HL7 publishes the pack at <https://build.fhir.org/ig/HL7/fhir-cross-version/package.tgz>, but that
**CI build is frozen at v0.1.0, built 2024-02-22** and has not been regenerated since (its own
`conversion-registry.html` nav link 404s). The source repo, meanwhile, has moved on by ~160 commits.
Those commits fix real dropped fields, so we compile the maps from source rather than consume the
stale artifact.

The pack in this repo is compiled from:

| | |
| ------------------- | ------------------------------------------------------------------------------ |
| Repo                | <https://github.com/HL7/fhir-cross-version>                                     |
| Commit              | `8113cd23751e23824816298af4bed8d41018beb0` (2026-03-25)                         |
| FML source          | `input/R4toR5/*.fml` (the repo ships FML; the JSON is generated)                |
| Compiler            | HL7 `validator_cli.jar` **6.9.11**, <https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar> |
| JDK                 | Temurin **21** (the validator needs 17+), <https://api.adoptium.net/v3/binary/latest/21/ga/mac/x64/jdk/hotspot/normal/eclipse?project=jdk> |

> **Not to be confused with `hl7.fhir.uv.extensions.r5`** (the FHIR *Extensions* Pack, currently
> v5.3.0). It appears as a *dependency* inside the xver pack's `package.json` and is easily grabbed
> by mistake — it contains CodeSystems/ValueSets/extension StructureDefinitions and **zero**
> cross-version StructureMaps. `hl7.fhir.uv.xver` is still versioned 0.1.0.

#### Regenerating the pack

```bash
# Pin the commit -- HL7 rewrites these maps continuously; drop the checkout to take latest.
git clone https://github.com/HL7/fhir-cross-version.git
git -C fhir-cross-version checkout 8113cd23751e23824816298af4bed8d41018beb0

curl -sSL -o validator_cli.jar \
  https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar

FML=$PWD/fhir-cross-version/input/R4toR5
PKG=$PWD/data/fhir/fhir-cross-version-package

# One invocation per map. The map is addressed by its canonical URL and the FML directory is
# loaded as an IG -- passing the .fml *path* instead silently fails while still printing
# "Successfully compiled" (validator bug).
for f in "$PKG"/StructureMap-*.json; do
  name=$(basename "$f" .json); name=${name#StructureMap-}      # e.g. Observation4to5
  java -jar validator_cli.jar compile \
    "http://hl7.org/fhir/uv/xver/StructureMap/$name" \
    -ig "$FML" -version 5.0.0 -output "$PKG/StructureMap-$name.json"
done
```

Each invocation reloads the FHIR core packages (~35 s), so a full 72-map run takes ~40 minutes.
The set of maps to compile is just "the ones already in `$PKG`" — to **add a resource type to
`aux_resources`**, compile its `StructureMap-<Type>4to5.json` (and any datatype map it introduces)
into the same directory.

Nothing else in the repo depends on the FML source, so it is not vendored.

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

**Shipped patch — `StructureMap-MedicationRequest4to5-patch.json`.** R5 merged R4's
`reasonCode`/`reasonReference` into a single `reason` **`CodeableReference`**, but the official map
still maps both to `reason` through the anonymous default group, and neither source rule carries a
`type` hint — so the engine has nothing to type them with and the element drops. The patch
overrides both rules to:

- call the HL7 conversion groups **by explicit name** (`CodeableConcept2CodeableReference` → nests
  under `.concept`, `Reference2CodeableReference` → under `.reference`), sidestepping type
  inference for the element itself; and
- declare an explicit source **`type`** (`CodeableConcept` / `Reference`), which lets the
  source-type fallback type the value's *children* as well. Without it the untypedness propagates
  and only the primitive `.text` survives — `.coding` is silently dropped.

> `medication[x] → medication` (also became a `CodeableReference`) needs **no** patch — its source
> rules *do* carry a `type` hint, so the engine handles it generically (see the dispatch refinements
> in [How the engine works](#how-the-engine-works)).

**Retired patch — `dosageInstruction`.** The 2024 pack had no rule for it at all, so all dosing/sig
was silently dropped and we patched the rule in. The 2026-03 maps add it upstream
(`src.dosageInstruction -> tgt.dosageInstruction;`), so the patch rule was **removed** — this is the
rule-level merge paying off exactly as intended: the local correction was the only thing that had to
be dropped, and the rest of the map kept tracking upstream.

**Adding a patch.** Drop a `StructureMap-<Anything>.json` into the patches dir with a `group[]`;
each rule you include upserts by `name` into the official group of the matching name. A patch group
whose `name` has no official counterpart is added as a new group.

## How the engine works

FML (FHIR Mapping Language) StructureMaps are executable transforms. Running them faithfully is
tractable here because, across all R4 → R5 maps, the feature surface is tiny:

- **Transforms used:** `create`, `copy`, `translate` (and `evaluate`, which appears only in
  `Subscription` and is skipped). No `listMode`, no `check`.
- **Conditions** are almost all trivial equality (`code = 'Patient'`, `(s = 'allergy')`).
- **Datatype recursion** happens via `dependent` group calls — explicit or *implied* (§4).

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
   computes the key as `base + TitleCase(type)`.

   A `type` on a **non-choice** element, though, is a plain *type assertion*, not a choice
   discriminator — `reasonCode : CodeableConcept` is still keyed `reasonCode`, not
   `reasonCodeCodeableConcept`. The engine therefore resolves the key against the data: it prefers
   the flattened form and falls back to the bare element name when only that is present.

4. **Typed datatype dispatch — explicit and implicit.** Recursing into `Observation.code` means
   resolving the group for the element's **runtime type** (`code` → the `CodeableConcept` group).
   The maps don't carry that type, so the engine recovers it by introspecting the **R5
   `fhir.resources`** model of the current type. A field annotation ending in `Type` is a complex
   type (→ recurse into its group); anything else is a **primitive** (→ copy the scalar directly,
   since FHIR JSON primitives are bare scalars, not objects). Backbone elements
   (`ObservationComponent`) dispatch to the group whose name matches the type.

   **The dispatch is triggered two ways, and the engine must support both** — this is the one place
   the two published packs genuinely differ:
   - **explicitly**, via an anonymous `dependent` (`DefaultMappingGroupAnonymousAlias(code, code)`).
     The 2024 v0.1.0 pack was generated by a publisher that *materialised* this on nearly every rule
     (56 of 57 in `Observation4to5`);
   - **implicitly**, from a bare `src.code -> tgt.code` with no `dependent` at all. This is what FML
     actually means — the engine is expected to dispatch on the element's type — and it is what the
     current publisher emits (3 of 58 rules in `Observation4to5` carry a dependent).

   So the engine **synthesises the implied dependent** for any complex element a rule creates
   without one ([`_run_implicit_dependent`](core/fhir/cross_version.py)). Without this, a modern
   pack would create an empty `{}` for every complex element, recurse into nothing, and prune it
   away — near-total data loss. Both forms route through the same group-selection logic
   ([`_datatype_group`](core/fhir/cross_version.py)), so the engine runs either pack unchanged.

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

   Note the untypedness is **contagious**: an untyped source var cannot type its own children
   either, so a renamed element with no hint loses its complex sub-structure even when the element
   itself is created. This is why the patch declares a source `type` rather than only naming the
   conversion group.

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
  such a rule is skipped. Both the bare (`type = 'allergy'`) and parenthesised (`(s = 'allergy')`)
  spellings are handled — the newer maps use the latter.
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
  `reasonReference` convert to correctly-nested R5 `CodeableReference`; `reason` carries its
  `.coding` and not just `.text` (the source-`type` half of the patch); `dosageInstruction` survives;
  and the patch loader is verified to **override** the official rule of the same name rather than
  append a duplicate.
- **Pack-idiom coverage** (no DB): implicit datatype dispatch (a rule with no `dependent` still
  recurses — `Observation.component.referenceRange`), and parenthesised rule conditions
  (`AllergyIntolerance.type`). Both are what the current pack emits and the 2024 pack did not; they
  are the two things that would break silently on a pack bump.
- **Endpoint**: single-resource POST returns a one-entry Bundle and stores the aux row with
  provenance; a dropped optional field (`CarePlan.activity.detail`) yields a `warning` outcome with
  the resource still created; an **error** entry (`Coverage.payor`) still carries the drop warning;
  request-level source header → `400`; unsupported type → error entry; `GET` → `405`; Bundle with
  per-entry success + error outcomes, each carrying an `OperationOutcome`.

A crash-smoke over all 28 configured aux resource types confirms every type converts without error.

## Adding / changing coverage

- **New resource type:** compile its `StructureMap-<Type>4to5.json` into the pack directory (see
  [Regenerating the pack](#regenerating-the-pack)) — no code change. (The type still needs to be a
  configured aux or mapped resource to be *stored*.)
- **Different map pack / version:** set `FHIR_XVER_PACKAGE_DIR`.
- The engine is map-driven; extend it only to support a new FML feature (e.g. a richer condition
  grammar), in [cross_version.py](core/fhir/cross_version.py).

### Fields recovered by the 2026-03 pack

Moving off the frozen 2024 build fixed six elements that the maps previously had no rule for at all
(they were dropped silently, and reported as `warning` drops):

| Element                              | Note                                          |
| ------------------------------------ | --------------------------------------------- |
| `Observation.component.referenceRange` | on the **mapped** OMH resource                |
| `QuestionnaireResponse.item`           | the entire response payload                   |
| `Provenance.agent`                    |                                               |
| `MedicationRequest.dosageInstruction`  | previously supplied by our patch, now upstream |
| `Bundle.link`                          |                                               |
| `Parameters.parameter.part`            |                                               |

Of the 72 maps we load, 15 had substantive rule changes; the rest of the churn was the canonical-URL
rebase to `hl7.org/fhir/uv/xver/...` (harmless — the loader indexes groups by `name`, not `url`) and
ConceptMap renames (harmless — value-set ConceptMaps are unbundled and pass through, see §5).
