from rest_framework import pagination
from rest_framework.response import Response

class FHIRBundlePagination(pagination.PageNumberPagination):
    def get_paginated_response(self, data):
        response = {
                'resourceType': 'Bundle',
                'type': 'searchset',
                'link': [],
                # TBD: 'count': self.page.paginator.count,
                'entry': data,
            }
        if self.get_previous_link():
            response['link'].append({
                'relation': 'previous',
                'url': self.get_previous_link()
            })
        if self.get_next_link():
            response['link'].append({
                'relation': 'next',
                'url': self.get_next_link()
            })
        if len(response['link']) == 0:
            del response['link']
        return Response(response)