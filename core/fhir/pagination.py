from django.db.models.query import RawQuerySet
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from core.pagination import PaginatedRawQuerySet


class ConcatenatedResults:
    """A paginator-friendly concatenation of several querysets, each with its own serializer.

    Used to present a FHIR search as the UNION of the mapped Django rows and the FhirAuxResource
    rows of a type. Supports ``.count()`` and slice ``__getitem__`` so Django's Paginator slices
    each source at the DB level (LIMIT/OFFSET) rather than materializing everything in Python.

    ``sources`` is a list of ``(queryset, serialize_fn)``. Indexing returns a list of
    ``(serialize_fn, instance)`` pairs preserving source order.
    """

    def __init__(self, sources):
        self._sources = sources
        self._counts = None  # cached per-source counts, computed once

    def _source_counts(self):
        if self._counts is None:
            self._counts = [qs.count() for qs, _ in self._sources]
        return self._counts

    def count(self):
        return sum(self._source_counts())

    def __len__(self):
        return self.count()

    def __getitem__(self, item):
        if not isinstance(item, slice):
            raise TypeError("ConcatenatedResults only supports slicing")
        start = item.start or 0
        remaining = None if item.stop is None else max(item.stop - start, 0)

        result = []
        offset = start
        for (queryset, serialize_fn), size in zip(self._sources, self._source_counts()):
            if remaining is not None and remaining <= 0:
                break
            if offset >= size:
                offset -= size
                continue
            stop = None if remaining is None else offset + remaining
            rows = list(queryset[offset:stop])
            result.extend((serialize_fn, row) for row in rows)
            if remaining is not None:
                remaining -= len(rows)
            offset = 0
        return result


class FHIRBundlePagination(PageNumberPagination):
    """
    FHIR Bundle pagination using database-level pagination with raw SQL.
    No in-memory result sets or mock objects.
    """

    # FHIR standard query parameters
    page_size_query_param = "_count"
    page_query_param = "_page"
    page_size = 20
    max_page_size = 1000  # TBD: May need to be adjusted based on database performance and testing

    def paginate_queryset(self, queryset, request, view=None):
        if isinstance(queryset, RawQuerySet):
            queryset = PaginatedRawQuerySet.from_raw(queryset)
        return super().paginate_queryset(queryset, request, view=view)

    def get_paginated_response(self, data):
        """Return FHIR-compliant Bundle response with pagination"""
        response_data = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": self.page.paginator.count,
            "entry": data,
            "link": self._get_fhir_links(),
            "meta": {},
        }

        return Response(response_data)

    def _get_fhir_links(self):
        """Generate FHIR Bundle links for pagination"""
        links = []

        # Self link (always present)
        links.append({"relation": "self", "url": self.request.build_absolute_uri()})

        prev_link = self.get_previous_link()
        next_link = self.get_next_link()
        if prev_link:
            links.append({"relation": "previous", "url": prev_link})
        if next_link:
            links.append({"relation": "next", "url": next_link})
        return links
