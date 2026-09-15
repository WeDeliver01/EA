"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export function AppQueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // SPEC-09 §6: real-time via WebSocket is deferred (see the
            // ADR) - polling stands in for it until that's built, short
            // enough that the dashboard still feels live.
            refetchInterval: 5000,
            staleTime: 2000,
            retry: 1,
          },
        },
      })
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
