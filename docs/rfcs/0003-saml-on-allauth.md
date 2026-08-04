# RFC 0003: SAML SSO on allauth — one auth stack, no vendored ACS, no override

- **Status:** Discussion
- **Companion PR:** #697 (stacked on #679 / RFC 0002)
- **Primary reviewers:** @s1monj (architecture), @travis-sauer-oltech (SAML behavior — author of the integration this replaces)
- **Follows:** RFC 0002 §5's resolution; provenance per RFC 0001's format

Bottom line up front: practitioner SAML SSO moves from the vendored
django_saml2_auth integration to **allauth's built-in SAML provider**
(`django-allauth[saml]`), the stack that already runs patient login and
password reset. ~250 lines of vendored auth code are deleted, the
pysaml2/pyopenssl dependency chain disappears — which **retires RFC 0002's
resolver override** (§6 trigger 3) — and the missing-`xmlsec1` deployment gap
(RFC 0002 §5) closes outright. SAML *behavior* is a greenfield: every
deployment has SAML disabled, unconfigured, and unsignable (validated live,
RFC 0002 §5), so there is no working IdP integration to preserve.

## 1. Why

Simon's review direction on RFC 0002 (2026-08-04):

> We're now using allauth for the patient login and password reset flows, so
> if we can use that for SAML too that would be ideal. At the time I chose the
> grafana saml because we weren't using allauth and it was a lighter option

The `django-saml2-auth-community` swap in #679 was always transitional — it
unwedged security updates without changing behavior. This RFC is the
end-state: one auth framework (allauth 65.x) for every login modality, with
SAML config managed the same way allauth manages everything else.

What we get beyond consistency:

- **Deletion of vendored auth-critical code.** The previous integration
  carried a hand-maintained copy of the library's ACS view (`acs()` in
  core/views/common.py) and user-creation logic (`get_or_create_user()` in
  core/utils.py) — 250 lines of security-sensitive code we owned. allauth's
  maintained flows replace both.
- **The override dies.** Dropping django-saml2-auth-community removes
  pysaml2/pyopenssl entirely, so `[tool.uv] override-dependencies` (RFC 0002
  §3) is deleted rather than monitored indefinitely.
- **The xmlsec1 gap closes** (§5 below) — production SAML request signing
  becomes possible for the first time, with zero Docker changes.

## 2. The change

| | before (#679) | after (#697) |
|---|---|---|
| Package | django-saml2-auth-community 3.22.0 (+ pysaml2, pyopenssl) | `django-allauth[saml]` (+ python3-saml, xmlsec, lxml) |
| ACS endpoint | vendored `acs()` at `/sso/acs/` | allauth's at `/allauth/saml/<org>/acs/` |
| IdP config | `auth.sso.idp_metadata_url` JheSetting → trigger hook | `SocialApp` row (provider `saml`) in Django admin |
| User provisioning | vendored `get_or_create_user()` | `JheSocialAccountAdapter` (~20 lines) |
| Login button gate | `auth.sso.saml2` JheSetting | unchanged |
| Domain restriction | `auth.sso.valid_domains` seeded but **never read** | enforced in the adapter |
| Resolver override | `pyopenssl>=26.2` required | deleted |

Provisioning semantics preserved from the old integration (for Travis's
review): every IdP-asserted user is created as a **practitioner** —
`JheUser.save()` then auto-creates the Practitioner profile and applies
`auth.default_orgs` — with `identifier` set from the SAML NameID/uid.
`SOCIALACCOUNT_EMAIL_VERIFICATION = "none"` because the IdP asserts the
email; `SOCIALACCOUNT_LOGIN_ON_GET = True` so our own login button is
one-click instead of bouncing through allauth's interstitial confirm page.

## 3. Alternatives considered

| Alternative | Why not |
|---|---|
| **Stay on django-saml2-auth-community** | Maintained, but permanently a second auth stack with vendored ACS/user code we own, plus the pysaml2 freeze means the override lives forever. Simon's direction was explicit. |
| **Drop SAML entirely** | A product decision nobody has made; allauth's provider keeps the capability at near-zero carrying cost (one adapter class + one template conditional). |
| **Keep IdP config in JheSettings** (custom `SocialAccountAdapter.get_app` reading `auth.sso.idp_metadata_url`) | More glue code to maintain, for a setting that has never been populated in any environment. allauth's SocialApp admin is the native, documented pattern; the two JheSettings that survive (`auth.sso.saml2`, `auth.sso.valid_domains`) are the ones with actual JHE-specific behavior. |

## 4. Operational runbook (first real IdP, whenever that happens)

1. Django admin → Social applications → add: provider `saml`, a name, and
   `client_id` = the URL slug (e.g. `berkeley`). Put IdP metadata URL,
   attribute mapping, and optional SP cert/key in the settings JSON
   ([allauth SAML docs](https://docs.allauth.org/en/latest/socialaccount/providers/saml.html)).
2. Give the IdP our SP metadata: `/allauth/saml/<slug>/metadata/`.
3. Optionally set `auth.sso.valid_domains` (comma-separated) to restrict
   signup domains.
4. Flip `auth.sso.saml2` to `1` — the login-page button appears. Order
   matters: the button's `provider_login_url` needs the SocialApp to exist.

## 5. xmlsec: the RFC 0002 §5 gap closes

RFC 0002 §5 documented that the deployed image has no `xmlsec1` binary, so
pysaml2-based request signing could never work in production. python3-saml
does not shell out to `xmlsec1` — it signs in-process through the `xmlsec`
Python binding, whose wheels bundle libxmlsec1 and libxml2. Verified in the
built image: `xmlsec` and `lxml` both link libxml2 2.14.6 (the known
version-mismatch failure mode is absent) and `OneLogin_Saml2_Auth` imports
cleanly. **No Dockerfile changes.**

## 6. Cons / accepted risks

- allauth's socialaccount app brings its own DB tables (its migrations
  auto-apply on deploy). Unused rows cost nothing.
- `SOCIALACCOUNT_LOGIN_ON_GET = True` allows login initiation via GET link;
  acceptable for an SSO redirect flow initiated from our own login page.
- python3-saml/xmlsec is a heavier binary dependency than pysaml2's pure
  Python — but it's the maintained, allauth-blessed path, and it's what makes
  signing actually work.
- Enabling the button flag before creating the SocialApp raises on the login
  page (documented ordering in §4; flag is seeded off).

## 7. How this was researched (provenance, per RFC 0001's process)

Developed with Claude (Claude Code) on 2026-08-04, immediately after Simon's
review direction. Process: (1) full audit of the django_saml2_auth surface
(three Python files, urls, template, seeds, tests) before any edit; (2) the
adapter API was verified against installed allauth 65.18 by introspection —
one speculative override (`on_authentication_error` messaging) was caught
and deleted when introspection showed the hypothesized exception path
doesn't exist; (3) the xmlsec claim in an earlier draft of RFC 0002 §5
("the gap carries over") was **disproven** by building the production image
and checking linked library versions, and corrected in both RFCs; (4) the
human decisions: allauth-native SocialApp config over JheSetting glue; keep
the `auth.sso.saml2` gate and landing-page behavior byte-compatible; ship as
a draft PR stacked on #679 for Travis's review rather than merging directly.
Verification: 983 backend tests (6 new), repo-pinned pre-commit, Docker
image build + in-container import check.
