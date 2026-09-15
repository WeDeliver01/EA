"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { OutcomeBadge, PnlText } from "@/components/Badges";
import { Panel } from "@/components/Panel";
import { api } from "@/lib/api";
import { formatDateTime, formatMoney, formatNumber } from "@/lib/format";
import { usePrimaryAccount } from "@/lib/use-account";

export default function DashboardPage() {
  const { account } = usePrimaryAccount();
  const accountId = account?.id;

  const riskState = useQuery({
    queryKey: ["risk-state", accountId],
    queryFn: () => api.riskState(accountId!),
    enabled: accountId !== undefined,
  });
  const positions = useQuery({
    queryKey: ["positions", accountId, "OPEN"],
    queryFn: () => api.positions(accountId!, "OPEN"),
    enabled: accountId !== undefined,
  });
  const latestRuns = useQuery({
    queryKey: ["analysis-runs", accountId, "latest"],
    queryFn: () => api.analysisRuns({ account_id: accountId, limit: 5 }),
    enabled: accountId !== undefined,
  });
  const gateRejections = useQuery({
    queryKey: ["gate-rejections", accountId],
    queryFn: () => api.gateRejections({ account_id: accountId }),
    enabled: accountId !== undefined,
  });

  if (account === undefined) {
    return <p className="text-sm text-text-muted">Loading account…</p>;
  }

  const risk = riskState.data;
  const maxRejectionCount = Math.max(1, ...(gateRejections.data?.rejections.map((r) => r.count) ?? [1]));

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Panel title="Account">
        <dl className="grid grid-cols-2 gap-y-2 text-sm">
          <dt className="text-text-secondary">Balance</dt>
          <dd className="tabular text-right text-text-primary">
            {formatMoney(account.balance, account.currency)}
          </dd>
          <dt className="text-text-secondary">Equity</dt>
          <dd className="tabular text-right text-text-primary">
            {formatMoney(account.equity, account.currency)}
          </dd>
          <dt className="text-text-secondary">Today&apos;s P/L</dt>
          <dd className="text-right">
            <PnlText value={risk?.realised_pnl_today ?? "0"} currency={account.currency} />
          </dd>
          <dt className="text-text-secondary">Open risk</dt>
          <dd className="tabular text-right text-text-primary">
            {risk ? formatMoney(risk.open_risk, account.currency) : "—"}
          </dd>
          <dt className="text-text-secondary">Drawdown from peak</dt>
          <dd className="tabular text-right text-text-primary">
            {risk ? `${(Number(risk.current_drawdown_pct) * 100).toFixed(1)}%` : "—"}
          </dd>
          <dt className="text-text-secondary">Trades today</dt>
          <dd className="tabular text-right text-text-primary">{risk?.trades_today ?? "—"}</dd>
        </dl>
      </Panel>

      <Panel title="Open positions">
        {positions.data === undefined || positions.data.length === 0 ? (
          <p className="text-sm text-text-muted">No open positions.</p>
        ) : (
          <table className="w-full text-sm">
            <tbody>
              {positions.data.map((p) => (
                <tr key={p.id} className="border-t border-border first:border-t-0">
                  <td className="py-1.5 pr-2">{p.symbol}</td>
                  <td className="py-1.5 pr-2 tabular">{p.direction}</td>
                  <td className="py-1.5 pr-2 tabular text-right">{formatNumber(p.volume, 2)}</td>
                  <td className="py-1.5 text-right">
                    <PnlText value={p.unrealised_pnl} currency={account.currency} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <Link href="/positions" className="mt-3 inline-block text-xs text-accent">
          View all positions →
        </Link>
      </Panel>

      <Panel title="Latest decisions">
        {latestRuns.data === undefined || latestRuns.data.length === 0 ? (
          <p className="text-sm text-text-muted">No evaluations yet.</p>
        ) : (
          <ul className="divide-y divide-border">
            {latestRuns.data.map((run) => (
              <li key={run.id} className="flex items-center justify-between py-1.5 text-sm">
                <Link href={`/analysis/${run.id}`} className="text-text-primary hover:text-accent">
                  {run.symbol} · {run.timeframe}
                </Link>
                <span className="flex items-center gap-2">
                  <span className="tabular text-xs text-text-muted">
                    {formatDateTime(run.as_of)}
                  </span>
                  <OutcomeBadge outcome={run.outcome} />
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-text-muted">
          This is why the system feels alive when it isn&apos;t trading - every evaluation is
          recorded, WAIT included.
        </p>
      </Panel>

      <Panel title="Gate telemetry (7 days)">
        {gateRejections.data === undefined || gateRejections.data.rejections.length === 0 ? (
          <p className="text-sm text-text-muted">
            No rejections recorded - either nothing has run, or every evaluation has passed every
            gate.
          </p>
        ) : (
          <>
            <p className="mb-3 text-xs text-text-muted">
              {gateRejections.data.total_evaluations} evaluations, {gateRejections.data.trades}{" "}
              traded
            </p>
            <ul className="space-y-2">
              {gateRejections.data.rejections.map((r) => (
                <li key={r.code} className="text-xs">
                  <div className="mb-1 flex justify-between text-text-secondary">
                    <span>{r.code}</span>
                    <span className="tabular">
                      {r.count} ({(Number(r.pct) * 100).toFixed(0)}%)
                    </span>
                  </div>
                  <div className="h-1.5 w-full rounded bg-surface-raised">
                    <div
                      className="h-1.5 rounded bg-warning"
                      style={{ width: `${(r.count / maxRejectionCount) * 100}%` }}
                    />
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </Panel>
    </div>
  );
}
