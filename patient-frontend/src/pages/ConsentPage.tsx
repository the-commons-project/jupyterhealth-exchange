import { useState, useEffect } from 'react';
import { api } from '../api';
import { Header } from '../components/Header';

export function ConsentPage() {
  const patient = JSON.parse(sessionStorage.getItem('patient') || '{}');
  const studies = JSON.parse(sessionStorage.getItem('pendingConsents') || '[]');
  const [consented, setConsented] = useState<Record<string, boolean>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  // Derive the data source name from the first study for the button text.
  const firstStudy = studies[0];
  const firstDataSources = firstStudy?.dataSources || firstStudy?.data_sources || [];
  const deviceName = firstDataSources.length > 0 ? firstDataSources[0].name : 'Wearable Device';

  // Default all scopes to checked
  useEffect(() => {
    const defaults: Record<string, boolean> = {};
    for (const study of studies) {
      for (const sc of (study.pendingScopeConsents || [])) {
        defaults[`${study.id}-${sc.code.id}`] = true;
      }
    }
    setConsented(defaults);
  }, []);

  const handleSubmit = async () => {
    setSubmitting(true);
    setError('');
    try {
      // 1. Submit consents
      for (const study of studies) {
        const scopeConsents = (study.pendingScopeConsents || []).map((sc: any) => ({
          codingSystem: sc.code.codingSystem || sc.code.coding_system,
          codingCode: sc.code.codingCode || sc.code.coding_code,
          consented: consented[`${study.id}-${sc.code.id}`] ?? true,
        }));
        await api.submitConsents(patient.id, { studyScopeConsents: [{ studyId: study.id, scopeConsents }] });
      }

      // 2. Immediately redirect to wearable OAuth using the study's data source.
      // The DataSource carries provider_key (e.g. "oura") so we know which OW
      // provider to authorize without showing a generic provider picker.
      const study = studies[0];
      const dataSources = study.dataSources || study.data_sources || [];
      const dataSource = dataSources[0];

      if (study && dataSource) {
        const providerKey = dataSource.providerKey || dataSource.provider_key || 'oura';
        const result = await api.getWearableRedirect(patient.id, study.id, dataSource.id, providerKey);
        window.location.href = result.authorizationUrl || result.authorization_url;
      } else {
        setError('No data source configured for this study.');
        setSubmitting(false);
      }
    } catch (e: any) {
      setError(e.message);
      setSubmitting(false);
    }
  };

  return (
    <div style={{ minHeight: '100vh', background: '#fff', color: '#212529' }}>
      <Header />

      {/* Content */}
      <div style={{ maxWidth: 640, margin: '0 auto', padding: '40px 24px', fontFamily: '"Helvetica Neue", sans-serif' }}>
        <h1 style={{ fontSize: 28, fontWeight: 700, marginBottom: 8, color: '#212529' }}>
          Consent to Share Data
        </h1>
        <p style={{ color: '#6c757d', marginBottom: 32, fontSize: 15 }}>
          Review the study details below and confirm which health data you agree to share with the research team.
        </p>

        {studies.map((study: any) => {
          const org = study.organization;
          const dataSources = study.dataSources || study.data_sources || [];
          const dataSourceName = dataSources.length > 0 ? dataSources[0].name : 'Wearable Device';
          const practitioners = study.practitioners || [];
          const providerName = practitioners.length > 0 ? practitioners[0].name : null;

          return (
            <div key={study.id} style={{
              marginBottom: 32,
              border: '1px solid #dee2e6',
              borderRadius: 8,
              overflow: 'hidden',
            }}>
              {/* Study header */}
              <div style={{ background: '#f8f9fa', padding: '20px 24px', borderBottom: '1px solid #dee2e6' }}>
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
                      Data Source
                    </div>
                    <div style={{ fontWeight: 600, color: '#212529' }}>{dataSourceName}</div>
                  </div>
                </div>

                {/* Scopes */}
                <div style={{ color: '#6c757d', fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 8 }}>
                  Data types to share
                </div>
                <div style={{ border: '1px solid #dee2e6', borderRadius: 6 }}>
                  {(study.pendingScopeConsents || []).map((sc: any, i: number, arr: any[]) => {
                    const isChecked = consented[`${study.id}-${sc.code.id}`] ?? true;
                    return (
                      <label key={sc.code.id} style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 12,
                        padding: '10px 14px',
                        cursor: 'pointer',
                        borderBottom: i < arr.length - 1 ? '1px solid #dee2e6' : 'none',
                        background: isChecked ? '#fff7ed' : '#fff',
                      }}>
                        <div
                          onClick={() => setConsented(prev => ({
                            ...prev,
                            [`${study.id}-${sc.code.id}`]: !isChecked
                          }))}
                          style={{
                            width: 20, height: 20, borderRadius: 4, flexShrink: 0,
                            border: isChecked ? 'none' : '2px solid #ced4da',
                            background: isChecked ? '#F37626' : '#fff',
                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                            cursor: 'pointer',
                          }}
                        >
                          {isChecked && (
                            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                              <path d="M2 7l3.5 3.5L12 4" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
                            </svg>
                          )}
                        </div>
                        <span style={{ color: '#212529', fontSize: 15 }}>{sc.code.text}</span>
                      </label>
                    );
                  })}
                </div>
              </div>
            </div>
          );
        })}

        {error && (
          <div style={{ background: '#f8d7da', color: '#842029', padding: '12px 16px', borderRadius: 6, marginBottom: 16, fontSize: 14 }}>
            {error}
          </div>
        )}

        {submitting ? (
          <div style={{ textAlign: 'center', padding: '20px 0' }}>
            <div style={{
              width: 36, height: 36, border: '3px solid #F37626', borderTop: '3px solid transparent',
              borderRadius: '50%', animation: 'spin 1s linear infinite', margin: '0 auto 16px',
            }} />
            <p style={{ color: '#F37626', fontWeight: 600, fontSize: 16, margin: '0 0 4px' }}>
              Connecting to {deviceName}...
            </p>
            <p style={{ color: '#6c757d', fontSize: 13, margin: 0 }}>
              You will be redirected to authorize data sharing.
            </p>
            <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          </div>
        ) : (
          <>
            <button
              onClick={handleSubmit}
              style={{
                padding: '14px 32px',
                fontSize: 16,
                fontWeight: 600,
                background: '#F37626',
                color: 'white',
                border: 'none',
                borderRadius: 6,
                cursor: 'pointer',
                width: '100%',
                fontFamily: '"Helvetica Neue", sans-serif',
              }}
            >
              Consent &amp; Connect {deviceName}
            </button>
            <p style={{ color: '#6c757d', fontSize: 13, textAlign: 'center', marginTop: 12 }}>
              You will be redirected to authorize data sharing.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
