# RFC 0003: SAML SSO on allauth — one auth stack, no vendored ACS, no override

- **Status:** Discussion — becomes Accepted when a maintainer approves
  companion PR #697; the approving review + merge commit are the acceptance
  record, and this line flips to Accepted in the merge. (Where RFC docs live
  vs. the `02_rfc.yml` issue flow is an open question posed to maintainers in
  RFC 0001, #677.)
- **Companion PR:** #697 (stacked on #679 / RFC 0002)
- **Primary reviewers:** @s1monj (architecture), @travis-sauer-oltech (SAML behavior — author of the integration this replaces)
- **Follows:** RFC 0002 §5's resolution; provenance per RFC 0001 (#677, in review)

**TL;DR:** Practitioner SAML SSO moves from the vendored django_saml2_auth
integration to allauth's built-in SAML provider (`django-allauth[saml]`) —
the stack that already runs patient login and password reset. ~250 lines of
vendored auth code are deleted, the pysaml2/pyopenssl chain disappears
(retiring RFC 0002's resolver override, §6 trigger 3), and the
missing-`xmlsec1` deployment gap closes (§4). Behavior is greenfield: SAML
has never been operable in any deployment — verified live on fly.io
2026-07-31: no `xmlsec1` binary in the image, `auth.sso.saml2 = 0`, no IdP
metadata ever configured (RFC 0002 §5) — so there is nothing to preserve.

## 1. Why

Simon's review direction on RFC 0002 (2026-08-04):

> We're now using allauth for the patient login and password reset flows, so
> if we can use that for SAML too that would be ideal. At the time I chose the
> grafana saml because we weren't using allauth and it was a lighter option

Beyond consistency: we stop owning ~250 lines of security-sensitive vendored
code (a hand-maintained ACS view and user-creation logic), and the override
is deleted rather than monitored indefinitely.

## 2. The change

| | before (#679) | after (#697) |
|---|---|---|
| Package | django-saml2-auth-community (+ pysaml2, pyopenssl) | `django-allauth[saml]` (+ python3-saml, xmlsec) |
| ACS endpoint | vendored `acs()` at `/sso/acs/` | allauth's at `/allauth/saml/<org>/acs/` |
| IdP config | `auth.sso.idp_metadata_url` JheSetting + trigger hook | `SocialApp` row (provider `saml`) in Django admin |
| User provisioning | vendored `get_or_create_user()` | `JheSocialAccountAdapter` (~30 lines) |
| Login button | `auth.sso.saml2` JheSetting | flag **and** exactly one saml SocialApp (else the button hides instead of 500ing the login page) |
| Domain restriction | `auth.sso.valid_domains` seeded but **never read** | enforced only when SAML would create a brand-new account — existing accounts (matched by email or previously linked) never pass through it (§2); revoke = deactivate the user |
| Resolver override | `pyopenssl>=26.2` required | deleted |

Provisioning (for Travis's review): every IdP-asserted user is created as a
**practitioner** (`JheUser.save()` auto-creates the profile and applies
`auth.default_orgs`), `identifier` = the SAML uid (a mapped attribute, or
NameID as fallback — §3 requires a stable mapping) and `email_is_verified` is
set. No asserted email → signup fails closed ("Sign Up Closed", never the
type-any-email form). `SOCIALACCOUNT_EMAIL_VERIFICATION = "none"` (the IdP
asserts the email); `SOCIALACCOUNT_LOGIN_ON_GET = True` (one-click from our
own button).

**Existing accounts:** email-authentication logs a practitioner with an
existing password account straight in via SAML and links the SocialAccount —
without it, allauth's enumeration-safe default dead-ends every existing
same-email user on a fake "verification e-mail sent" page. It is enabled
**per-IdP** by `"email_authentication": true` in that SocialApp's settings
JSON (the global `SOCIALACCOUNT_EMAIL_AUTHENTICATION` stays at allauth's
default False); the matched email must additionally count as verified, which
happens when the IdP asserts the mapped `email_verified` attribute or the
SocialApp declares `"verified_email": true` (§3). Two adapter guards ride
this path (`core/adapters.py`): existing **patient** accounts are rejected
(SAML is a practitioner entrance), and a verified allauth `EmailAddress` row
is backfilled at link time — without it, allauth's anti-pre-registration
guard would wipe the practitioner's password, since JHE's own signup never
creates those rows. Corollary: matched existing accounts never pass through
`is_open_for_signup`, so the `auth.sso.valid_domains` gate applies only to
users JHE has never seen — after the first rollout, the minority. (Also
fixed en route: `ACCOUNT_USER_MODEL_USERNAME_FIELD = None`, required for
JHE's username-less user model on allauth's social save path.)

Alternatives rejected: staying on the community fork (permanent second auth
stack + vendored code + override forever; Simon's direction was explicit);
dropping SAML (a product decision nobody made — allauth keeps it at near-zero
cost); IdP config in JheSettings via a custom `get_app` (more glue for a
setting never once populated; SocialApp admin is the native pattern).

## 3. Operational runbook (first real IdP)

1. Django admin → Social applications → add: provider `saml`, a name,
   `client_id` = the URL slug (e.g. `berkeley`). IdP metadata URL, attribute
   mapping, and SP cert/key go in the settings JSON
   ([allauth SAML docs](https://docs.allauth.org/en/latest/socialaccount/providers/saml.html)):

   ```json
   {
     "idp": {"metadata_url": "https://idp.example.edu/metadata"},
     "verified_email": true,
     "email_authentication": true,
     "advanced": {
       "private_key": "<PEM key>",
       "x509cert": "<PEM cert>",
       "authn_request_signed": true,
       "want_assertion_signed": true
     }
   }
   ```

   - `"verified_email": true` — trust the IdP's email assertions;
     `"email_authentication": true` — let those emails log existing password
     accounts in (§2). Set both only for IdPs that verify mailbox ownership.
   - The `advanced` signing flags **default to off** in allauth. Even
     without them python3-saml (strict mode, allauth's default) rejects any
     response carrying no IdP signature and validates whichever signature is
     present; the flags close the response-signed-but-assertion-unsigned gap
     and sign our AuthnRequests (which requires the key/cert pair — the cert
     is published in our SP metadata for the IdP to pin). Leave
     `reject_idp_initiated_sso` at its default (true).
   - Require a **stable uid**: the IdP must assert
     `urn:oasis:names:tc:SAML:attribute:subject-id` (allauth's default uid
     mapping) or the `attribute_mapping` must map `uid` to a persistent
     attribute (eduPersonPrincipalName, employeeID). Otherwise allauth falls
     back to NameID — a *transient* NameID mints a junk SocialAccount every
     login and a junk `JheUser.identifier`. Verify at onboarding: log in
     twice, confirm the user still has exactly one SocialAccount.
2. Give the IdP our SP metadata: `/allauth/saml/<slug>/metadata/`.
3. Optionally set `auth.sso.valid_domains` (comma-separated).
4. Flip `auth.sso.saml2` to `1`. The button renders only with exactly one
   saml SocialApp; multiple IdPs need per-IdP buttons in a customized login
   template.

(The retired `auth.sso.idp_metadata_url` JheSetting is deleted from existing
databases by migration `0043` — no operator action.)

### 3.1 Back-out

1. Flip `auth.sso.saml2` to `0` — this only hides the login button; the
   `/allauth/saml/<slug>/*` endpoints stay mounted.
2. Delete the saml SocialApp row — the real kill switch: every SAML endpoint
   for that slug then 404s.
3. If backing out permanently, also delete `SocialAccount` rows with
   provider `saml`; they are inert without the SocialApp, but a future
   re-enable would silently re-link users under the old IdP's uids.
4. Passwords are unaffected: the adapter's EmailAddress backfill (§2)
   prevents allauth's email-authentication password wipe, and rejected
   patients are turned away before any account mutation. Should a user ever
   end up with an unusable password anyway, recovery is allauth's reset at
   `/allauth/password/reset/` — the login page's "Forgot Password"
   (Django's `PasswordResetForm`) silently skips unusable-password users.

## 4. xmlsec: the RFC 0002 §5 gap closes

python3-saml signs in-process through the `xmlsec` Python binding, whose
wheels bundle libxmlsec1/libxml2 — no `xmlsec1` binary needed. Verified in
the built image: xmlsec and lxml both link libxml2 2.14.6 (no
version-mismatch failure mode) and `OneLogin_Saml2_Auth` imports cleanly.
**No Dockerfile changes.**

## 5. Cons / accepted risks

- allauth's socialaccount app brings its own DB tables (migrations
  auto-apply on deploy).
- `SOCIALACCOUNT_LOGIN_ON_GET = True` is a login-CSRF surface, but the
  harmful variant — logging a victim into an *attacker's* account — is
  blocked by allauth regardless: SP-initiated responses must carry an
  `InResponseTo` matching a state stashed in the victim's own session
  (unknown state → `PermissionDenied`), and unsolicited IdP-initiated
  assertions are rejected by default (`reject_idp_initiated_sso`, which must
  stay default — §3). The residual is a third-party page force-starting the
  victim's *own* login; we accept that for a practitioner portal in exchange
  for one-click SSO. The interstitial confirm page (allauth's default) would
  only add a click guarding that drive-by self-login.
- Email authentication makes the per-IdP `"email_authentication"` +
  `"verified_email"` declarations security-relevant: only set them for IdPs
  that verify mailbox ownership. Note the default attribute mapping also
  honors an IdP-asserted `email_verified` attribute
  (`http://schemas.auth0.com/email_verified`) as proof of verification — a
  configured IdP asserting it gets verified-email treatment without the
  `"verified_email"` declaration; override `attribute_mapping` if that
  should not be honored.
- SP-side signing flags default to off in allauth — mitigated by the §3
  runbook's required `advanced` block.
- Same-email existing accounts skip the signup gate entirely (§2) — patient
  accounts are rejected by the adapter, and allauth's email-authentication
  password wipe (fires on any matched account lacking a verified allauth
  `EmailAddress` row) is neutralized by the adapter's backfill; both are
  pinned by ACS integration tests.
- python3-saml/xmlsec is a heavier binary dependency than pysaml2 — but it's
  the maintained, allauth-blessed path, and it makes signing actually work.

## 6. Provenance (per RFC 0001's process, #677)

Developed with Claude (Claude Code) on 2026-08-04, immediately after Simon's
direction. The django_saml2_auth surface was fully audited before any edit;
adapter APIs were verified against installed allauth 65.18 by introspection
(one speculative override was deleted when the hypothesized exception path
turned out not to exist); an earlier "the xmlsec gap carries over" claim was
disproven by building the production image and corrected. Three independent
adversarial review passes (correctness/security, DRY/KISS, tests/docs) ran
before review; their confirmed findings — the existing-account dead-end, the
login-page 500 on a flag/SocialApp mismatch, and the signup-only scope of
the domain gate — are fixed and reflected above. Verification: 988 backend
tests (11 new), repo-pinned pre-commit, Docker image build + in-container
import check.
