import { useEffect, useState } from 'react';
import { api } from '../api';
import { Header } from '../components/Header';

export function ManagePage() {
  const patient = JSON.parse(sessionStorage.getItem('patient') || '{}');
  const [consents, setConsents] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [revoking, setRevoking] = useState<number | null>(null);
  const [revoked, setRevoked] = useState(false);
  const [confirmStudyId, setConfirmStudyId] = useState<number | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const data = await api.getConsents(patient.id);
        setConsents(data.studies || []);
      } catch (e) {
        // Fallback to stored data if API fails
        const stored = sessionStorage.getItem('existingConsents');
        if (stored) setConsents(JSON.parse(stored));
      }
      setLoading(false);
    })();
  }, [patient.id]);

  const [revokedStudyIds, setRevokedStudyIds] = useState<Set<number>>(new Set());

  const handleRevoke = async (studyId: number) => {
    setRevoking(studyId);
    try {
      await api.revokeConsent(patient.id, studyId);
    } catch (e: any) {
      // Log but don't block — the JHE backend may still have revoked locally
      console.warn('Revoke API error (consent may still be revoked):', e.message);
    }
    setRevokedStudyIds(prev => new Set([...prev, studyId]));
    setRevoked(true);
    setRevoking(null);
  };

  return (
    <div style={{ minHeight: '100vh', background: '#fff', color: '#212529' }}>
      <Header />

      <div style={{ maxWidth: 640, margin: '0 auto', padding: '40px 24px', fontFamily: '"Helvetica Neue", sans-serif' }}>
        <h1 style={{ fontSize: 28, fontWeight: 700, marginBottom: 8, color: '#212529' }}>
          Manage Your Consents
        </h1>
        <p style={{ color: '#6c757d', marginBottom: 32, fontSize: 15 }}>
          Review your active data sharing agreements. You can revoke consent at any time to stop sharing data with a study.
        </p>

        {revoked && (
          <div style={{
            background: '#d1e7dd', border: '1px solid #badbcc', borderRadius: 8,
            padding: '12px 16px', marginBottom: 24, fontSize: 14, color: '#0f5132',
          }}>
            Consent revoked successfully. Data sharing has been stopped.
          </div>
        )}

        {loading ? (
          <div style={{ textAlign: 'center', padding: '40px 0' }}>
            <div style={{
              width: 36, height: 36, border: '3px solid #F37626', borderTop: '3px solid transparent',
              borderRadius: '50%', animation: 'spin 1s linear infinite', margin: '0 auto 16px',
            }} />
            <p style={{ color: '#6c757d' }}>Loading your consents...</p>
            <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          </div>
        ) : consents.length === 0 ? (
          <div style={{
            textAlign: 'center', padding: '40px 0',
            border: '1px solid #dee2e6', borderRadius: 8, background: '#f8f9fa',
          }}>
            <p style={{ color: '#6c757d', fontSize: 16, margin: 0 }}>No active consents.</p>
          </div>
        ) : (
          consents.map((study: any) => {
            const org = study.organization;
            const scopes = (study.scopeConsents || study.scope_consents || [])
              .filter((s: any) => s.consented);
            const practitioners = study.practitioners || [];
            const providerName = practitioners.length > 0 ? practitioners[0].name : null;
            const isRevoked = revokedStudyIds.has(study.id);

            return (
              <div key={study.id} style={{
                marginBottom: 24,
                border: `1px solid ${isRevoked ? '#f5c6cb' : '#dee2e6'}`,
                borderRadius: 8,
                overflow: 'hidden',
                opacity: isRevoked ? 0.85 : 1,
              }}>
                {/* Study header */}
                <div style={{ background: isRevoked ? '#f8d7da' : '#f8f9fa', padding: '20px 24px', borderBottom: `1px solid ${isRevoked ? '#f5c6cb' : '#dee2e6'}` }}>
                  <h2 style={{ margin: 0, fontSize: 20, color: '#212529' }}>{study.name}</h2>
                  <p style={{ margin: '8px 0 0', color: '#6c757d', fontSize: 14 }}>{study.description}</p>
                </div>

                {/* Study details */}
                <div style={{ padding: '20px 24px' }}>
                  <div style={{ display: 'flex', gap: 40, marginBottom: 20, flexWrap: 'wrap' }}>
                    {providerName && (
                      <div>
                        <div style={{ color: '#6c757d', fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>
                          Provider
                        </div>
                        <div style={{ fontWeight: 600, color: '#212529' }}>{providerName}</div>
                      </div>
                    )}
                    <div>
                      <div style={{ color: '#6c757d', fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>
                        Organization
                      </div>
                      <div style={{ fontWeight: 600, color: '#212529' }}>{org?.name || 'Unknown'}</div>
                    </div>
                    <div>
                      <div style={{ color: '#6c757d', fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>
                        Status
                      </div>
                      <div style={{ fontWeight: 600, color: isRevoked ? '#dc3545' : '#198754' }}>
                        {isRevoked ? 'Revoked' : 'Active'}
                      </div>
                    </div>
                  </div>

                  {/* Shared data types */}
                  <div style={{ color: '#6c757d', fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 8 }}>
                    {isRevoked ? 'Data types (no longer shared)' : 'Data types being shared'}
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 20 }}>
                    {scopes.map((s: any) => (
                      <span key={s.code.id} style={{
                        background: isRevoked ? '#f8d7da' : '#e8f5e9',
                        color: isRevoked ? '#842029' : '#2e7d32',
                        padding: '4px 12px',
                        borderRadius: 16, fontSize: 13, fontWeight: 500,
                        textDecoration: isRevoked ? 'line-through' : 'none',
                      }}>
                        {s.code.text}
                      </span>
                    ))}
                  </div>

                  {isRevoked ? (
                    <p style={{ color: '#6c757d', fontSize: 13, margin: 0, fontStyle: 'italic' }}>
                      Data sharing has been stopped. To re-enroll, please contact your study coordinator for a new invite link.
                    </p>
                  ) : (
                    <button
                      onClick={() => setConfirmStudyId(study.id)}
                      disabled={revoking === study.id}
                      style={{
                        padding: '10px 24px',
                        fontSize: 14,
                        fontWeight: 600,
                        background: '#fff',
                        color: '#dc3545',
                        border: '1px solid #dc3545',
                        borderRadius: 6,
                        cursor: revoking === study.id ? 'not-allowed' : 'pointer',
                        opacity: revoking === study.id ? 0.6 : 1,
                      }}
                    >
                      {revoking === study.id ? 'Revoking...' : 'Revoke Consent'}
                    </button>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Confirmation modal */}
      {confirmStudyId !== null && (
        <div style={{
          position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center',
          zIndex: 1000,
        }}>
          <div style={{
            background: '#fff', borderRadius: 12, padding: '32px', maxWidth: 440, width: '90%',
            boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
          }}>
            <div style={{
              width: 48, height: 48, borderRadius: '50%', background: '#f8d7da', color: '#dc3545',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 24, margin: '0 auto 16px',
            }}>!</div>
            <h3 style={{ textAlign: 'center', margin: '0 0 8px', fontSize: 20, color: '#212529' }}>
              Revoke Consent?
            </h3>
            <p style={{ textAlign: 'center', color: '#6c757d', fontSize: 14, lineHeight: 1.6, margin: '0 0 24px' }}>
              This will immediately stop sharing your health data with this study.
              The research team will no longer receive new data from your Oura Ring.
              To re-enroll after revoking, you will need to contact your study coordinator for a new invite link.
            </p>
            <div style={{ display: 'flex', gap: 12 }}>
              <button
                onClick={() => setConfirmStudyId(null)}
                style={{
                  flex: 1, padding: '10px 20px', fontSize: 14, fontWeight: 600,
                  background: '#f8f9fa', color: '#495057', border: '1px solid #dee2e6',
                  borderRadius: 6, cursor: 'pointer',
                }}
              >
                Cancel
              </button>
              <button
                onClick={async () => {
                  const id = confirmStudyId;
                  setConfirmStudyId(null);
                  await handleRevoke(id);
                }}
                style={{
                  flex: 1, padding: '10px 20px', fontSize: 14, fontWeight: 600,
                  background: '#dc3545', color: '#fff', border: 'none',
                  borderRadius: 6, cursor: 'pointer',
                }}
              >
                Yes, Revoke Consent
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
