"use client";

import { useQuery } from "@tanstack/react-query";
import { DirectionBadge, PnlText } from "@/components/Badges";
import { Panel } from "@/components/Panel";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import { usePrimaryAccount } from "@/lib/use-account";

export default function TradesPage() {
  const { account } = usePrimaryAccount();
  const accountId = account?.id;

  const trades = useQuery({
    queryKey: ["trades", accountId],
    queryFn: () => api.trades({ account_id: accountId, limit: 200 }),
    enabled: accountId !== undefined,
  });

  return (
    <Panel title="Trade history">
      <p className="mb-4 text-xs text-text-muted">Closed trades, most recent first.</p>

      {trades.data === undefined || trades.data.length === 0 ? (
        <p className="text-sm text-text-muted">No closed trades yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-text-secondary">
              <th className="pb-2 pr-3 font-normal">Symbol</th>
              <th className="pb-2 pr-3 font-normal">Direction</th>
              <th className="pb-2 pr-3 text-right font-normal">Volume</th>
              <th className="pb-2 pr-3 text-right font-normal">Entry</th>
              <th className="pb-2 pr-3 text-right font-normal">Exit</th>
              <th className="pb-2 pr-3 text-right font-normal">R</th>
              <th className="pb-2 pr-3 font-normal">Exit reason</th>
              <th className="pb-2 pr-3 text-right font-normal">Closed</th>
              <th className="pb-2 text-right font-normal">Net P/L</th>
            </tr>
          </thead>
          <tbody>
            {trades.data.map((t) => (
              <tr key={t.id} className="border-t border-border">
                <td className="py-1.5 pr-3">{t.symbol}</td>
                <td className="py-1.5 pr-3">
                  <DirectionBadge direction={t.direction} />
                </td>
                <td className="py-1.5 pr-3 tabular text-right">{formatNumber(t.volume, 2)}</td>
                <td className="py-1.5 pr-3 tabular text-right">{formatNumber(t.entry_price, 2)}</td>
                <td className="py-1.5 pr-3 tabular text-right">{formatNumber(t.exit_price, 2)}</td>
                <td className="py-1.5 pr-3 tabular text-right">{formatNumber(t.r_multiple, 2)}</td>
                <td className="py-1.5 pr-3 text-xs text-text-secondary">{t.exit_reason}</td>
                <td className="py-1.5 pr-3 tabular text-right text-xs text-text-muted">
                  {formatDateTime(t.exit_time)}
                </td>
                <td className="py-1.5 text-right">
                  <PnlText value={t.net_pnl} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
