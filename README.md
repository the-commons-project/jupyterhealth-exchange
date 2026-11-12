# JupyterHealth Exchange
JupyterHealth Exchange is a Django web application that facilitates sharing patient-consented health data with authorized users via a Web UI and REST, MCP, and FHIR APIs.

<p align="center">
	<img src="docs/jupyterhealth-exchange.png" width="400">
</p>
Features include:

- OAuth 2.0, OIDC using [django-oauth-toolkit](https://github.com/jazzband/django-oauth-toolkit) and [django-allauth](https://github.com/pennersr/django-allauth) for account login and SAML/SSO
- Simple Role Based Access Control
- FHIR R5 validation using [fhir.resources](https://github.com/glichtner/fhir.resources)
- [Open mHealth](https://www.openmhealth.org/) validation using JSON schemas
- REST APIs using [Django Rest Framework](https://github.com/encode/django-rest-framework)
- Built-in, light-weight Vanilla JS SPA UI (npm not required) using [oidc-clinet-ts](https://github.com/authts/oidc-client-ts), [handlebars](https://github.com/handlebars-lang/handlebars.js) and [bootstrap](https://github.com/twbs/bootstrap)

---


## Documentation

- https://jupyterhealth.github.io/software-documentation/


## What It Does

##### Typical User Flow

Researchers create studies and recruit patients, who consent and submit observations via client apps, and the data is then stored in JupyterHealth Exchange and queried by researchers using Jupyter Notebooks or other systems.

<p align="center">
	<img src="docs/jupyterhealth-exchange-user_flow.jpg" height="400">
</p>

**Typical Data Flow**

Users manage the system via the Web UI, and data producers receive invitation credentials by email, manage consents through the Admin API, and upload data to JupyterHealth Exchange using the FHIR API. Data consumers such as Jupyter Notebooks or other systems then query and read the data through REST and MCP APIs.

<p align="center">
	<img src="docs/jupyterhealth-exchange-data_flow.jpg" height="400">
</p>


## Quick Start with Docker (local development)

Requires Docker with Compose v2.

```
docker compose build
docker compose up
```

The first start migrates and seeds the database (about 30 seconds). Then browse to http://localhost:8000 and log in as `manager_mary@example.com` / `Jhe1234!`, or as `admin@example.com` / `Jhe1234!` for Django admin at http://localhost:8000/admin. See [Seed Data](docs/seed-data.md) for the other accounts.

What's running:

| service | what | host address |
| --- | --- | --- |
| `jhe` | Django dev server; reloads on code and template edits | http://localhost:8000 |
| `db` | Postgres 16; data in `./local/db` (survives `docker compose down`) | `localhost:5432` |
| `cron` | runs `ow_poll` every 15 minutes, like production | none |
| `mcp` (profile `mcp`) | the [MCP server](mcp_server/), wired to the local JHE | http://localhost:8401 |
| `s3` (profile `s3`) | an S3-compatible object store ([PGSTY Silo](https://github.com/pgsty/silo)) for features that deliver to S3; data in `./local/s3` | API `localhost:9000`, console http://localhost:9001 (`jhe-dev-s3` / `jhe-dev-s3-secret`) |

The two profiled services are off by default. Turn them on per run with `COMPOSE_PROFILES=mcp,s3 docker compose up`, or keep them on by setting `COMPOSE_PROFILES` in a root `.env` (see `.env.example`). Compose only acts on the profiles that are active, so `docker compose down` leaves a profiled service running unless the profile is active for that command too; `docker compose --profile '*' down` stops everything.

The first boot also generates a dev-only OIDC signing key into `./local/oidc.key`; delete the file to rotate it. Emails (patient invitations, signup verification, login codes) print to `docker compose logs jhe`.

Ports already in use? Set `JHE_PORT`, `JHE_DB_PORT`, `MCP_PORT`, `S3_PORT` and `S3_CONSOLE_PORT` in a root `.env` (see `.env.example`), or per run: `JHE_PORT=8010 JHE_DB_PORT=5442 MCP_PORT=8411 docker compose up`. The seeded OAuth redirect URIs and site URL follow `JHE_PORT`, but they are written when the database is seeded, so run `scripts/dev/reset.sh` after changing it. Two seeded client invitation URLs (Open Wearables, EHR Patient Portal) are fixed at `localhost:8001` / `localhost:8000`; edit them under Clients if you need them.

Configuration lives in [compose/environment/](compose/environment/README.md). The committed values are public, local-only development values; never use them in a deployment.

Helper scripts:

| script | does |
| --- | --- |
| `scripts/dev/manage.sh <command>` | run a `manage.py` command in the `jhe` container |
| `scripts/dev/shell.sh` | Django shell in the `jhe` container |
| `scripts/dev/reset.sh` | wipe `./local/db` and start over with freshly seeded data |
| `scripts/dev/test.sh [--fresh] [pytest args]` | backend tests in a container, against a disposable in-memory Postgres |
| `scripts/dev/check.sh [--quick]` | lint, format, missing-migration check, type and layering checks for `patient_access/`, backend and frontend tests, all in containers <!-- uscdi-patient-access: the mypy and import checks --> |
| `scripts/dev/bruno-token.sh` | API token for the [Bruno](https://www.usebruno.com/) collection in `opencollection/JHE/`, or for curl |

The test and check scripts use their own compose project (`docker-compose.test.yml`), so they never touch your dev data, and need nothing installed but Docker.

### Trying out the local stack

The examples use the default ports; substitute yours.

**Web UI.** Log in at http://localhost:8000 as `manager_mary@example.com` / `Jhe1234!` and browse Organizations → Studies → Patients → Observations. Generating an invitation with "send email" prints the email to `docker compose logs jhe`.

**REST and FHIR APIs.** Get a token for the admin user, then query:

```
TOKEN=$(scripts/dev/bruno-token.sh | awk '/Token:/{print $2}')
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/studies
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/FHIR/R5/Observation?patient=40001"
```

`bruno-token.sh` makes the user a manager of every organization, so mint tokens for `admin@example.com` (the default) rather than for an account whose permissions you want to test. Observation uploads go to `POST /FHIR/R5/Observation` with `Content-Type: application/json`, a `subject` of `Patient/<id>`, a `device` of `Device/<id>` (a personal-device data source on the patient's study) and the Open mHealth JSON base64-encoded in `valueAttachment.data`. Records imported from an EHR are returned only with `_source:below=https://jupyterhealth.org/fhir/fhir-source/`.

**MCP server.** Add it to an MCP client, for example Claude Code:

```
claude mcp add --transport http jhe-local http://localhost:8401/mcp
```

Authenticate (in Claude Code, `/mcp` → `jhe-local`), log in to JHE in the browser, then ask about your studies and patients. Every tool call runs as the logged-in user, so logging in as a different seeded user shows different studies. `docker compose logs mcp` shows an audit line per call. The MCP server keeps sessions in memory, so reconnect after restarting the `mcp` container.

**EHR Patient Portal against the Epic sandbox.** The shared Epic sandbox app only redirects back to `http://localhost:8000`, so run JHE on port 8000 (and `scripts/dev/reset.sh` if you changed it). Generate an invitation for patient 40001 with the "EHR Patient Portal" client, open it in a private window, choose "Epic Health System (Sandbox)" and sign in as one of the sandbox test patients listed on [fhir.epic.com](https://fhir.epic.com).

**USCDI Patient Access against the SMART sandbox.** The server-side client that delivers each pull to S3 needs the `s3` profile: `COMPOSE_PROFILES=s3 docker compose up`. The first boot generates the module's key (`./local/patient-access.key`), and `seed_patient_access_sandbox` adds two SMART Health IT sandbox "hospitals" and points the S3 settings at the local store (rerun it with `scripts/dev/manage.sh seed_patient_access_sandbox` if the `s3` service was not up yet; `scripts/dev/manage.sh patient_access_check` confirms a pull can be delivered). Generate an invitation for patient 40001 with the "USCDI Patient Access" client, open it, agree to share, pick "SMART Sandbox Express (no login, fixed patient)" and continue: the sandbox signs in by itself, the pull runs on the server, and the bundle lands in the `patient-access-dev` bucket (console at http://localhost:9001). The delivered file's specification is `patient_access/docs/patient-access-bundle.md`, and `python patient_access/scripts/validate_patient_access_bundle.py FILE` checks a file against it. The HTTP library's own request lines carry patient ids in their query strings, so they stay out of the log; to see them while debugging locally, set `PATIENT_ACCESS_HTTP_DEBUG_LOGGING=1` in `compose/environment/local.secret.env` and restart. <!-- uscdi-patient-access -->

## Getting Started (without Docker)

1. Set up your Python environment and install dependencies with [uv](https://docs.astral.sh/uv/): `uv sync` - this project uses Django **version 5.2**
    - `uv sync` installs the exact versions from `uv.lock` into `.venv` (dev dependencies included by default; use `uv sync --no-dev` for a production-only install)

1. Create a new Postgres DB (currently only Postgres is supported)

1. Copy `.env.example` to `.env` and update the `DB_*` parameters to match (2) above.

   -  Optionally you can add a Django `SECRET_KEY` by running the command below or you can leave this for now to use a randomly generated value at runtime (this will not work with more than one worker)
      `$ openssl rand -base64 32`

1. Ensure the `.env` is loaded into your Python environment, and prefix commands with `uv run` (or activate the venv: `$ source .venv/bin/activate`)

1. Run the Django migration `$ python manage.py migrate` to create the database tables.

1. Seed the database by running the Django management command `$ python manage.py seed`

1. Start the server with `$ python manage.py runserver`

1. Browse to http://localhost:8000/admin and enter the credentials `admin@example.com` `Jhe1234!`

1. Under *Django OAuth Toolkit* > *Applications* you should see the seeded OAuth2 application named `JHE Admin UI` . Click on the PK and under *Redirect uris* there should be an entry of `http://localhost:8000/auth/callback` - this is used for the Web UI OAuth 2.0 login.

1. Click the LOG OUT button at the top

1. Finally, we need to create an RS256 Private Key for signing the JWT

      - Run `openssl genrsa -out oidc.key 4096`
      - Run `awk '{printf "%s%s", (NR==1?"":"\\n"), $0}' oidc.key` to remove line breaks
        **Note: some python environments and OS combinations do not handle the "\n" so you may need to include line breaks in the `.env` file.**

      - Return to the `.env` file and update the `OIDC_RSA_PRIVATE_KEY`
      - Keep the `oidc.key` somewhere safe

1. Browse to http://localhost:8000/ and log in with the credentials `manager_mary@example.com` / `Jhe1234!` or `manager_mark@example.com` / `Jhe1234!` and you should be directed to the `/clients/jhe-admin/organizations` path with some example Organizations in the dropdown. See [Seed Data](docs/seed-data.md) to understand how these example Users and Organizations are structured.

1. New users can be signed up from the base URL (eg http://localhost:8000/) with the default invitation code "**jhe**". This invitation code and other settings can be changed from the same URL by logging in as the Admin user (`admin@example.com` / `Jhe1234!`) and opening the System Settings menu.

> [!NOTE]
> Due to browser security restrictions and the [oidc-client-ts](https://github.com/authts/oidc-client-ts) used for authentication, the web app **must be accessed over HTTPS for any hostname other than localhost** - see [Running in Production](#running-in-production) below.



## Who and What the App Manages

Entities are based on the [HL7 FHIR model](https://build.fhir.org/), a widely used healthcare standard for organizing and exchanging clinical information between systems.

### Patients & Practitioners

- Any user accessing the Web UI is a [Practitioner](https://build.fhir.org/practitioner.html) by default. In practice this might be a researcher, a clinician, an administrator or even an individual setting up JupyterHealth Exchange to view their own health data.
- [Patient](https://build.fhir.org/patient.html) users are registered by Practitioners and are sent a link to authenticate and upload data.
- The same OAuth 2.0 strategy is used for both Practitioners and Patients, the only difference being that the authorization code is provided out-of-band for Patients (ie invitation links).

### Organizations

- An [Organization](https://build.fhir.org/organization.html) is a group of Practitioners, Patients and Studies (FHIR Groups) and is used to manage access to data.
- An Organization is typically hierarchical with sub-Organizations like Institutions, Departments, Labs etc.
- A Practitioner belongs to one or more Organization.
- A Patient belongs to one or more Organization.
- A Study belongs to one single Organization.

### Studies

- A Study is a [Group](https://build.fhir.org/group.html) of Patients and belongs to a single Organization.
- A Study has one or more Clients (apps that talk to JupyterHealth Exchange), one or more matching Data Sources (anything that produces data) and one or more Scope Requests (eg Blood Pressure, Heart Rate, etc)
- When a Patient is added to a Study, they must explicitly consent to sharing the requested Scopes before any personal data (Observations) can be uploaded or shared.

### Observations

- An [Observation](https://www.hl7.org/fhir/observation.html) is Patient data and belongs to a single Patient.
- An Observation must reference a Patient ID as the *subject* and a Data Source ID as the *device*.
- Personal device data is expected to be in the [Open mHealth](https://www.openmhealth.org/documentation/#/overview/get-started) (JSON) format however the system can be easily extended to support any binary data attachments or discrete Observation records.
- Observation data is stored as a *valueAttachment* in Base 64 encoded JSON binary.
- Authorization to view Observations depends on the relationship of Organization, Study and Scopes/consents as described above.

### Data Sources

- A Data Source is anything that produces Observations (typically a device app eg iHealth). An Observation references a Data Source ID in the *device* field.
- A Data Source supports one or more Scopes (types) of Observations (eg Blood Glucose).
- A Study has one or more associated Data Sources.
-  A Data Source has one Client. In some cases a single app may be both a Data Source and a Client, in which case a record is created for each and both are added to the Study.

### Clients

- Clients are apps that talk to JupyterHealth Exchange. Each Client is an OAuth 2.0 application with its own Client ID.
- A Study supports **more than one** Client, so several apps can submit and read data for the same Study (multi-client support). Manage a Study's Clients under View Study → Clients.
- There are two kinds of Client:
  - **Patient apps** (OAuth authorization-code grant): managed by an admin under the Clients menu. Each has an invitation URL used to onboard Patients and one or more associated Data Sources.
  - **Practitioner API clients** (OAuth client-credentials grant): created self-service by a Practitioner for direct API access, scoped to that Practitioner.
- A Client has one or more associated Data Sources. In some cases a single app may be both a Data Source and a Client, in which case a record is created for each and both are added to the Study.
- A Patient's Clients are consolidated across every Study they are enrolled in, so an app can discover all the Studies and invitations relevant to that Patient.

## Quick Start Walkthrough

1. Sign up as a new user from the Web UI.
2. Create a new Organization (your user is automatically added to the Organization with a Manager role).
3. Create a new Study for the Organization (View Organization → Studies+).
4. Create a new Patient for the Organization using a different email than step 1 (Patients → Add Patient).
5. Add Data Sources and Scopes to the Study (View Study → Data Sources+, Scope Requests+).
6. Add the Patient to the Study (Patients → Select patient → Add Patient(s) to Study).
7. Create an Invitation Link for the Patient (View Patient → Generate Invitation Link).
8. Use the code in the invitation link with the Auth API to exchange it for an access token.
9. Upload Observations using the FHIR API and access token.
10. View the Observations from the Web UI.

## Demonstration Clients, Data Sources and Auth Strategies

| Data Source | Client | Auth |
| --- | --- | --- |
| **CareX**<br>`omh:blood-pressure:4.0`<br>`omh:heart-rate:2.0`<br><br>**Questionnaire**<br>`QuestionnaireResponse` | **CareX** | Invitation Link |
| **Dexcom Stelo**<br>`omh:blood-glucose:4.0`<br><br>**iHealth**<br>`omh:body-temperature:4.0`<br>`omh:heart-rate:2.0` | **CommonHealth** | Invitation Link |
| **EHR Patient Portal**<br>`*` (all FHIR resources) | **EHR Patient Portal** | Invitation Link |
| – | **JHE Admin** | User Credentials<br/>(Username/Password) |
| **Oura**<br>`ieee:physical-activity:1.0`<br>`ieee:sleep-episode:1.0`<br>`ieee:sleep-stage-summary:1.0`<br>`ieee:time-in-bed:1.0`<br>`ieee:total-sleep-time:1.0`<br>`omh:body-height:2.0`<br>`omh:body-weight:3.0`<br>`omh:heart-rate:2.0`<br>`omh:oxygen-saturation:2.0`<br>`omh:respiratory-rate:2.0` | **Open Wearables** | Invitation Link |
| – | – | Patient Access<br/>(E-mail one-time code) |

## Running in Production

`GET /health` is the liveness probe: no database, no auth, and it answers whatever Host header
the probe carries (a load balancer sends the container's own IP address), so it can be the
target of an ALB, Fly or Kubernetes check as is. It returns `{"status": "ok", "version": "<JHE
version>"}` with status 200.

## Contributing

See [doc](https://jupyterhealth.github.io/software-documentation/) for test requirements, coding standards, and PR checklist.
