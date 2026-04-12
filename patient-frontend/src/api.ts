let accessToken = '';

export function setToken(token: string) {
  accessToken = token;
  localStorage.setItem('jhe_access_token', token);
}

export function getToken() {
  if (!accessToken) {
    accessToken = localStorage.getItem('jhe_access_token') || '';
  }
  return accessToken;
}

export function setRefreshToken(token: string) {
  localStorage.setItem('jhe_refresh_token', token);
}

export function getRefreshToken() {
  return localStorage.getItem('jhe_refresh_token') || '';
}

export function setClientId(clientId: string) {
  localStorage.setItem('jhe_client_id', clientId);
}

export function getClientId() {
  return localStorage.getItem('jhe_client_id') || '';
}

export function clearAuth() {
  accessToken = '';
  localStorage.removeItem('jhe_access_token');
  localStorage.removeItem('jhe_refresh_token');
  localStorage.removeItem('jhe_client_id');
}

export async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  const clientId = getClientId();
  if (!refreshToken || !clientId) return false;

  try {
    const res = await fetch('/o/token/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'refresh_token',
        refresh_token: refreshToken,
        client_id: clientId,
      }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    if (data.access_token) {
      setToken(data.access_token);
      if (data.refresh_token) setRefreshToken(data.refresh_token);
      return true;
    }
    return false;
  } catch {
    return false;
  }
}

async function apiFetch(path: string, options: RequestInit = {}) {
  // Ensure we have a token
  if (!accessToken) {
    accessToken = localStorage.getItem('jhe_access_token') || '';
  }

  let res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...options.headers,
    },
  });

  // If 401, try refreshing the token
  if (res.status === 401) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      res = await fetch(path, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${accessToken}`,
          ...options.headers,
        },
      });
    }
  }

  if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
  return res.json();
}

export const api = {
  exchangeToken: (code: string, clientId: string, codeVerifier: string) =>
    fetch('/o/token/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'authorization_code',
        code,
        client_id: clientId,
        code_verifier: codeVerifier,
        redirect_uri: `${window.location.origin}/auth/callback`,
      }),
    }).then(r => r.json()),

  getMe: () => apiFetch('/api/v1/patients/me'),

  getConsents: (patientId: number) =>
    apiFetch(`/api/v1/patients/${patientId}/consents`),

  submitConsents: (patientId: number, data: any) =>
    apiFetch(`/api/v1/patients/${patientId}/consents`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  getWearableRedirect: (patientId: number, studyId: number, dataSourceId: number, provider: string = 'oura') =>
    apiFetch(`/api/v1/patients/${patientId}/wearable-redirect`, {
      method: 'POST',
      body: JSON.stringify({
        study_id: studyId,
        data_source_id: dataSourceId,
        provider: provider,
        redirect_uri: `${window.location.origin}/ow/complete`,
      }),
    }),

  getWearableStatus: (patientId: number) =>
    apiFetch(`/api/v1/patients/${patientId}/wearable-status`),

  revokeConsent: (patientId: number, studyId: number) =>
    apiFetch(`/api/v1/patients/${patientId}/consents/${studyId}`, {
      method: 'DELETE',
    }),
};
