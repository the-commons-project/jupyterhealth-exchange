from rest_framework.pagination import PageNumberPagination
import math
from django.db import models
from rest_framework.response import Response


class FHIRBundlePagination(PageNumberPagination):
    """
    FHIR Bundle pagination using database-level pagination with raw SQL.
    No in-memory result sets or mock objects.
    """

    # FHIR standard query parameters
    page_size_query_param = "_count"
    page_query_param = "_page"
    default_page_size = 20
    max_page_size = 1000  # TBD: May need to be adjusted based on database performance and testing

    def paginate_raw_sql(self, sql, params, request, count_sql=None):
        """
        Paginate raw SQL directly using database level pagination.
        Returns (results, count, page_number, page_size)
        """
        try:
            page_size = self.get_page_size(request)
            page_number = self.get_page_number(request)
        except (ValueError, TypeError):
            page_size = self.default_page_size
            page_number = 1

        offset = (page_number - 1) * page_size

        paginated_sql = f"{sql} LIMIT {page_size} OFFSET {offset}"

        # Django's RawQuerySet expects a model to map results to, so we need to modify the count query
        if count_sql:
            count_query_with_alias = f"{count_sql} AS count_col"
        else:
            count_query_with_alias = f"SELECT COUNT(*) AS count_col FROM ({sql}) AS subquery"

        # Execute the count query through the ORM
        count_result = list(models.Manager.raw(count_query_with_alias, params))
        total_count = count_result[0].count_col if count_result else 0

        # Execute main query through Django's QuerySet.raw()
        # This is a RawQuerySet which doesn't pull all results into memory at once
        raw_query_set = models.Manager.raw(paginated_sql, params)

        columns = [field.name for field in raw_query_set.columns]
        results = [{columns[i]: getattr(row, columns[i]) for i in range(len(columns))} for row in raw_query_set]

        # Store request and pagination info for links and response
        self.request = request
        self.page_number = page_number
        self.page_size = page_size
        self.total_count = total_count
        self.total_pages = math.ceil(total_count / page_size) if total_count > 0 else 1

        return results

    def get_page_number(self, request, paginator=None):
        """Get page number from request or default to 1"""
        try:
            return int(request.query_params.get(self.page_query_param, 1))
        except (ValueError, TypeError):
            return 1

    def get_page_size(self, request):
        """Get page size from request or use default"""
        if self.page_size_query_param:
            try:
                requested_page_size = int(request.query_params.get(self.page_size_query_param, self.default_page_size))
                return min(requested_page_size, self.max_page_size)
            except (ValueError, TypeError):
                pass
        return self.default_page_size

    def get_paginated_fhir_response(self, data):
        """Return FHIR-compliant Bundle response with pagination"""
        response_data = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": self.total_count,
            "entry": data,
            "link": self._get_fhir_links(),
            "meta": {
                "pagination": {
                    "page": self.page_number,
                    "pageSize": self.page_size,
                    "totalPages": self.total_pages,
                }
            },
        }
        return Response(response_data)

    def _get_fhir_links(self):
        """Generate FHIR Bundle links for pagination"""
        links = []
        base_url = self.request.build_absolute_uri().split("?")[0]
        query_params = self.request.query_params.copy()

        # Self link (always present)
        links.append({"relation": "self", "url": self.request.build_absolute_uri()})

        # Previous page link
        if self.page_number > 1:
            prev_params = query_params.copy()
            prev_params[self.page_query_param] = self.page_number - 1
            prev_url = f"{base_url}?{prev_params.urlencode()}"
            links.append({"relation": "previous", "url": prev_url})

        # Next page link
        if self.page_number < self.total_pages:
            next_params = query_params.copy()
            next_params[self.page_query_param] = self.page_number + 1
            next_url = f"{base_url}?{next_params.urlencode()}"
            links.append({"relation": "next", "url": next_url})

        # First page link
        if self.page_number > 1:
            first_params = query_params.copy()
            first_params[self.page_query_param] = 1
            first_url = f"{base_url}?{first_params.urlencode()}"
            links.append({"relation": "first", "url": first_url})

        # Last page link
        if self.page_number < self.total_pages:
            last_params = query_params.copy()
            last_params[self.page_query_param] = self.total_pages
            last_url = f"{base_url}?{last_params.urlencode()}"
            links.append({"relation": "last", "url": last_url})

        return links
