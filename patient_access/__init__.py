"""Patient Access: a server-side SMART on FHIR patient-access pull, delivered as one FHIR document
Bundle per pull.

    core/         framework-free: models, plugs (Protocols), the SMART flow, the fetch, the pipeline
    spool/        where a pull waits between fetch and delivery (local disk, encrypted)
    formatters/   the document Bundle, as an entry iterator and a byte stream
    destinations/ where a pull is delivered (S3, multipart, streamed)
    dispatch/     how a job is started (a thread in the web process; inline for tests)
    django_app/   the Django app a host installs: the two tables, the stores, the wiring
    testing/      in-memory plugs and sandbox helpers for tests and dev

Core imports nothing from Django or boto3 (enforced by import-linter contracts in pyproject.toml);
`django_app` is the only Django code. The package lives in the JHE repository for now and is meant
to move to its own repository unchanged.
"""

__version__ = "0.1.0"
