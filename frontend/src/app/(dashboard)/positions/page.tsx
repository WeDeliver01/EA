"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import clsx from "clsx";
import { DirectionBadge, PnlText } from "@/components/Badges";
import { Panel } from "@/components/Panel";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import { usePrimaryAccount } from "@/lib/use-account";

const STATUS_OPTIONS = ["OPEN", "CLOSED", "ALL"] as const;
type StatusFilter = (typeof STATUS_OPTIONS)[number];

export default function PositionsPage() {
  const { account } = usePrimaryAccount();
  const accountId = account?.id;
  const [status, setStatus] = useState<StatusFilter>("OPEN");

  const positions = useQuery({
    queryKey: ["positions", accountId, status],
    queryFn: () => api.positions(accountId!, status === "ALL" ? undefined : status),
    enabled: accountId !== undefined,
  });

  return (
    <Panel title="Positions">
      <div className="mb-4 flex items-center justify-between">
        <p className="text-xs text-text-muted">Open positions and their closed history.</p>
        <div className="flex gap-1">
          {STATUS_OPTIONS.map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setStatus(option)}
              className={clsx(
                "rounded px-2 py-1 text-xs font-medium",
                status === option
                  ? "bg-accent/20 text-accent"
                  : "text-text-secondary hover:text-text-primary"
              )}
            >
              {option}
            </button>
          ))}
        </div>
      </div>

      {positions.data === undefined || positions.data.length === 0 ? (
        <p className="text-sm text-text-muted">No positions for this filter.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-text-secondary">
              <th className="pb-2 pr-3 font-normal">Symbol</th>
              <th className="pb-2 pr-3 font-normal">Direction</th>
              <th className="pb-2 pr-3 text-right font-normal">Volume</th>
              <th className="pb-2 pr-3 text-right font-normal">Entry</th>
              <th className="pb-2 pr-3 text-right font-normal">SL</th>
              <th className="pb-2 pr-3 text-right font-normal">TP</th>
              <th className="pb-2 pr-3 text-right font-normal">Opened</th>
              <th className="pb-2 pr-3 text-right font-normal">Status</th>
              <th className="pb-2 text-right font-normal">P/L</th>
            </tr>
          </thead>
          <tbody>
            {positions.data.map((p) => (
              <tr key={p.id} className="border-t border-border">
                <td className="py-1.5 pr-3">{p.symbol}</td>
                <td className="py-1.5 pr-3">
                  <DirectionBadge direction={p.direction} />
                </td>
                <td className="py-1.5 pr-3 tabular text-right">{formatNumber(p.volume, 2)}</td>
                <td className="py-1.5 pr-3 tabular text-right">{formatNumber(p.entry_price, 2)}</td>
                <td className="py-1.5 pr-3 tabular text-right text-text-secondary">
                  {p.stop_loss !== null ? formatNumber(p.stop_loss, 2) : "—"}
                </td>
                <td className="py-1.5 pr-3 tabular text-right text-text-secondary">
                  {p.take_profit !== null ? formatNumber(p.take_profit, 2) : "—"}
                </td>
                <td className="py-1.5 pr-3 tabular text-right text-xs text-text-muted">
                  {formatDateTime(p.opened_at)}
                </td>
                <td className="py-1.5 pr-3 text-right text-xs text-text-secondary">{p.status}</td>
                <td className="py-1.5 text-right">
                  <PnlText value={p.status === "OPEN" ? p.unrealised_pnl : p.realised_pnl} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
