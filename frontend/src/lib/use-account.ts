"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

/** Single-account MVP (matches the backend's own single-tenant scope,
 * `SCHEDULER_ACCOUNT_ID` etc.) - the first account is "the" account. */
export function usePrimaryAccount() {
  const query = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  return { ...query, account: query.data?.[0] };
}
