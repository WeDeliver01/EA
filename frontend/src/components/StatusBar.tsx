"use client";

import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "@/lib/api";
import { formatMoney } from "@/lib/format";
import { usePrimaryAccount } from "@/lib/use-account";

function Chip({ label, value, bad }: { label: string; value: string; bad: boolean }) {
  return (
    <span
      className={clsx(
        "tabular rounded border px-2 py-1 text-xs",
        bad ? "border-short/40 bg-short/10 text-short" : "border-border bg-surface text-text-secondary"
      )}
    >
      <span className="text-text-muted">{label}</span> {value}
    </span>
  );
}

export function StatusBar() {
  const { account } = usePrimaryAccount();
  const accountId = account?.id;

  const detail = useQuery({
    queryKey: ["account", accountId],
    queryFn: () => api.account(accountId!),
    enabled: accountId !== undefined,
  });
  const riskState = useQuery({
    queryKey: ["risk-state", accountId],
    queryFn: () => api.riskState(accountId!),
    enabled: accountId !== undefined,
  });
  const systemStatus = useQuery({
    queryKey: ["system-status", accountId],
    queryFn: () => api.systemStatus(accountId),
    enabled: accountId !== undefined,
  });

  if (account === undefined || detail.data === undefined) {
    return (
      <div className="flex h-10 items-center border-b border-border bg-surface px-4 text-xs text-text-muted">
        Loading…
      </div>
    );
  }

  const d = detail.data;
  const risk = riskState.data;
  const sys = systemStatus.data;
  const todayPnl = risk ? Number(risk.realised_pnl_today) : 0;

  return (
    <div className="flex h-10 items-center gap-2 border-b border-border bg-surface px-4">
      <Chip label="AGENT" value={d.agent_connected ? "●" : "○ disconnected"} bad={!d.agent_connected} />
      <Chip
        label="TRADING"
        value={d.trading_enabled ? "ENABLED" : "DISABLED"}
        bad={!d.trading_enabled}
      />
      <Chip
        label="KILL SWITCH"
        value={d.kill_switch_active ? "ON" : "OFF"}
        bad={d.kill_switch_active}
      />
      <Chip
        label="RECON"
        value={
          sys?.unresolved_discrepancy_count !== undefined && sys.unresolved_discrepancy_count > 0
            ? `${sys.unresolved_discrepancy_count} UNRESOLVED`
            : "CLEAN"
        }
        bad={(sys?.unresolved_discrepancy_count ?? 0) > 0}
      />
      <Chip label="EQUITY" value={formatMoney(d.equity, d.currency)} bad={false} />
      <Chip
        label="TODAY"
        value={formatMoney(risk?.realised_pnl_today ?? "0", d.currency)}
        bad={todayPnl < 0}
      />
      {risk && <Chip label="DD" value={`${(Number(risk.current_drawdown_pct) * 100).toFixed(1)}%`} bad={false} />}
    </div>
  );
}
