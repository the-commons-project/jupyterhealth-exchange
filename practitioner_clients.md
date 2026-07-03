# Practitioner Clients

## Overview

A **PractitionerClient** lets a practitioner generate their own OAuth
**client-credentials** application from the UI and obtain an **API Key** they can use
for machine-to-machine access to JHE. It is the practitioner-facing counterpart to
[`JheClient`](core/models/jhe_client.py), which augments a *patient* end-user client.

Both models wrap a Django OAuth Toolkit (DOT) `Application` with JHE-specific data:

| Model               | End user      | DOT grant type        | DOT client type | Extra data                       |
| ------------------- | ------------- | --------------------- | --------------- | -------------------------------- |
| `JheClient`         | Patient       | `authorization-code`  | `public`        | `invitation_url`, `aux_data`     |
| `PractitionerClient`| Practitioner  | `client-credentials`  | `confidential`  | `practitioner` (FK), `label`     |

The **API Key** returned to the practitioner is:

```
base64(client_id ":" client_secret)
```

i.e. an HTTP Basic credential the practitioner presents to the OAuth token endpoint to
obtain an access token via the client-credentials grant.

> **Out of scope for this initial implementation:** the UI, and any permission-check
> changes needed so a client-credentials access token can call the rest of the API. This
> change only adds the model, the management API endpoint, and the migration.

## Model

[`core/models/practitioner_client.py`](core/models/practitioner_client.py)

```python
class PractitionerClient(models.Model):
    application  = OneToOneField(OAUTH2_PROVIDER_APPLICATION_MODEL, on_delete=CASCADE,
                                 related_name="practitioner_client")
    practitioner = ForeignKey("Practitioner", on_delete=CASCADE, related_name="clients")
    label        = TextField(blank=True, default="")
```

* `application` — one-to-one link to the DOT `Application`. `CASCADE` means deleting
  either side removes the other (the delete endpoint deletes the `Application`, which
  cascades to the `PractitionerClient`).
* `practitioner` — the owning practitioner. A practitioner may have many clients.
* `label` — optional free-text label chosen by the practitioner.

Migration: [`core/migrations/0038_practitionerclient.py`](core/migrations/0038_practitionerclient.py).

## The client secret is stored unhashed

By default DOT **hashes** `client_secret` on save (`hash_client_secret=True`), so the
plaintext is only available once, at creation. Because the API contract requires the API
Key (`base64(client_id:client_secret)`) to be returned on **every** read — LIST and READ,
not just CREATE — these applications are created with **`hash_client_secret=False`** so the
secret remains readable.

This is the standard "viewable API key" trade-off: the secret sits in the database in
plaintext (weaker than a hash), in exchange for being able to display it again later. It
applies only to practitioner-client applications; all other JHE clients keep DOT's default
hashing.

## No automatic JheClient

`JheClient` (the patient-client wrapper) is created **explicitly** wherever a patient
client is made — there is no `post_save` signal that auto-creates one for every
`Application`. A `PractitionerClient` is therefore the only JHE wrapper a
client-credentials application ever gets; creating one never produces a stray `JheClient`.

## Admin Clients endpoint

The existing admin Clients API ([`ClientViewSet`](core/views/client.py), `/api/v1/clients`)
manages patient clients, which are exactly the Applications wrapped by a `JheClient`. Its
queryset filters on that relation (`Application.objects.filter(jhe_client__isnull=False)`),
which naturally excludes both the JHE Portal client and practitioner clients (neither has a
`JheClient`) without matching on names or grant types. Practitioner clients are managed
exclusively through the `PractitionerClient` endpoint below.

## API

Registered in [`core/urls.py`](core/urls.py) as a `ModelViewSet` at:

```
/api/v1/practitioner_clients
```

Implementation: [`core/views/practitioner_client.py`](core/views/practitioner_client.py),
serializer [`core/serializers/practitioner_client.py`](core/serializers/practitioner_client.py).

### Authorization

The single rule for every endpoint is **self-operation by a practitioner**:

* The caller (identified by the Bearer access token → `request.user`) must be a
  practitioner. Otherwise `403 Forbidden` with
  `"Only practitioner users can manage practitioner clients."` (the `IsPractitioner`
  permission class). Unauthenticated requests get `401`.
* The viewset queryset is filtered to `practitioner == request.user.practitioner`, so a
  practitioner can only LIST/READ/UPDATE/DELETE **their own** clients. Acting on another
  practitioner's client returns `404` (it is simply not in their queryset).

The practitioner ID is always derived from the token's user — never from the request body.

### Representation

LIST, READ, CREATE and UPDATE all return the same shape:

```json
{
  "id": 12,
  "label": "my laptop",
  "created": "2026-06-16T14:55:00Z",
  "name": "_practitioner_client_3_20260616145500",
  "key": "MzdhYmM...OmZvbw=="
}
```

| Field     | Source                                            |
| --------- | ------------------------------------------------- |
| `id`      | `PractitionerClient.id`                           |
| `label`   | `PractitionerClient.label`                        |
| `created` | `Application.created`                             |
| `name`    | `Application.name`                                |
| `key`     | `base64(Application.client_id:Application.client_secret)` |

### CREATE — `POST /api/v1/practitioner_clients`

Body: `{ "label": "<optional string>" }`

1. Resolve the practitioner from the token user (403 if not a practitioner).
2. Create a DOT `Application` with:
   * `user` = token user
   * `name` = `_practitioner_client_<practitioner_id>_<YYYYMMDDHHMMSS>`
   * `client_type` = `confidential`
   * `authorization_grant_type` = `client-credentials`
   * `hash_client_secret` = `False`
   * `skip_authorization` = `True`
   * `client_id` (16 chars) / `client_secret` (32 chars) — generated from
     `ascii_letters + digits` (see `_CLIENT_ID_LENGTH` / `_CLIENT_SECRET_LENGTH` in the view).
     These are deliberately shorter than DOT's defaults (40-char id, 128-char secret) so the
     base64 API key stays workable; a 32-char secret over a 62-char alphabet is still ~190
     bits of entropy. The alphabet excludes `:` so it can't clash with the `client_id:client_secret`
     separator used to build the key.
3. Create the `PractitionerClient` linking the application, the practitioner and the label.
4. Return `201` with the representation above (including the API `key`).

### UPDATE — `PATCH /api/v1/practitioner_clients/{id}`

Only `label` is mutable. The serializer exposes `label` as the sole writable field, so any
other keys in the body (e.g. `name`, `key`) are ignored. `PUT` is disabled (`405`).

### DELETE — `DELETE /api/v1/practitioner_clients/{id}`

Deletes the underlying DOT `Application`; the one-to-one `CASCADE` removes the
`PractitionerClient` row with it.

### LIST / READ — `GET /api/v1/practitioner_clients[/{id}]`

Returns the caller's own clients (list) or a single own client (detail), each including the
API `key`.

## Tests

[`tests/backend/test_practitioner_client_viewset.py`](tests/backend/test_practitioner_client_viewset.py)
covers: create returns a decodable `base64(client_id:client_secret)` key and links the
practitioner; label optional; no `JheClient` is created for the new app; patient → 403 and
anonymous → 401; LIST/READ scoping to own clients (others → 404); update restricted to
`label`; `PUT` → 405; delete removes both the `PractitionerClient` and the `Application`;
and that one practitioner cannot read or delete another's client.
