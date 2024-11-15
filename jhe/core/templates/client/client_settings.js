{% autoescape off %}

window.OIDCSettings = {
  authority: "{{ OIDC_CLIENT_AUTHORITY }}",
  client_id: "{{ OIDC_CLIENT_ID }}",
  // silentRequestTimeoutInSeconds: 600,
  // popup_redirect_uri: 'http://127.0.0.1:8000/auth/callback_popup',
  redirect_uri: "{{ OIDC_CLIENT_REDIRECT_URI }}",
  extraQueryParams: {},
  response_mode: "query",
};

const CONSTANTS = {
  SITE_URL: "{{ SITE_URL }}",
  ORGANIZATION_TOP_LEVEL_PART_OF_ID: 0,
  ORGANIZATION_TOP_LEVEL_PART_OF_LABEL: "None (Top Level Organization)",
  ORGANIZATION_TYPES: {{ ORGANIZATION_TYPES }},
  DATA_SOURCE_TYPES: {{ DATA_SOURCE_TYPES }}
};

{% endautoescape %}