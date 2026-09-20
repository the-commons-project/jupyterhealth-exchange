{% autoescape off %}

const redirectUri = window.location.origin + "{{ OAUTH2_CALLBACK_PATH }}";
const authority = window.location.origin + "{{ OIDC_CLIENT_AUTHORITY_PATH }}";

window.OIDCSettings = {
  authority: authority,
  client_id: "{{ OIDC_CLIENT_ID }}",
  // silentRequestTimeoutInSeconds: 600,
  redirect_uri: redirectUri,
  extraQueryParams: {},
  response_mode: "query",
};

const CONSTANTS = {
  JHE_VERSION: "{{ JHE_VERSION }}",
  ORGANIZATION_TOP_LEVEL_PART_OF_ID: 0,
  ORGANIZATION_TOP_LEVEL_PART_OF_LABEL: "None (Top Level Organization)",
  ORGANIZATION_TYPES: {{ ORGANIZATION_TYPES }},
  DATA_SOURCE_TYPES: {{ DATA_SOURCE_TYPES }},
  EHR_SUPPORTED_SCOPES: {{ EHR_SUPPORTED_SCOPES }},
  EHR_PATIENT_PORTAL_BASE_SCOPES: "{{ EHR_PATIENT_PORTAL_BASE_SCOPES }}",
  JHE_SETTING_VALUE_TYPES: {{ JHE_SETTING_VALUE_TYPES }},
  ROLE_PERMISSIONS: {{ ROLE_PERMISSIONS }},
  FHIR_RESOURCES: {{ FHIR_RESOURCES }}
};

{% endautoescape %}
