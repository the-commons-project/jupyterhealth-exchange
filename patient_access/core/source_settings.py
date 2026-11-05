"""SourceSettings: how we authenticate with one endpoint and what we pull from it.

    SourceSettings
      .registration  ClientRegistration   our app as registered with the vendor (phase 3)
      .fetch         FetchProfile         the query table, limits and document preferences (phase 4)

Vendor knowledge (the profile, the scope version, the methods the vendor accepts) is the same in
every deployment and lives in code (`vendors.py`, `profiles.py`). The deployment's part is the
client id, which the host's directory supplies. Document *caps* are not here: they are runtime
settings the host reads once per attempt (`settings.py`).
"""

from __future__ import annotations

from enum import StrEnum

from .credentials import PUBLIC, AuthMethod
from .models import DocumentSource, Value
from .scopes import Access, Context, Permission, ScopeSet, constraints_of


class ScopeVersion(StrEnum):
    V1 = "v1"  # patient/X.read
    V2 = "v2"  # patient/X.rs


def to_v2(scope: str) -> str:
    return scope.removesuffix(".read") + ".rs" if scope.endswith(".read") else scope


class QuerySpec(Value):
    """One row of the query table: what to GET and which scope gates it."""

    label: str
    resource_type: str
    query: str  # "Observation?category=laboratory"; "Patient" with single=True is an instance read
    scope: str  # the patient/<Type>.read scope that gates it
    single: bool = False  # an instance read of the patient's own id (Patient)

    @property
    def access(self) -> Access:
        """What running this row needs: a search under its parameters, or a read for `single`."""
        _, _, query = self.query.partition("?")
        return Access(
            context=Context.PATIENT,
            resource_type=self.resource_type,
            permission=Permission.READ if self.single else Permission.SEARCH,
            parameters=constraints_of(query),
        )


class DocumentProfile(Value):
    """Vendor knowledge about documents: where to look and which format to prefer."""

    sources: frozenset[DocumentSource] = frozenset(DocumentSource)
    # Preference order when a DocumentReference offers one document in several formats.
    prefer: tuple[str, ...] = ("text/plain", "text/html", "application/pdf", "text/rtf", "application/xml")


class FetchProfile(Value):
    name: str
    queries: tuple[QuerySpec, ...]
    page_cap: int = 200
    page_size: int | None = None  # adds _count when set
    timeout_seconds: float = 30.0
    max_retries: int = 3
    documents: DocumentProfile = DocumentProfile()

    def with_query(self, label: str, query: str, *, name: str) -> FetchProfile:
        """A derived profile with one row's query replaced."""
        if label not in {q.label for q in self.queries}:
            raise KeyError(f"profile {self.name!r} has no row {label!r}")
        rows = tuple(q.model_copy(update={"query": query}) if q.label == label else q for q in self.queries)
        return self.model_copy(update={"name": name, "queries": rows})


class ClientRegistration(Value):
    """Our app as registered with one vendor in one environment."""

    vendor: str
    client_id: str
    methods: tuple[AuthMethod, ...] = (PUBLIC,)  # preference order
    base_scopes: tuple[str, ...] = ("openid", "fhirUser", "launch/patient")
    scope_version: ScopeVersion = ScopeVersion.V1

    def spelled(self, scope: str) -> str:
        """`scope` in this registration's scope form (`.read` becomes `.rs` under v2)."""
        return to_v2(scope) if self.scope_version is ScopeVersion.V2 else scope

    def scopes(self, resource_scopes: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """Base scopes plus the given resource scopes, in this registration's scope form."""
        out: list[str] = list(self.base_scopes)
        for s in map(self.spelled, resource_scopes):
            if s not in out:
                out.append(s)
        return tuple(out)


class SourceSettings(Value):
    registration: ClientRegistration
    fetch: FetchProfile

    def scopes(self, resource_scopes: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
        """Base scopes plus the resource scopes asked for (every row's, when None)."""
        wanted = resource_scopes if resource_scopes is not None else [q.scope for q in self.fetch.queries]
        return self.registration.scopes(wanted)

    def queries_for(self, scopes: tuple[str, ...]) -> tuple[QuerySpec, ...]:
        """Rows whose access these scopes cover, in any SMART spelling (see `scopes.py`)."""
        covered = ScopeSet.parse(scopes)
        return tuple(q for q in self.fetch.queries if covered.covers(q.access))
