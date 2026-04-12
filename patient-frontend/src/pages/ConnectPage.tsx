import { useState } from 'react';
import { api } from '../api';

export function ConnectPage() {
  const patient = JSON.parse(sessionStorage.getItem('patient') || '{}');
  const studies = JSON.parse(sessionStorage.getItem('studies') || '[]');
  const [connecting, setConnecting] = useState(false);

  const handleConnect = async () => {
    setConnecting(true);
    const study = studies[0];
    const dataSources = study.dataSources || study.data_sources || [];
    const dataSource = dataSources[0];
    if (!study || !dataSource) { alert('No study or data source configured.'); setConnecting(false); return; }
    const result = await api.getWearableRedirect(patient.id, study.id, dataSource.id);
    window.location.href = result.authorizationUrl || result.authorization_url;
  };

  return (
    <div style={{ padding: 40, maxWidth: 600 }}>
      <h1>Connect Your Wearable</h1>
      <p>Connect your Oura Ring to share health data with the study.</p>
      <button onClick={handleConnect} disabled={connecting} style={{ padding: '12px 24px', fontSize: 16 }}>
        {connecting ? 'Redirecting...' : 'Connect Oura Ring'}
      </button>
    </div>
  );
}
