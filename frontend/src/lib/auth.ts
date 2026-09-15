"use client";

/**
 * Token storage and the refresh dance. `localStorage`, not an httpOnly
 * cookie - this is a single-operator internal tool (per the ADR), not a
 * multi-tenant product, so the simpler client-storage approach is an
 * accepted tradeoff rather than the hardened default a public product
 * would need.
 */

const ACCESS_TOKEN_KEY = "dt_access_token";
const REFRESH_TOKEN_KEY = "dt_refresh_token";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

export function storeTokens(tokens: TokenPair): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token);
  localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
}

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

export function isAuthenticated(): boolean {
  return getAccessToken() !== null;
}
