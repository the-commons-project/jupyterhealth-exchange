# RFC 0001: CapabilityStatement source of truth (fhir_config vs. code) + AI-work provenance

- **Status:** Discussion
- **Companion PR:** #676 (CapabilityStatement + smart-configuration) · **Origin issue:** #615
- **Also piloting:** the "share the prompt + decisions with the PR" process proposed by @s1monj

This document does two jobs: (1) it is the provenance manifest for PR #676 —
what was asked, what was decided interactively, and why the code looks the way
it does; (2) it is the design question #615's implementation surfaced, asked
explicitly: **which facts should move into `fhir_config.json` so it stays the
single source of truth**, with a recommendation per gap. Nothing in #676 is
precious: once we agree on the config structure, the builder gets reworked to
match and the hardcoded constants below disappear.

---

## 1. How #676 was built (decision log)

The work was done with Claude (Claude Code), driven by explicit human decisions
at each fork. Condensed to the decisions that shaped the design; the full
working spec and plan exist and can be shared verbatim.

1. **Origin.** Asked "where does the capability statement live?" → discovered
   #615 was still open (never implemented). Self-assigned.
2. **Scope decision (human):** build #615 *and* make the MCP server consume it
   (a `get_server_capabilities` tool + preflight checks that fail open on older
   instances). MCP half is a separate stacked branch, not in #676.
3. **Design constraint (human):** align with the US Core server
   CapabilityStatement IG *as it applies to JHE*. Outcome: adopted its
   conventions (expectation extensions, SMART security block, security
   documentation) while explicitly **not** claiming conformance — no
   `instantiates`, no `supportedProfile`, no invented `searchParam.definition`
   canonicals (US Core is R4; JHE is R5 and lacks vread/history and
   Provenance `_revinclude`).
4. **Requirement (human):** the statement must be public — no authentication.
   Verified to return 200 with no credentials *and* with an invalid bearer.
5. **Additions (human questions → built):** state the OMH `valueAttachment`
   encoding somewhere machine-visible (landed as Observation `documentation`,
   pending a real StructureDefinition), and serve
   `/.well-known/smart-configuration` for SMART public (PKCE) clients.
6. **Truthfulness pass:** checking every declared capability against the actual
   view behavior surfaced the gaps in section 2 — places where the config, as
   it exists today, cannot express what the server really does. Rather than let
   the statement overclaim, each gap was corrected with a hardcoded constant
   plus a `documentation` caveat; those constants are the artifact this RFC
   asks where to home.

Key point for review: the scope beyond #615's one-line description ("read
fhir_config.json and DOT meta, render a minimal statement") came from the
human decisions in steps 2–5, each recorded above — not from the tool
freelancing. The config-vs-code split in section 2, however, was decided
implicitly during implementation, and that is precisely the decision this RFC
hands back.

## 2. Architecture of #676 and the reasoning behind it

Why the implementation is shaped the way it is — the part a diff can't show:

- **Render from the config at request time, no stored artifact.** The builder
  calls only `core/fhir/config.py` accessors — the same allow-lists the FHIR
  dispatcher enforces — so the statement *cannot* drift from routing when the
  config changes, needs no regeneration step, touches no database (safe on a
  public route), and derives its absolute URLs from the request so every
  deployment automatically advertises its own endpoints. The alternatives
  (hand-authored JSON, or a generated file checked into the repo) were
  rejected precisely because they create a second artifact that goes stale.
- **One entry per resource type, union of both stores, caveats in
  `documentation`.** FHIR's CapabilityStatement has no "backing store"
  dimension, and a JHE client cannot choose the store per-param — so
  per-store entries aren't expressible and omitting either store's
  capabilities would under-claim. The union is the honest "what can I send"
  set, with `documentation` carrying the store caveats (which writes reach
  which store, which params need `_source`) so a generic client is never
  silently misled. Everything declared carries expectation `SHALL` because
  the config is an allow-list: declared == supported; nothing speculative.
- **Where config and reality disagree, correct in code rather than
  overclaim.** The constants in section 3 exist because emitting the config
  verbatim would publish false capabilities (e.g. update/delete on mapped
  rows that 405). Accuracy of the *output* was prioritized over purity of
  derivation — with this RFC as the follow-up to move those corrections into
  the config itself.
- **Public, CORS-open, cacheable.** FHIR requires the capabilities
  interaction to be retrievable without authorization; browser-based SMART
  apps consume discovery documents cross-origin; the content is
  deploy-static, hence `Cache-Control` and a build-stable `date`.
- **`smart-configuration` as a second document, not folded into the
  CapabilityStatement.** SMART App Launch mandates the well-known document as
  its own endpoint (it's what public PKCE client libraries fetch first); the
  CapabilityStatement's `oauth-uris` extension is kept too, and both derive
  from the same request, so the two discovery channels cannot disagree.
- **US Core as style guide, not conformance target.** Its conventions
  (expectation extensions, security block shape) make the statement legible
  to tooling that knows US Core, but `instantiates`/`supportedProfile` are
  omitted because claiming them would be false (R4 vs R5; unimplemented
  SHALLs).

## 3. The gaps: facts the statement needs that `fhir_config.json` cannot express

Each row is a "does this belong in the config?" decision.

| # | Fact | Where it lives today | Why the config can't say it | Recommendation |
|---|------|----------------------|------------------------------|----------------|
| 1 | Mapped-store rows do **not** support update/delete (the view 405s them), yet mapped Observation declares `__interaction: ["*"]` | `_MAPPED_IMPLEMENTED = {create, read, search}` | `"*"` means "everything" but the mapped handler implements only create/read/search | **Config.** Replace `"*"` on *mapped* entries with explicit lists (pure config edit, no engine change). Keep `"*"` for aux entries, where it is true. The config stops overclaiming and the constant dies. |
| 2 | `identifier` / `code` filter on *every* search via the canonical patient-scoped filters, even where only the aux `__search` block declares them | `_CANONICAL_PARAMS` | They're view-level behavior (`_canonical_search_kwargs`), not per-resource mapping | **Code.** Engine behavior, not resource mapping; one commented constant is the honest home. |
| 3 | `_id`, `_lastUpdated`, `_source` work on every resource | `_COMMON_SEARCH_PARAMS` | Same — served by `apply_common_search_filters` / the view | **Code**, same reasoning as #2. |
| 4 | `__search.type` values (`const`, `code`, `identifier`) are *matching* strategies, but the statement needs FHIR search-param types (all three are `token`) | `_SEARCH_TYPES` remap | One config field carries two vocabularies | **Config (light).** Keep `type` as-is; allow an optional `fhir_type` override where they differ. Alternatively keep the code map — it's total and stable — if adding a field feels like clutter. Genuinely open. |
| 5 | The aux store serves `PATCH` (JSON merge) wherever it serves update | synthesized (`"patch"` added when `update` ∈ aux) | Config has no patch concept | **Code.** It's an engine invariant (PATCH == partial update on aux), not per-resource. |
| 6 | JHE-native Observations carry OMH data points as base64 `valueAttachment` | `if resource_type == "Observation"` prose | Profile-level fact with no StructureDefinition to reference | **Neither, eventually.** Short-term keep the special case; the real fix is authoring a JHE Observation profile and declaring `supportedProfile` — worth its own issue. |
| 7 | `"R5"` → `"5.0.0"` | `_FHIR_VERSION_NUMBERS` | Config carries the label, not the semver FHIR requires | **Config.** Add `fhir_version_number` next to `fhir_version`. Trivial. |
| 8 | `_sort` (date, lastUpdated) and `_summary=count` support | prose in `rest.documentation` | Partially derivable: `__sortDate` presence implies date sort | **Derive** the date-sort claim from `__sortDate`; `lastUpdated`/`_summary` are engine-level (code). |

If rows 1, 4, 7 (and the derivation in 8) land in the config, `capability.py`
collapses toward the minimal renderer #615 described, and the config is again
the single place that answers "what does this server do".

## 4. Decision requested

1. Agree/adjust the per-row calls above (especially #1's explicit mapped
   interaction lists and #4's `fhir_type`).
2. On agreement: a follow-up commit (happy to fold into #676 or stack) applies
   the config changes and deletes the corresponding constants; the existing 14
   capability tests pin behavior through the refactor.

## 5. Process proposal (the "RFC for Claude prompts" pilot)

For AI-built changes with architectural surface — new endpoints, public API
shape, source-of-truth/config structure — a doc like this one accompanies (or
precedes) the PR: the decision log plus the design questions. Mechanical
changes (dependency bumps, test additions, doc syncs) don't get one; the
review effort should go where the decisions are. This document is the pilot;
if the shape works, the next one happens *before* the implementation lands
rather than retrofitted.

**Open question — where should these live?** This pilot is a PR into
`docs/rfcs/` because that makes it reviewable with normal tooling, but it's
not obvious source control is the right home for prompt/decision provenance
(vs. GitHub Discussions, the issue thread, or a shared doc, with only the
final design decision landing in-repo). Genuinely undecided — pick whatever
you'd actually want to read and comment on, and the next one follows that.
