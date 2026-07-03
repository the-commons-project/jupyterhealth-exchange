# Email one-time-code login for patient-access OAuth clients

## Overview

OAuth auth-code clients (DOT applications) whose `client_id` is listed in the
**`auth.patient_access_clients`** JheSetting log in via the django-allauth
**email + one-time-code** ("magic code") flow instead of the standard
`/accounts/login/` password form. This is intended for patient-facing clients,
where patients authenticate by entering their email, receiving a code, and
entering it — no password.

Every other OAuth client (and direct portal login) is unaffected and continues
to use the password form.

### How it works

1. A client hits `/o/authorize/?client_id=…` → DOT bounces the unauthenticated
   user to `/accounts/login/?next=<authorize URL>`.
2. `LoginView.get()` parses `client_id` out of `next`; if it is in
   `auth.patient_access_clients`, it 302s to
   `/accounts/login-otp/?next=<same URL>`.
3. The patient enters their email → allauth emails a code → the patient enters
   the code → `perform_login` redirects back to the original authorize URL →
   **OAuth resumes and the auth code is delivered to the registered callback.**
   Unlisted clients fall through to the password form.

### Key behaviours

- **Existing accounts only.** An unknown email gets allauth's enumeration-safe
  "no account" email and cannot log in; the flow never creates accounts.
  Patients already pre-exist as `JheUser`s (the invitation flow requires
  `patient.jhe_user`), so this is compatible with how patients are provisioned.
- **Email-verification bridge.** Patients are created outside allauth and have no
  allauth `EmailAddress` row, which would otherwise stall login at a
  confirm-email screen under `ACCOUNT_EMAIL_VERIFICATION="mandatory"`. The custom
  OTP view creates an unverified `EmailAddress` when the code is requested;
  entering the emailed code then verifies it and completes login. See the
  detailed write-up below.

### Relevant code

- `LoginView` routing + `JheRequestLoginCodeView` — [core/views/common.py](core/views/common.py)
- URL `accounts/login-otp/` → `login-otp` — [core/urls.py](core/urls.py#L56)
- `ACCOUNT_LOGIN_BY_CODE_ENABLED = True` (+ resend) — [jhe/settings.py](jhe/settings.py#L101)
- Setting seeded as an empty JSON array — [core/management/commands/seed.py](core/management/commands/seed.py#L100)
- Tests — [tests/backend/test_login_otp.py](tests/backend/test_login_otp.py)

---

## Configuring a new OAuth client to use the code flow

### Step 1 — Find the client's `client_id`

This is the DOT `Application.client_id` (not the JHE `Client` primary key). You
can read it from:

- Django admin → **Applications** (`/admin/oauth2_provider/application/`), or
- the JHE admin UI clients list, or
- the database: `Application.objects.values("name", "client_id")`.

### Step 2 — Add the `client_id` to `auth.patient_access_clients`

The setting is a JSON array of `client_id` strings (seeded empty: `[]`). Use any
one of the following.

**a) Django admin**

1. Go to `/admin/` → **Jhe settings**.
2. Open the row with key `auth.patient_access_clients` (create it if missing).
3. Set **value type** = `json` and put the array in the **value json** field, e.g.
   ```json
   ["abc123theclientid"]
   ```
4. Save.

**b) API (superuser only)**

`POST`/`PUT`/`PATCH` to the `jhe_settings` endpoint with the typed payload:

```http
PATCH /api/v1/jhe_settings/<id>
Content-Type: application/json

{
  "key": "auth.patient_access_clients",
  "value_type": "json",
  "value": ["abc123theclientid"]
}
```

**c) Django shell**

```python
from core.models import JheSetting

s, _ = JheSetting.objects.update_or_create(
    key="auth.patient_access_clients",
    defaults={"value_type": "json"},
)
s.set_value("json", ["abc123theclientid"])
s.save()
```

To enable multiple clients, include all their `client_id`s in the array.

### Step 3 — Wait for the cache (or restart)

`get_setting()` caches values for 60 seconds. Allow up to a minute for the change
to take effect (or restart the app).

### Step 4 — Ensure the patient account exists

The flow only logs in **existing** `JheUser`s. Make sure the patient has been
provisioned (e.g. via the invitation flow) with the email they will enter. An
unknown email receives an enumeration-safe "no account" email and cannot proceed.

### Step 5 — Verify

Visit the client's authorize URL while logged out:

```
/o/authorize/?client_id=abc123theclientid&response_type=code&redirect_uri=…&scope=openid+email
```

You should be redirected to `/accounts/login-otp/` (the email-code page) rather
than the password form. Enter the patient's email, retrieve the code (in local
dev the email backend is the console backend, so the code prints to the server
log), enter it, and confirm the browser lands on the client's callback with an
auth code.

---

## Detail: the "Email-verification bridge"

Here's the detailed picture.

### What "mandatory" verification actually gates

`ACCOUNT_EMAIL_VERIFICATION = "mandatory"` ([jhe/settings.py:100](jhe/settings.py#L100)) isn't a one-off check at login — allauth runs every login through an ordered set of **login stages**, and one of them is `EmailVerificationStage`. Its `handle()` ([stages.py:161-166](https://github.com/pennersr/django-allauth/blob/main/allauth/account/stages.py#L161-L166)) does, for the mandatory case:

```python
elif email_verification == EmailVerificationMethod.MANDATORY:
    if not has_verified_email(login.user, login.email):
        send_verification_email_at_login(self.request, login)
        response = get_adapter().respond_email_verification_sent(...)  # 302 -> /allauth/confirm-email/
```

When it returns a `response`, the login machinery short-circuits — the user is **not** actually logged in, they're parked on the confirm-email page. That 302 to `/allauth/confirm-email/` is exactly what the test hit before the fix.

The critical detail: `has_verified_email()` only consults **allauth's `EmailAddress` table** (`EmailAddress.objects.filter(user_id=..., verified=True)`). It does **not** look at `JheUser.email` or JHE's own `JheUser.email_is_verified` field. Those are invisible to allauth.

### Why patients always failed that gate

JHE patients are never created through allauth — they come from `JheUser.objects.create_user(...)` and the invitation flow ([patient_invitation.py](core/models/patient_invitation.py)), neither of which touches `EmailAddress`. So a patient has **zero `EmailAddress` rows**, `has_verified_email` is always `False`, and the mandatory stage blocks them every time.

### Why login-by-code didn't already solve it

allauth's code flow *intends* for "entering the emailed code" to count as verifying the email. `LoginCodeVerificationProcess.finish()` calls `verify_email_indirectly(...)` **before** `perform_login` ([login_by_code.py:42-43](https://github.com/pennersr/django-allauth/blob/main/allauth/account/internal/flows/login_by_code.py#L42-L43)). But look at what that function does ([email_verification.py:24-34](https://github.com/pennersr/django-allauth/blob/main/allauth/account/internal/flows/email_verification.py#L24-L34)):

```python
def verify_email_indirectly(request, user, email):
    try:
        email_address = EmailAddress.objects.get_for_user(user, email)
    except EmailAddress.DoesNotExist:
        return False        # <-- no row: silently does nothing
    else:
        if not email_address.verified:
            return verify_email(request, email_address)
        return True
```

It only *flips an existing row* to verified. With no row, it's a no-op, the code-entry never marks anything verified, and the downstream mandatory stage still blocks. So login-by-code quietly assumes the EmailAddress row already exists — which is true for allauth-native users but never for JHE patients.

### The bridge

`JheRequestLoginCodeView.form_valid` ([core/views/common.py](core/views/common.py)) creates the missing row, unverified, at the moment the patient *requests* a code:

```python
EmailAddress.objects.get_or_create(
    user=user, email=email,
    defaults={"verified": False, "primary": True},
)
```

Now the rest of allauth's intended flow works on its own:
1. Code is emailed; patient enters it.
2. `finish()` → `verify_email_indirectly` **finds** the row → `verify_email()` sets `verified=True`, makes it primary, saves, fires the `email_confirmed` signal ([email_verification.py:47+](https://github.com/pennersr/django-allauth/blob/main/allauth/account/internal/flows/email_verification.py#L47)).
3. `perform_login` → `EmailVerificationStage` now sees `has_verified_email == True` → passes → login completes → redirect to `next` (the OAuth authorize URL).

### Why unverified-at-creation, and why it's safe

- **Unverified, not verified:** I create it `verified=False` and let the code-confirm step flip it. That matches allauth's semantics — the address is only proven once the patient enters the code that was emailed to it. Creating it pre-verified would assert "this email is confirmed" before the user proved anything.
- **Not an auth bypass:** the row by itself grants nothing — login still requires the correct emailed code. Someone requesting a code for an email they don't control just creates an unverified row and causes a code email to the *real* owner; they can't read it, so they can't log in. allauth's enumeration protection and rate limiting still apply.

### Implications worth knowing

- Patients now accumulate allauth `EmailAddress` rows (verified after first code login). That's benign bookkeeping, but it means other allauth-email flows (e.g. password-reset-by-email) would now find a verified address for them — not used by patients today, just noting it.
- The `email_confirmed`/`email_added` signals fire on first verification. Nothing in JHE listens to them currently.

### Alternatives I rejected

- **Flip `ACCOUNT_EMAIL_VERIFICATION` to `optional`/`none`** — it's global, so it'd weaken verification for the password signup flow too.
- **Backfill `EmailAddress` rows for all patients** (migration or at patient creation) — broader change touching patient creation and the invitation flow; the per-view bridge is localized to the code-login path only.

One thing to flag for your call: this keeps `email_is_verified` (JHE's own field on `JheUser`) and allauth's `EmailAddress.verified` as **two separate notions of "verified."** The code flow updates allauth's, not JHE's. If anything in JHE relies on `JheUser.email_is_verified`, you may want me to also set it `True` when the code login verifies the address.
