import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/tokens.css";
import "./styles.css";
import "./styles/workbench.css";
import { AppProviders } from "./app/providers";
import { AppRouter } from "./app/router";
import { applyDocumentBrand } from "./brand";

applyDocumentBrand(document);

const rootElement = document.querySelector<HTMLDivElement>("#app");
if (!rootElement) throw new Error("Application root is missing.");

createRoot(rootElement).render(
  <StrictMode>
    <AppProviders>
      <AppRouter />
    </AppProviders>
  </StrictMode>,
);
