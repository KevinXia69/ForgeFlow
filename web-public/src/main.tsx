import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { WorkbenchErrorBoundary } from "./ErrorBoundary";
import { LanguageProvider } from "./i18n";
import "@fontsource-variable/geist";
import "@fontsource-variable/lora";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");
createRoot(root).render(
  <StrictMode>
    <LanguageProvider>
      <WorkbenchErrorBoundary><App /></WorkbenchErrorBoundary>
    </LanguageProvider>
  </StrictMode>,
);
