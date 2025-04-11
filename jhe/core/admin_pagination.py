from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.exceptions import NotFound

from django.db import models
from django.db.models import sql
from django.db.models.query import RawQuerySet
from urllib.parse import urlencode

# https://stackoverflow.com/questions/32191853/best-way-to-paginate-a-raw-sql-query-in-a-django-rest-listapi-view#:~:text=A%20more%20efficient%20solution%20than,in%20your%20raw%20SQL%20query
class PaginatedRawQuerySet(RawQuerySet):
    def __init__(self, raw_query, **kwargs):
        super(PaginatedRawQuerySet, self).__init__(raw_query, **kwargs)
        self.original_raw_query = raw_query
        self._result_cache = None

    def __getitem__(self, k):
        """
        Retrieves an item or slice from the set of results.
        """
        if not isinstance(k, (slice, int,)):
            raise TypeError
        assert ((not isinstance(k, slice) and (k >= 0)) or
                (isinstance(k, slice) and (k.start is None or k.start >= 0) and
                 (k.stop is None or k.stop >= 0))), \
            "Negative indexing is not supported."

        if self._result_cache is not None:
            return self._result_cache[k]

        if isinstance(k, slice):
            qs = self._clone()
            if k.start is not None:
                start = int(k.start)
            else:
                start = None
            if k.stop is not None:
                stop = int(k.stop)
            else:
                stop = None
            qs.set_limits(start, stop)
            return qs

        qs = self._clone()
        qs.set_limits(k, k + 1)
        return list(qs)[0]

    def __iter__(self):
        self._fetch_all()
        return iter(self._result_cache)

    def count(self):
        if self._result_cache is not None:
            return len(self._result_cache)

        return self.model.objects.count()

    def set_limits(self, start, stop):
        limit_offset = ''

        new_params = tuple()
        if start is None:
            start = 0
        elif start > 0:
            new_params += (start,)
            limit_offset = ' OFFSET %s'
        if stop is not None:
            new_params = (stop - start,) + new_params
            limit_offset = 'LIMIT %s' + limit_offset

        self.params = self.params + new_params
        self.raw_query = self.original_raw_query + limit_offset
        self.query = sql.RawQuery(sql=self.raw_query, using=self.db, params=self.params)

    def _fetch_all(self):
        if self._result_cache is None:
            self._result_cache = list(super().__iter__())

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self.model.__name__)

    def __len__(self):
        self._fetch_all()
        return len(self._result_cache)

    def _clone(self):
        clone = self.__class__(raw_query=self.raw_query, model=self.model, using=self._db, hints=self._hints,
                               query=self.query, params=self.params, translations=self.translations)
        return clone
    
class AdminModelRawManager(models.Manager):
    def raw(self, raw_query, params=None, translations=None, using=None):
        if using is None:
            using = self.db
        return PaginatedRawQuerySet(raw_query, model=self.model, params=params, translations=translations, using=using)

    def my_raw_sql_method(self, query, params=None):
      """
      Execute a raw SQL query with pagination support.
      
      Args:
        query (str): The raw SQL query to execute
        params (tuple, optional): Parameters for the SQL query
        
      Returns:
        PaginatedRawQuerySet: A queryset that can be paginated
      """
      return self.raw(raw_query=query, params=params)

class CustomPageNumberPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    page_query_param = 'page'
    max_page_size = 1000

    def get_paginated_response(self, data):
        return Response({
            'count': self.page.paginator.count,
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'results': data
        })

    def paginate_queryset(self, queryset, request, view=None):
        if not queryset:
            raise NotFound("No results found")
        return super().paginate_queryset(queryset, request, view)
    
def build_url(base_url, page, params):
  query_params = {k: v for k, v in params.items() if v is not None}
  query_params['page'] = page
  return f"{base_url}?{urlencode(query_params)}"

# This mixin is used to allow custom pagination for Admin API views.
class AdminListMixin:
  """Mixin that provides a standardized list implementation for admin APIs."""
  raw_manager = AdminModelRawManager
  model_class = None  # Override in subclass
  serializer_class = None  # Override in subclass
  
  def get_queryset_params(self, request):
    """Extract and return parameters needed for the query.
    Override in subclasses to customize parameter handling."""
    return {
      'organization_id': request.query_params.get('organization_id')
    }
  
  def list(self, request):
    # Get query parameters for pagination
    page_param = request.query_params.get('page', None)
    page_size_param = request.query_params.get('page_size', None)

    try:
      page = int(page_param) if page_param and page_param.lower() != "null" else 1
    except (ValueError, AttributeError):
      page = 1

    try:
      page_size = int(page_size_param) if page_size_param and page_size_param.lower() != "null" else 20
    except (ValueError, AttributeError):
      page_size = 20
    
    # Get query parameters
    params = self.get_queryset_params(request)
    
    # Call the model's query methods
    data = self.model_class.for_practitioner_organization_study_patient(
      request.user.id,
      **params,
      page=page,
      pageSize=page_size
    )
    
    count = self.model_class.count_for_practitioner_organization_study_patient(
      request.user.id,
      **params
    )
    
    serialized_data = self.serializer_class(data, many=True).data
    
    base_url = request.build_absolute_uri().split('?')[0]

    # Build response with pagination links
    response_params = {**params, 'page_size': page_size}
    response_data = {
      'count': count,
      'next': build_url(base_url, page + 1, response_params) if page * page_size < count else None,
      'previous': build_url(base_url, page - 1, response_params) if page > 1 else None,
      'results': serialized_data
    }
    
    return Response(response_data)