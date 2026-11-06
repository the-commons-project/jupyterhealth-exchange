"""SMART scopes as values, and the set algebra the flow needs.

A scope string parses to a `ResourceScope`: a context, a resource type (or `*`), a set of
permissions and a set of constraints. SMART v1 and v2 spellings parse to the same value
(`read` is {r, s}, `*` is all five), so nothing depends on which form a server echoes back:

    patient/Observation.read                       -> patient, Observation, {r, s}
    patient/Observation.rs                         -> patient, Observation, {r, s}
    patient/Observation.r   +   patient/Observation.s     (Epic grants read and search apart)
    patient/Observation.s?category=...|laboratory  -> patient, Observation, {s}, {category: ...}

What a query row needs is one `Access`: one permission on one type, under the row's own query
parameters. A `ScopeSet` covers an access when one of its scopes has the context, the type, the
permission, and only constraints the access satisfies. A constraint is satisfied by a parameter
of the same name whose token matches: the codes are equal, and the systems too when both sides
name one (`laboratory` satisfies `http://.../observation-category|laboratory`).

Coverage is per access, so a requested scope can be covered by several granted ones together:
`Observation.read` asks for {r, s}, and Epic's `Observation.r` and `Observation.s` cover it.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from urllib.parse import parse_qsl

from .models import Value


class Context(StrEnum):
    PATIENT = "patient"
    USER = "user"
    SYSTEM = "system"


class Permission(StrEnum):
    """SMART v2 permissions, in their canonical order."""

    CREATE = "c"
    READ = "r"
    UPDATE = "u"
    DELETE = "d"
    SEARCH = "s"


ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

# SMART v1 permission words, as v2 permission sets.
V1_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "read": frozenset({Permission.READ, Permission.SEARCH}),
    "write": frozenset({Permission.CREATE, Permission.UPDATE, Permission.DELETE}),
    "*": ALL_PERMISSIONS,
}

WILDCARD_TYPE = "*"


class Constraint(Value):
    """One query parameter a granular scope is limited to: `category=<system>|<code>`."""

    name: str
    value: str

    @property
    def system(self) -> str | None:
        system, bar, _ = self.value.rpartition("|")
        return system if bar and system else None

    @property
    def code(self) -> str:
        return self.value.rpartition("|")[2]

    def satisfied_by(self, other: Constraint) -> bool:
        return (
            self.name == other.name
            and self.code == other.code
            and (self.system is None or other.system is None or self.system == other.system)
        )


def constraints_of(query: str) -> frozenset[Constraint]:
    """The parameters of a query string (`a=1&b=2`) as constraints."""
    return frozenset(Constraint(name=k, value=v) for k, v in parse_qsl(query, keep_blank_values=True))


class Access(Value):
    """One permission on one resource type, under the parameters of the query that needs it."""

    context: Context
    resource_type: str
    permission: Permission
    parameters: frozenset[Constraint] = frozenset()


class ResourceScope(Value):
    context: Context
    resource_type: str  # a FHIR type, or WILDCARD_TYPE
    permissions: frozenset[Permission]
    constraints: frozenset[Constraint] = frozenset()

    @classmethod
    def parse(cls, raw: str) -> ResourceScope | None:
        """The resource scope `raw` names, or None for anything else (`openid`, `launch/patient`,
        a malformed or unknown spelling)."""
        head, _, query = raw.partition("?")
        context, slash, rest = head.partition("/")
        resource_type, dot, permissions = rest.rpartition(".")
        if not (slash and dot and resource_type) or context not in Context._value2member_map_:
            return None
        parsed = _permissions(permissions)
        if parsed is None:
            return None
        return cls(
            context=Context(context),
            resource_type=resource_type,
            permissions=parsed,
            constraints=constraints_of(query),
        )

    def accesses(self) -> frozenset[Access]:
        """What this scope asks for, one access per permission (a wildcard type stays a wildcard)."""
        return frozenset(
            Access(
                context=self.context,
                resource_type=self.resource_type,
                permission=p,
                parameters=self.constraints,
            )
            for p in self.permissions
        )

    def grants(self, access: Access) -> bool:
        """A `user/` scope covers a `patient/` access too: in a patient's own standalone launch
        the user is the patient, and every query is pinned to the patient's id, so it can only
        ever widen what we fetch to what the server granted."""
        return (
            (self.context is access.context or (self.context is Context.USER and access.context is Context.PATIENT))
            and self.resource_type in (access.resource_type, WILDCARD_TYPE)
            and access.permission in self.permissions
            and all(any(c.satisfied_by(p) for p in access.parameters) for c in self.constraints)
        )


def _permissions(raw: str) -> frozenset[Permission] | None:
    """v1 words, or v2 letters (each at most once, in `cruds` order)."""
    if raw in V1_PERMISSIONS:
        return V1_PERMISSIONS[raw]
    order = [p.value for p in Permission]
    if not raw or any(c not in order for c in raw) or list(raw) != sorted(set(raw), key=order.index):
        return None
    return frozenset(Permission(c) for c in raw)


class ScopeSet(Value):
    """The resource scopes among a list of scope strings (the rest are ignored)."""

    scopes: frozenset[ResourceScope]

    @classmethod
    def parse(cls, raw: Iterable[str]) -> ScopeSet:
        return cls(scopes=frozenset(s for s in map(ResourceScope.parse, raw) if s is not None))

    def covers(self, access: Access) -> bool:
        return any(s.grants(access) for s in self.scopes)

    def covers_all(self, scope: ResourceScope) -> bool:
        return all(self.covers(a) for a in scope.accesses())

    def withheld(self, requested: Iterable[str]) -> tuple[str, ...]:
        """The requested resource scopes this set does not fully cover, as written, sorted."""
        return tuple(
            sorted(
                raw
                for raw in requested
                if (scope := ResourceScope.parse(raw)) is not None and not self.covers_all(scope)
            )
        )
