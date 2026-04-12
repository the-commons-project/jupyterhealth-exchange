import { useEffect, useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { parseInviteCode } from '../auth';
import { api, setToken, setRefreshToken, setClientId, getToken, refreshAccessToken } from '../api';
import { Header } from '../components/Header';

export function LandingPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [status, setStatus] = useState('Loading...');
  const [error, setError] = useState('');

  useEffect(() => {
    (async () => {
      try {
        // Read the invite token from ?invite= (preferred) or fall back to
        // the legacy ?code= and ?link= params for backward compat with old
        // emails. ?invite= is the canonical name because ?code= collides
        // with the OAuth2 authorization-code response query string.
        const link =
          searchParams.get('invite') ||
          searchParams.get('code') ||
          searchParams.get('link');

        // Try using stored tokens first (return visit)
        const existingToken = getToken();
        if (existingToken) {
          setStatus('Welcome back! Loading your consents...');
          try {
            const patient = await api.getMe();
            const consentData = await api.getConsents(patient.id);
            sessionStorage.setItem('patient', JSON.stringify(consentData.patient || patient));
            if (link) localStorage.setItem('inviteLink', window.location.href);
            return routeToNextPage(consentData, navigate);
          } catch {
            // Token expired — try refresh
            const refreshed = await refreshAccessToken();
            if (refreshed) {
              const patient = await api.getMe();
              const consentData = await api.getConsents(patient.id);
              sessionStorage.setItem('patient', JSON.stringify(consentData.patient || patient));
              if (link) localStorage.setItem('inviteLink', window.location.href);
              return routeToNextPage(consentData, navigate);
            }
            // Refresh failed — fall through to auth code flow
          }
        }

        // First visit: exchange auth code for tokens
        if (!link) {
          setError('No invite link provided and no active session. Please use the link from your email.');
          return;
        }

        localStorage.setItem('inviteLink', window.location.href);
        setStatus('Authenticating...');

        const { clientId, authCode, codeVerifier } = parseInviteCode(link);
        const tokenData = await api.exchangeToken(authCode, clientId, codeVerifier);

        if (tokenData.error) {
          setError(`Auth error: ${tokenData.error_description || tokenData.error}`);
          return;
        }

        // Store tokens for future visits
        setToken(tokenData.access_token);
        setClientId(clientId);
        if (tokenData.refresh_token) setRefreshToken(tokenData.refresh_token);

        setStatus('Authenticated! Loading your study...');
        const patient = await api.getMe();
        const consentData = await api.getConsents(patient.id);
        sessionStorage.setItem('patient', JSON.stringify(consentData.patient || patient));
        routeToNextPage(consentData, navigate);

      } catch (e: any) {
        setError(e.message);
      }
    })();
  }, [searchParams, navigate]);

  return (
    <div style={{ minHeight: '100vh', background: '#fff', color: '#212529' }}>
      <Header />
      <div style={{ maxWidth: 520, margin: '0 auto', padding: '80px 24px', textAlign: 'center', fontFamily: '"Helvetica Neue", sans-serif' }}>
        {error ? (
          <>
            <h2 style={{ color: '#842029', marginBottom: 12 }}>Error</h2>
            <p style={{ color: '#6c757d' }}>{error}</p>
          </>
        ) : (
          <>
            <div style={{
              width: 40, height: 40, border: '3px solid #F37626', borderTop: '3px solid transparent',
              borderRadius: '50%', animation: 'spin 1s linear infinite', margin: '0 auto 20px',
            }} />
            <p style={{ color: '#6c757d', fontSize: 16 }}>{status}</p>
            <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          </>
        )}
      </div>
    </div>
  );
}

function routeToNextPage(consentData: any, navigate: (path: string) => void) {
  const pendingStudies = consentData.studiesPendingConsent || consentData.studies_pending_consent || [];
  const existingStudies = consentData.studies || [];

  if (pendingStudies.length > 0) {
    sessionStorage.setItem('pendingConsents', JSON.stringify(pendingStudies));
    navigate('/ow/consent');
  } else if (existingStudies.length > 0) {
    sessionStorage.setItem('existingConsents', JSON.stringify(existingStudies));
    navigate('/ow/manage');
  } else {
    // No pending, no existing — might mean all consented already, go to manage
    navigate('/ow/manage');
  }
}
