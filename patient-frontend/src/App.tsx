import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { LandingPage } from './pages/LandingPage';
import { ConsentPage } from './pages/ConsentPage';
import { ConnectPage } from './pages/ConnectPage';
import { CompletePage } from './pages/CompletePage';
import { ManagePage } from './pages/ManagePage';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/ow" element={<LandingPage />} />
        <Route path="/ow/consent" element={<ConsentPage />} />
        <Route path="/ow/connect" element={<ConnectPage />} />
        <Route path="/ow/complete" element={<CompletePage />} />
        <Route path="/ow/manage" element={<ManagePage />} />
      </Routes>
    </BrowserRouter>
  );
}
