# RFC 0002: Unblocking dependency security updates — django-saml2-auth-community, uv, and one declared override

- **Status:** Discussion
- **Companion PR:** #679 · **Hotfix for pre-existing main regression:** #678
- **Primary reviewers:** @s1monj (architecture/process), @travis-sauer-oltech (SAML behavior)
- **Follows:** the provenance-manifest format piloted in RFC 0001 (#677)

This document explains why the root Django app's dependency toolchain changes
in the companion PR: what was broken, why every smaller fix was worse, what
the new architecture is, and exactly when the workaround it contains can be
deleted. Bottom line up front: **three dependency pins outside our control had
wedged the app so that no security update to pyjwt or cryptography could ever
be applied**, dependabot's automated security PRs were failing red on every
run (and intermittently blocking dev deploys via the all-checks gate), and the
only clean, declarative exit is the one-line resolver override this PR adds —
which requires uv, because pipenv structurally cannot express it.

---

## 1. The problem

### 1.1 What was visibly broken

- Dependabot's **security-update job for pyjwt fails red** on every trigger
  with `security_update_not_possible`: it wants pyjwt ≥2.13.0 (five published
  advisories, one HIGH), but the dependency graph cannot resolve past 2.12.1.
- The **"Jhe Dev Deploy" workflow's wait-for-all-checks gate** treats those
  failing dependabot runs as failed commit checks, so fly.io dev deploys were
  intermittently skipped depending on timing.
- Dependabot **PR #333 (pyopenssl 26)** has been open and unmergeable for
  months, for reasons nobody had diagnosed.
- Our `Pipfile.lock` was silently **unsound**: it contained
  `cryptography 48.0.1` alongside `pyopenssl 24.2.1`, which declares
  `cryptography <44`. Years of piecemeal single-package bumps produced a
  combination that honest resolution forbids. It kept working only because
  `pipenv sync` installs the lock verbatim with `--no-deps` and never
  re-validates the graph. Any honest relock — including every dependabot
  pip-ecosystem PR — would have **downgraded cryptography by five major
  versions** (to 43.0.3, which carries two HIGH advisories:
  GHSA-537c-gmf6-5ccf, GHSA-r6ph-v2qm-q3c2).

### 1.2 The pin chain that causes all of it

```
Pipfile: grafana-django-saml2-auth = "*"        ← frozen forever at 3.21.0
  └── django-saml2-auth-community == 3.21.0     ← pins pyjwt==2.12.1
        └── pysaml2 == 7.5.4                    ← pins pyopenssl<24.3.0
              └── pyopenssl 24.2.1              ← caps cryptography<44
```

Three independent upstream facts make this chain unfixable in place:

1. **Grafana archived their repo on 2026-06-16.** `grafana-django-saml2-auth`
   3.21.0 is the final release ever published under that name — it is now an
   empty metapackage pointing at `django-saml2-auth-community==3.21.0`. Our
   `"*"` requirement on the grafana name can never resolve to anything newer,
   which permanently pins pyjwt at 2.12.1. The maintained continuation is
   [mostafa/django-saml2-auth](https://github.com/mostafa/django-saml2-auth)
   (PyPI: `django-saml2-auth-community`; same `django_saml2_auth` import
   paths). Its 3.22.0 pins `pyjwt==2.13.0` — the fixed version. The rename is
   mandatory regardless of anything else in this RFC.
2. **pysaml2 is frozen.** Its `pyopenssl<24.3.0` cap was added
   ([commit 735bfa53](https://github.com/IdentityPython/pysaml2/commit/735bfa5327f42080ef60e9fd31d8d31029d98e21))
   because pyopenssl 24.3 removed `OpenSSL.crypto.verify`, which pysaml2 calls
   in **exactly one place** (`saml2/cert.py:331`, `OpenSSLWrapper.verify`) — a
   path reachable only when the `validate_certificate` config option is set
   (we don't set it; it defaults off) and which fails closed even then. The
   fix PRs ([#977](https://github.com/IdentityPython/pysaml2/pull/977)
   removes pyOpenSSL entirely; [#1021](https://github.com/IdentityPython/pysaml2/pull/1021)
   is the minimal port) are approved-by-community but unmerged: the only
   maintainer with merge rights has been unreachable since 2026-02, and the
   IdentityPython community announced a re-grouping effort on 2026-07-15.
   nixpkgs, eduID Sweden, and Italy's national IAM proxy all ship patched
   forks rather than wait.
3. **Modern pyopenssl tracks modern cryptography in lockstep** (26.2 ↔
   cryptography 48, 26.3 ↔ 49). The *only* stale pin in the entire chain is
   pysaml2's. Staying on pyopenssl <26 also leaves its own CVE-2026-27459
   unpatched.

### 1.3 Security drivers, stated honestly

pyjwt 2.13.0 fixes five advisories, including HIGH
[GHSA-xgmm-8j9v-c9wx](https://github.com/advisories/GHSA-xgmm-8j9v-c9wx)
(a public-key JWK accepted as an HMAC secret enables forged HS256 tokens).
**We verified this is not currently exploitable in JHE**: the only JWKS-driven
decode (`core/oidc_verify.py`) restricts `algorithms=["RS256","RS384","ES384"]`,
explicitly excluding the HS* family the attack requires, and issuer/JWKS
provenance is allowlisted before any fetch. So this is defense-in-depth and
hygiene, not an emergency patch — but the *class* of problem is real: we had
an auth-stack package wedged so that **no future pyjwt or cryptography
security fix, exploitable or not, could ever be applied**. That is the
unacceptable part.

## 2. The change

Three moves, one PR:

1. **Package rename** (forced by the Grafana archive):
   `grafana-django-saml2-auth` → `django-saml2-auth-community>=3.22.0`.
   Import paths (`django_saml2_auth.*`), settings (`SAML2_AUTH`), URLs, and
   our vendored ACS view are untouched — zero code changes.
2. **Toolchain: pipenv → uv** for the root app (`Pipfile`/`Pipfile.lock` →
   `pyproject.toml` + `uv.lock`). All version constraints carry over
   unchanged (Django stays exact-pinned, tracking main — 5.2.16 as of the
   #689 rebase; `fhir.resources` stays 7.1.0; `omh-shim` stays 1.4.0). The repo already uses uv + dependabot for
   `/mcp_server`, so this makes the root app the second instance of an
   existing pattern rather than a new toolchain. Dockerfile installs via
   `uv export --frozen --no-dev | uv pip install --system` (same
   system-site-packages layout as `pipenv install --system`); CI uses
   `astral-sh/setup-uv` + `uv sync --frozen` + `uv run pytest`; the
   dependabot root entry flips `pip` → `uv`.
3. **The one-line override** (the reason uv is required):

   ```toml
   [tool.uv]
   override-dependencies = ["pyopenssl>=26.2"]
   ```

   This lifts pysaml2's stale cap and lets pyopenssl/cryptography float
   together honestly. The resulting lock is **fully consistent** — no more
   deliberate unsoundness anywhere:

   | package | before (locked) | after (locked) |
   |---|---|---|
   | pyjwt | 2.12.1 (5 open advisories) | **2.13.0** (clean) |
   | cryptography | 48.0.1 (unsound pairing) | **50.0.0** (clean, honest) |
   | pyopenssl | 24.2.1 (CVE-2026-27459) | **26.4.0** (clean) |
   | django-saml2-auth-community | 3.21.0 via dead shim | **3.22.0** direct |
   | pysaml2 / everything else | — | unchanged (django tracks main: 5.2.16) |

   ("Before" reflects the lock as of the RFC's first draft. Main has since
   drifted *worse*: the 2026-08-04 merge of dependabot #692 forced the honest
   pipenv relock described in §3, shipping the predicted cryptography
   **43.0.3 downgrade** to main — plus a duplicate distribution, with both
   `grafana-django-saml2-auth` and `django-saml2-auth-community` 3.21.0 in
   `Pipfile.lock` installing the same `django_saml2_auth` module path. This
   PR's relock supersedes both artifacts.)

## 3. Why uv and not something smaller

Every smaller option was investigated and rejected on evidence:

| Alternative | Why not |
|---|---|
| **Merge dependabot's pyjwt PR** | Impossible — that's the bug. `security_update_not_possible`: resolution can't pass 2.12.1 while the grafana shim pins the chain. |
| **Swap to community 3.22.0 under pipenv, honest relock** | Tested. The honest relock **downgrades cryptography 48.0.1 → 43.0.3** (pyopenssl 24.2.1's `<44` cap finally enforced), which carries two HIGH advisories. Strictly worse than today. |
| **Keep hand-editing Pipfile.lock** (the accidental status quo, made deliberate) | pipenv has **no override mechanism** and `pipenv sync --deploy` only hash-checks the Pipfile — but every honest relock (any `pipenv lock/update/install`, any dependabot pip PR) silently reverts the splice. An invariant enforced by nobody, which is exactly how we got the unsound lock we have. |
| **pip/pip-tools with `--no-deps`** | pip 26 hard-errors on the honest resolution (`ResolutionImpossible`), `pip check` fails CI, and no override support exists ([pip#8076](https://github.com/pypa/pip/issues/8076) unimplemented). |
| **Poetry** | No override capability; the request ([poetry#697](https://github.com/python-poetry/poetry/issues/697)) was closed not-planned. |
| **Git-pin a patched pysaml2 fork** (e.g. production-proven `peppelinux/pysaml2@pplnx-v7.5.4-1`) | Works and resolves cleanly, but introduces a git dependency into a healthcare app's auth stack that dependabot cannot version-manage, pinned to a personal fork. Kept as fallback if the override ever misbehaves. |
| **Drop SAML entirely** | Removes pysaml2/pyopenssl and the whole problem class. Deliberately **out of scope here** — it's a product decision (see §5), and this PR works whether or not that conversation happens. |
| **Wait for upstream** | pysaml2's maintainer has been unreachable for ~6 months; the community re-group is two weeks old. Indefinite timeline against a wedged security pipeline. |

Why the override is safe (empirically verified, not assumed):

- We built the actual pairings in scratch environments. pysaml2 7.5.4 imports
  and operates correctly under pyopenssl 26.x + cryptography 48/49. A standard
  SP doing Redirect/POST SSO with xmlsec1 signature verification **executes no
  pyOpenSSL code at all beyond the module import** — signature checking is an
  xmlsec1 subprocess, not pyOpenSSL.
- The two degraded corners both sit behind config we don't use:
  `validate_certificate=True` (cert-chain re-verification; fails **closed** —
  reports invalid — under pyopenssl ≥25) and PEFIM `generate_cert_info`
  (CSR generation APIs removed in pyopenssl 26.3). Neither is set in
  `jhe/settings.py`, and this RFC documents that they must not be enabled
  while the override exists.
- dependabot's uv updater literally shells out to `uv lock`, so **the override
  is respected on every future bot PR** — it survives relocks by construction
  (uv security updates GA'd 2025-12-16).
- Production precedent: [Flagsmith ships this exact override](https://github.com/Flagsmith/flagsmith/blob/main/api/pyproject.toml)
  (pysaml2 + `override-dependencies` past the pyopenssl cap) in their
  production SAML stack.

## 4. Pros / cons

**Pros**
- Every wedged security update unblocks: pyjwt 2.13.0, cryptography 49.0.0,
  pyopenssl 26.3.0 — via honest resolution, killing the recurring red
  dependabot job and the deploy-gate flakiness it caused.
- The lockfile becomes *sound* for the first time in a long while; "what's
  actually installed" and "what the resolver believes" agree again.
- One dependency toolchain (uv) across the repo instead of two (pipenv +
  uv); dependabot config becomes uniform; `uv sync --frozen` is faster than
  `pipenv install` in CI.
- The workaround is one grep-able line with a documented retirement trigger,
  instead of an undocumented unsound lock.

**Cons / accepted risks**
- An override is still a workaround: we are asserting, against pysaml2's
  metadata, that pyopenssl ≥26.2 is compatible. Evidence says yes (§3), but
  if pysaml2 ever *adds* new pyOpenSSL API usage in a patch release, the
  override could mask a real incompatibility. Mitigation: pysaml2 is frozen
  (that's the whole problem), and the override pins nothing — a future
  pysaml2 release that drops pyOpenSSL makes it a no-op to delete.
- `validate_certificate` and PEFIM cert generation must stay off while the
  override exists (they are off; both degrade fail-closed, not fail-open).
- Contributors need uv installed (`brew install uv` / the standalone
  installer); pipenv muscle memory (`pipenv shell`, `pipenv run`) becomes
  `uv run` / `.venv` activation. README updated.
- The dev-database bootstrap and deployment flow are otherwise unchanged, but
  any external tooling that parsed `Pipfile.lock` (none known in-repo) breaks.

## 5. Known issue explicitly out of scope (flagged for @travis-sauer-oltech)

While auditing SAML reachability we found a **pre-existing** gap, untouched by
this PR: the production Docker image never installs the `xmlsec1` binary, but
`jhe/settings.py` sets `AUTHN_REQUESTS_SIGNED`/`SIGN_REQUEST = not DEBUG`. A
production SAML login against the current image would therefore fail at
request signing — independent of anything here. Combined with the seed
default `auth.sso.saml2=0`, this means SAML has likely never been operable in
a deployed environment. Whether the right fix is adding `xmlsec1` to the
image, gating SAML wiring behind the setting, or a larger conversation about
the SAML stack's future (e.g. `django-allauth[saml]`, which JHE's existing
allauth dependency supports and which avoids pysaml2 entirely), we'd like
Travis's read — as the author of the SAML integration — in review comments or
a follow-up issue. **This PR changes no SAML behavior**: same package code
(renamed distribution), same import paths, same settings, same URLs.

Live validation (2026-07-31, inside the running `jhe` fly.io machine):
`shutil.which("xmlsec1")` → `None` — the deployed container cannot sign SAML
requests. The deployed database's settings rows: `auth.sso.saml2 = 0`
(disabled), `auth.sso.idp_metadata_url` empty, `auth.sso.valid_domains`
empty — SAML is off **and no IdP has ever been configured**. Consistently,
the public login page renders no SAML button. So "never operable in a
deployed environment" is observed fact, not inference: disabled,
unconfigured, and unsignable. (App logs can neither confirm nor refute
usage: gunicorn does not access-log requests and fly's CLI log buffer holds
only seconds of output — itself worth knowing.)

**Resolution (2026-08-04):** @s1monj answered the direction question in
review: allauth is now the patient login/password-reset stack, so SAML
should ideally live there too — the grafana package predates JHE's allauth
adoption and was chosen only as the lighter option at the time. Accordingly:

- The `django-saml2-auth-community` swap in this PR is **transitional**. The
  end-state is `django-allauth[saml]`
  (`allauth.socialaccount.providers.saml`), which drops the pysaml2/pyopenssl
  chain entirely and retires the override via §6 trigger 3.
- The migration is tracked in a follow-up issue (greenfield per the live
  validation above — nothing deployed to preserve). Design input from
  @travis-sauer-oltech as the current integration's author is requested
  there rather than blocking this PR.
- One gap carries over rather than disappearing: allauth's SAML provider is
  backed by `python3-saml`, whose `xmlsec` dependency needs the `libxmlsec1`
  system libraries — so the missing-`xmlsec1` Docker gap above must be fixed
  as part of that migration, not this PR.

## 6. Retirement triggers (when to delete the override)

Delete `[tool.uv] override-dependencies` and this RFC's workaround section
when **any** of:

1. pysaml2 releases with [#977](https://github.com/IdentityPython/pysaml2/pull/977)
   (pyOpenSSL removed) or [#1021](https://github.com/IdentityPython/pysaml2/pull/1021)
   (minimal port + unpinned) merged — watch the IdentityPython re-grouping
   ([IdentityPython/Meetings](https://github.com/IdentityPython/Meetings)).
2. `django-saml2-auth-community` moves to a pysaml2 release without the cap.
3. JHE migrates off pysaml2 — per §5's resolution this is now the planned
   path (allauth SAML provider, tracked in the follow-up issue).

The override line in `pyproject.toml` carries a comment pointing here.

## 7. How this was researched (provenance, per RFC 0001's process)

This change was developed with Claude (Claude Code), with the human decisions
at each fork recorded here. The initial symptom was red GitHub Actions; the
diagnosis and design went through: (1) log analysis of the failing dependabot
security jobs; (2) a first naive fix attempt (pipenv relock after the package
rename) that was **caught in review of the lock diff** downgrading
cryptography, and rejected; (3) five parallel research investigations —
pysaml2 upstream state, django-saml2-auth-community upstream state, JHE's
actual SAML/pyjwt usage and exploitability, override tooling across
pipenv/uv/pip/poetry, and an empirical compatibility matrix of the
"forbidden" version pairings in scratch venvs; (4) the human decisions: do
not accept the cryptography downgrade; do not hand-splice the lock; prefer
the declarative override; keep SAML behavior unchanged and hand the
xmlsec1/product question to its author; write this RFC for review by @s1monj
rather than merging directly. The full transcripts and the reproduction
script for the compatibility matrix exist and can be shared.
