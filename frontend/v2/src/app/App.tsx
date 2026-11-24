import { BrowserRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { AppRouter } from "@/app/router";
import { IconDefinitions } from "@/shared/components/Icon";

export function App() {
  return (
    <BrowserRouter>
      <AppProviders>
        <IconDefinitions />
        <AppRouter />
      </AppProviders>
    </BrowserRouter>
  );
}
