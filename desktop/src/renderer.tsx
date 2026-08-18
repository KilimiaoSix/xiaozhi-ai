import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { CameraMonitoringProvider } from './modules/features/camera-capture/context/CameraMonitoringProvider';
import { App } from './renderer/App';
import './renderer/styles.css';

const root = document.getElementById('root');

if (!root) {
  throw new Error('Renderer root element was not found.');
}

createRoot(root).render(
  <StrictMode>
    <CameraMonitoringProvider>
      <App />
    </CameraMonitoringProvider>
  </StrictMode>,
);
