import { useState } from 'react';
import { Header } from '../components/Header';

export function CompletePage() {
  const inviteLink = localStorage.getItem('inviteLink') || '';
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(inviteLink);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div style={{ minHeight: '100vh', background: '#fff', color: '#212529' }}>
      <Header />

      <div style={{ maxWidth: 520, margin: '0 auto', padding: '80px 24px', textAlign: 'center', fontFamily: '"Helvetica Neue", sans-serif' }}>
        <div style={{
          width: 64, height: 64, borderRadius: '50%', background: '#d1e7dd', color: '#198754',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 32, margin: '0 auto 20px',
        }}>&#10003;</div>
        <h1 style={{ fontSize: 28, fontWeight: 700, marginBottom: 12, color: '#198754' }}>
          Successfully Connected
        </h1>
        <p style={{ color: '#6c757d', fontSize: 16, lineHeight: 1.6 }}>
          Your Oura Ring is now connected and data sharing is active.
          Your health data will be synced automatically with the study.
        </p>

        <div style={{
          background: '#fff3cd', border: '1px solid #ffecb5', borderRadius: 8,
          padding: '16px 20px', marginTop: 28, textAlign: 'left',
        }}>
          <div style={{ fontWeight: 600, color: '#664d03', fontSize: 14, marginBottom: 6 }}>
            Important: Save your invite link
          </div>
          <p style={{ color: '#664d03', fontSize: 13, margin: '0 0 12px', lineHeight: 1.5 }}>
            Save the link below. You can use it at any time to review or revoke your
            data sharing consent.
          </p>
          {inviteLink && (
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <input
                readOnly
                value={inviteLink}
                style={{
                  flex: 1, padding: '8px 10px', fontSize: 12, border: '1px solid #ffecb5',
                  borderRadius: 4, background: '#fff', color: '#664d03', fontFamily: 'monospace',
                }}
                onClick={e => (e.target as HTMLInputElement).select()}
              />
              <button
                onClick={handleCopy}
                style={{
                  padding: '8px 14px', fontSize: 12, fontWeight: 600,
                  background: copied ? '#198754' : '#F37626', color: '#fff',
                  border: 'none', borderRadius: 4, cursor: 'pointer', whiteSpace: 'nowrap',
                }}
              >
                {copied ? 'Copied!' : 'Copy'}
              </button>
            </div>
          )}
        </div>

        <p style={{ color: '#adb5bd', fontSize: 14, marginTop: 24 }}>
          You may close this window.
        </p>
      </div>
    </div>
  );
}
