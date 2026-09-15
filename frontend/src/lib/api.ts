"use client";

import { clearTokens, getAccessToken, getRefreshToken, storeTokens, type TokenPair } from "./auth";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message);
  }
}

let refreshInFlight: Promise<boolean> | null = null;

async function refreshTokens(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (refreshToken === null) return false;
  const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!response.ok) return false;
  storeTokens((await response.json()) as TokenPair);
  return true;
}

/**
 * A single in-flight refresh at a time - several 401s arriving together
 * (e.g. the dashboard's several polled queries) must not each kick off
 * their own refresh and race to rotate the same refresh token, which
 * would invalidate all but one of them (`app/api/v1/auth.py`'s rotation).
 */
function refreshOnce(): Promise<boolean> {
  if (refreshInFlight === null) {
    refreshInFlight = refreshTokens().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const accessToken = getAccessToken();
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");
  if (accessToken !== null) headers.set("Authorization", `Bearer ${accessToken}`);

  let response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });

  if (response.status === 401 && accessToken !== null) {
    const refreshed = await refreshOnce();
    if (refreshed) {
      headers.set("Authorization", `Bearer ${getAccessToken()}`);
      response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
    } else {
      clearTokens();
      if (typeof window !== "undefined") window.location.href = "/login";
      throw new ApiError(401, "session expired");
    }
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, body.detail ?? response.statusText);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// ---- domain types (mirrors app/api/v1/*.py - every Decimal/UUID/datetime
// crosses the wire as a string, deliberately: SPEC-09 §8, "the backend is
// the single source of truth," no client-side re-derivation of numbers) ----

export interface Account {
  id: string;
  label: string;
  broker: string;
  environment: string;
  currency: string;
  balance: string;
  equity: string;
  trading_enabled: boolean;
  kill_switch_active: boolean;
}

export interface AccountDetail extends Account {
  mt5_login: string | number;
  leverage: number;
  agent_connected: boolean;
}

export interface RiskState {
  as_of: string;
  realised_pnl_today: string;
  realised_pnl_week: string;
  open_risk: string;
  trades_today: number;
  open_position_count: number;
  consecutive_losses: number;
  peak_equity: string;
  current_drawdown_pct: string;
  trading_enabled: boolean;
  kill_switch_active: boolean;
}

export interface AnalysisRunSummary {
  id: string;
  account_id: string;
  instrument_id: string;
  symbol: string;
  timeframe: string;
  as_of: string;
  mode: string;
  regime: string;
  outcome: "TRADE" | "WAIT";
  confluence_score: string;
  confluence_band: string;
  created_at: string;
}

export interface EvidenceItem {
  type: string;
  direction: string | null;
  present: boolean;
  weight: string;
  score: string;
  detail: Record<string, unknown>;
}

export interface GateItem {
  code: string;
  passed: boolean;
  detail: Record<string, unknown>;
}

export interface AnalysisRunDetail extends AnalysisRunSummary {
  strategy_version_id: string;
  direction: string | null;
  entry: string | null;
  stop_loss: string | null;
  narrative: string;
  engine_duration_ms: number;
  market_snapshot: Record<string, unknown>;
  evidence: EvidenceItem[];
  gates: GateItem[];
}

export interface SignalSummary {
  id: string;
  account_id: string;
  instrument_id: string;
  symbol: string;
  direction: "LONG" | "SHORT";
  entry: string;
  stop_loss: string;
  confluence_score: string;
  created_at: string;
}

export interface Position {
  id: string;
  broker_position_id: string;
  account_id: string;
  symbol: string;
  direction: "LONG" | "SHORT";
  volume: string;
  entry_price: string;
  stop_loss: string | null;
  take_profit: string | null;
  opened_at: string;
  status: string;
  signal_id: string | null;
  initial_risk: string;
  realised_pnl: string;
  unrealised_pnl: string;
  breakeven_moved: boolean;
  partials_taken: number;
}

export interface TradeSummary {
  id: string;
  account_id: string;
  instrument_id: string;
  symbol: string;
  direction: string;
  entry_time: string;
  exit_time: string;
  entry_price: string;
  exit_price: string;
  volume: string;
  net_pnl: string;
  r_multiple: string;
  exit_reason: string;
}

export interface GateRejectionsSummary {
  period: { from: string; to: string };
  total_evaluations: number;
  trades: number;
  rejections: { code: string; count: number; pct: string }[];
}

export interface SystemStatus {
  git_sha: string;
  environment: string;
  global_trading_enabled: boolean;
  scheduler_enabled: boolean;
  streams: { bars_closed_length: number; intents_pending_length: number };
  agent_connected?: boolean;
  unresolved_discrepancy_count?: number;
}

export const api = {
  login: (email: string, password: string) =>
    apiFetch<TokenPair>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  me: () => apiFetch<{ id: string; email: string; display_name: string; role: string }>("/auth/me"),
  logout: async () => {
    const refreshToken = getRefreshToken();
    if (refreshToken !== null) {
      await apiFetch("/auth/logout", {
        method: "POST",
        body: JSON.stringify({ refresh_token: refreshToken }),
      }).catch(() => undefined);
    }
    clearTokens();
  },

  accounts: () => apiFetch<Account[]>("/accounts"),
  account: (id: string) => apiFetch<AccountDetail>(`/accounts/${id}`),
  riskState: (id: string) => apiFetch<RiskState>(`/accounts/${id}/risk-state`),

  analysisRuns: (params: { account_id?: string; outcome?: string; limit?: number } = {}) =>
    apiFetch<AnalysisRunSummary[]>(`/analysis-runs${toQuery(params)}`),
  analysisRun: (id: string) => apiFetch<AnalysisRunDetail>(`/analysis-runs/${id}`),

  signals: (params: { account_id?: string; limit?: number } = {}) =>
    apiFetch<SignalSummary[]>(`/signals${toQuery(params)}`),

  positions: (accountId: string, status?: string) =>
    apiFetch<Position[]>(`/positions${toQuery({ account_id: accountId, status })}`),

  trades: (params: { account_id?: string; limit?: number } = {}) =>
    apiFetch<TradeSummary[]>(`/trades${toQuery(params)}`),

  gateRejections: (params: { account_id?: string } = {}) =>
    apiFetch<GateRejectionsSummary>(`/telemetry/gate-rejections${toQuery(params)}`),

  systemStatus: (accountId?: string) =>
    apiFetch<SystemStatus>(`/system/status${toQuery({ account_id: accountId })}`),
};

function toQuery(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined);
  if (entries.length === 0) return "";
  const search = new URLSearchParams(entries.map(([k, v]) => [k, String(v)]));
  return `?${search.toString()}`;
}
