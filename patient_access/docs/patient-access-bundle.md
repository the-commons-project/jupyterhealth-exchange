# The USCDI Patient Access bundle

What the USCDI Patient Access module delivers: one file per pull, written to the deployment's S3
bucket when a patient connects a healthcare organization and the pull completes. This document is
the file's specification. `patient_access/scripts/validate_patient_access_bundle.py` checks a file against it and
needs nothing but Python 3.11.

## 1. The object

| | |
|---|---|
| Key | `<prefix>/<start>-<job>/bundle.json`. The prefix is set per deployment (default `<patient id>/<FHIR source id>`); `<start>` is the pull's start in UTC as `YYYYMMDDTHHMMSSZ`; `<job>` is the first eight characters of the job id. Every pull gets its own key; nothing is ever overwritten. The newest key under a prefix is that patient's latest pull from that organization. |
| Content type | `application/fhir+json` |
| Encoding | UTF-8 containing only ASCII bytes: every non-ASCII character is a JSON `\uXXXX` escape. Strict JSON: no `NaN` or `Infinity` (a non-finite number the organization sent is `null`). |
| Size | The resources plus the documents, which the deployment caps (by default at most 10,000 documents, each under 2 GiB, 20 GiB in all). Readers should stream large files. |
| Encryption | Server-side, as the bucket is configured (SSE-S3, SSE-KMS or the bucket default). |

One object is one pull: one patient, one healthcare organization, one point in time. A patient
connected to three organizations has three prefixes. A repeat pull of the same organization is a
new object beside the earlier ones.

## 2. The Bundle

A FHIR R4 `Bundle` of type `document`.

| Field | Value |
|---|---|
| `id`, `identifier.value` | A UUID derived from the job id (`identifier.system` is `urn:ietf:rfc:3986`). |
| `type` | `document` |
| `timestamp` | When the bundle was assembled, ISO 8601 with offset. |
| `entry[0]` | The `Composition`, §3. |
| `entry[1]` | A `Device`: the software that wrote the file (`deviceName[0].name`, `version[0].value`). |
| `entry[2..]` | The organization's resources, grouped by type in the order of §3.2, then one `Binary` per delivered document, last (§5). |

Every entry has a `fullUrl` of the form `urn:uuid:<uuid>`, unique within the file, and a
`resource` with a `resourceType`. The UUID of a resource is derived from its type and id, so the
same resource gets the same `fullUrl` in every pull. A resource the organization sent without an
`id` keeps none; its `fullUrl` is derived from its position instead.

`Composition.meta.source`, when present, is the delivering system's own identifier for the
connection the pull belongs to (a URI it mints; not resolvable).

## 3. The Composition

| Field | Value |
|---|---|
| `status` | `final` |
| `type` | LOINC `60591-5`, Patient summary Document |
| `subject` | The patient: the `fullUrl` of the `Patient` entry when the pull holds one, else the organization's absolute URL for the patient. |
| `author[0]` | The `Device` entry's `fullUrl`. |
| `date` | As `Bundle.timestamp`. |
| `title` | `Records shared from <organization>` |
| `extension[0]` | The pull's provenance, §3.1. |
| `section[]` | One per record type, in a fixed order, then the documents section, §3.2 and §3.3. |

### 3.1 Provenance: the `patient-access-pull` extension

`Composition.extension[0].url` ends in `/patient-access-pull` (the base is the deployment's
extension namespace, by default `https://jupyterhealth.org/fhir/StructureDefinition/`). Its
`extension[]` children, each a nested extension of simple values (`valueString`, `valueInteger`,
`valueBoolean`, `valueDecimal`, `valueDateTime`); a value the pull does not have is omitted:

| Child | Fields |
|---|---|
| `job` | `id` (the pull's id), `created` (start), `finished` (assembly, same as `Bundle.timestamp`), `runs` (how many times a worker picked the pull up; more than 1 means it was resumed). |
| `participant` | `ref` (the delivering system's identifier for the patient), `provider` (how that identity was established), then the host attributes, one child each, repeated for a list. In this deployment: `patient_id`, `data_source_id`, `fhir_source_id` (integers), `fhir_source_uri` (string, also `meta.source`), `study_ids` (integer, repeated once per study the patient consented this connection to). |
| `source` | `id` (a stable, path-safe identifier for the organization), `vendor`, `brand`, `brandId`, `brandNpi`, `facility`, `facilityId`, `iss` (the FHIR server's base URL), `endpointId`, `customerId`, `customerName`, `ehrPatientId` (the patient's id at that organization), `authMethod`, `pkceAdvertised`, `typeChoice` (§6). |
| `scopes` | `requested` (repeated), `granted` (repeated, as the organization answered), `declined` (repeated: types the patient left unticked), `notGranted` (repeated: requested but withheld by the organization, or, when `typeChoice` is `ehr`, unticked on the organization's own screen). |
| `software` | `name`, `version`, `hostName`, `hostVersion`. |

### 3.2 Record sections

One section per record type the pull planned for, in this order, whether or not anything was
fetched:

| `code.text` | `title` |
|---|---|
| `Patient` | Demographics |
| `Condition` | Problems |
| `MedicationRequest` | Medications |
| `MedicationDispense` | Medication dispenses |
| `AllergyIntolerance` | Allergies and intolerances |
| `Immunization` | Immunizations |
| `Procedure` | Procedures |
| `Observation` | Results and vital signs |
| `DiagnosticReport` | Diagnostic reports |
| `DocumentReference` | Clinical notes |
| `Encounter` | Encounters |
| `CarePlan` | Care plans |
| `CareTeam` | Care teams |
| `Goal` | Goals |
| `ServiceRequest` | Service requests |
| `Device` | Devices |
| `QuestionnaireResponse` | Questionnaire responses |

A type the pull did not plan for but the organization returned anyway (through `_include`, for
instance) gets a section after these, in alphabetical order.

Each section holds:

- `entry[]`: a reference to every resource of that type in the bundle, by `fullUrl`, each once.
  The resources referenced are exactly the bundle's entries of that type.
- `extension[]`: one `patient-access-section-outcome` per query the pull ran for the type (two
  for `Observation`: laboratory results and vital signs), with `label`, `query` (the FHIR search
  relative to the server), `status` (§6), `count` (resources this query contributed, after
  duplicates across the type's queries were dropped), `pages`, `error` (the server's words, when
  it failed), `capped` (true when the page cap stopped paging), `seconds`. The sum of the
  queries' `count`s equals the number of entries in the section.
- When the section has no entries: `emptyReason` (§6) and `text`, a generated narrative. The
  narrative is written by the delivering software, not by the organization, and says so:
  `text.status` is `generated` and the sentence ends "This statement was generated by
  <host>; it is not a record from the healthcare organization." A section never has both
  `text` and `entry`.

### 3.3 The documents section

The last section, `code.text` `documents`, `title` "Document content". Its `entry[]` references
every `Binary` entry (§5). Its one extension, `patient-access-documents-outcome`, carries:

| Field | Meaning |
|---|---|
| `status`, `error` | The documents pass as a whole (§6). |
| `stored` | Documents in the bundle: the number of `Binary` entries. |
| `bytes` | Their total decoded size, as a decimal. |
| `seconds`, `capped` | Duration; whether a cap stopped the pass. |
| `skipped` (repeated) | `reason` (§6) and `count`: how many attempts ended for that reason. |
| `document` (repeated) | One per attempt, §4.2. |

When the section is empty it carries `emptyReason` and a generated narrative like any other.

## 4. The resources

Resources are as the organization returned them, with these changes:

- **References.** A `reference` to a resource that is in the bundle is rewritten to that entry's
  `fullUrl` (`urn:uuid:…`). A reference to the same server that is not in the bundle is made
  absolute (the server's base URL plus the relative reference) so it stays meaningful outside the
  server. References to other servers, `#`-local references and `urn:` references are untouched.
  No reference in the file is relative.
- **Numbers.** `NaN` and `Infinity` become `null`.
- **Attachments** (`DocumentReference.content[].attachment` and `DiagnosticReport.presentedForm[]`),
  §4.1.

### 4.1 Attachments and documents

A `DocumentReference` often offers one document in several formats; the pull takes the first that
could be fetched, in this order of preference: `text/plain`, `text/html`, `application/pdf`,
`text/rtf`, `application/xml`, then anything else. Every `presentedForm` of a `DiagnosticReport`
is a distinct document and each is taken.

In the delivered resource, each attachment is in one of three states:

| State | What the attachment looks like |
|---|---|
| Delivered | `url` is the `fullUrl` of a `Binary` entry; `contentType` and `size` describe the bytes as served (a `charset` may have been added). Any inline `data` is removed. |
| Tried, not delivered | Any inline `data` is removed; `url` (made absolute), `size` and `hash` are as the organization sent them. The documents outcome says why (§4.2). |
| Not tried | As the organization sent it, `url` made absolute. An alternate format whose sibling was delivered, for instance. |

### 4.2 Document records

Each `document` child of the documents outcome is one attempt:

| Field | Meaning |
|---|---|
| `referencedBy` | `<Type>/<id>` of the resource the attachment belongs to. |
| `source` | `DocumentReference.content` or `DiagnosticReport.presentedForm`. |
| `attachment` | The attachment's index in that list. |
| `contentType` | As served (delivered), or as the organization described it (not delivered). |
| `result` | `stored` or `skipped`. |
| `reason`, `detail` | For `skipped`: why (§6) and, for some reasons, a detail such as the HTTP status or the URL refusal. |
| `size`, `sha256` | For `stored`: the decoded bytes and their SHA-256, hex. |
| `binary` | For `stored`: the `fullUrl` of the `Binary` entry. |

## 5. Binary entries

One per delivered document, after every other entry:

```json
{"fullUrl": "urn:uuid:…", "resource": {"resourceType": "Binary", "id": "<document id>",
 "contentType": "text/html; charset=utf-8", "data": "<base64>"}}
```

`id` is `<Type>-<hash>-<index>`: the referencing resource's type, a hash of its id (the id itself
never appears in a file name), and the attachment index. `data` is standard base64 of the bytes;
an empty document has no `data`. The decoded length is the record's `size` and its SHA-256 the
record's `sha256`, which a reader can verify.

## 6. Vocabularies

| Where | Values |
|---|---|
| Section outcome `status` | `ok` (fetched, possibly nothing), `error` (the query failed; `error` says how), `skipped` (the organization did not grant the type's scope at all), `not_requested` (the patient left the type unticked), `not_granted` (requested, withheld by the organization), `pending` (never in a delivered bundle). |
| `emptyReason` (`http://terminology.hl7.org/CodeSystem/list-empty-reason`) | `nilknown` (fetched, nothing there), `withheld` (not requested, not granted, or the scope not granted), `unavailable` (a query failed, or documents were offered and none could be included). |
| Document `result` | `stored`, `skipped`. |
| Document skip `reason` | `count_cap`, `size_cap_each`, `size_cap_total`, `binary_json_cap`, `no_content`, `bad_inline_data`, `url_refused` (detail: `not_https`, `private_address`, `unparseable`, `too_many_redirects`), `http_error` (detail: the status), `network` (detail: the error class), `unreadable_binary`, `token_expired`, `too_slow`, `spool_corrupt`. |
| `typeChoice` | `host` (the patient chose record types on the delivering system's checklist) or `ehr` (the organization's own screen asked; types unticked there appear as `notGranted`). |
| `authMethod` | `public` in this build. |

## 7. Reading a file

- Which organization, when, for whom: `Composition.extension[0]` (`source.brand`, `job.finished`,
  `participant.ref` and the host attributes).
- What was asked for and what came back: the same extension's `scopes`, then each section's
  outcomes. "Which types failed" is every outcome with `status` `error`; "how many Observations"
  is the `Observation` section's `entry` count.
- The resources of a type: the entries whose `resource.resourceType` is that type, or the
  section's `entry[]` references resolved by `fullUrl`.
- A document's bytes: follow the attachment's `url` to the `Binary` entry, decode `data`, check
  the SHA-256 against the document record.
- A pull that was interrupted and resumed delivers the same file as an uninterrupted one would,
  with `job.runs` above 1.

## 8. Known limits

- Decimal text precision is not preserved: a value the organization wrote as `1.10` reads
  `1.1`. The numeric value is never changed.
- A document over the per-document cap, or past the per-document time limit, is skipped and
  recorded; the time limit is checked between reads, so a very slow server can exceed it.
- Resources the organization sent without an `id` cannot be matched across pulls.
