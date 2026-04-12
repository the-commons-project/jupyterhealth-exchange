export function Header() {
  return (
    <div style={{
      background: '#fff',
      padding: '12px 32px',
      borderBottom: '3px solid #F37626',
      display: 'flex',
      alignItems: 'center',
    }}>
      <img
        src={`${import.meta.env.BASE_URL}jupyter-health-logo.png`}
        alt="JupyterHealth"
        style={{ height: 40 }}
      />
    </div>
  );
}
