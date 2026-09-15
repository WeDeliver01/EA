"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { OutcomeBadge } from "@/components/Badges";
import { OutcomeFilter, type OutcomeFilterValue } from "@/components/OutcomeFilter";
import { Panel } from "@/components/Panel";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import { usePrimaryAccount } from "@/lib/use-account";

export default function SignalsPage() {
  const { account } = usePrimaryAccount();
  const accountId = account?.id;
  const [outcome, setOutcome] = useState<OutcomeFilterValue>("ALL");

  const runs = useQuery({
    queryKey: ["analysis-runs", accountId, outcome],
    queryFn: () =>
      api.analysisRuns({
        account_id: accountId,
        outcome: outcome === "ALL" ? undefined : outcome,
        limit: 100,
      }),
    enabled: accountId !== undefined,
  });

  return (
    <Panel title="Decision feed">
      <div className="mb-4 flex items-center justify-between">
        <p className="text-xs text-text-muted">
          Every evaluation the strategy has run, WAIT included - this is why the system feels
          alive when it isn&apos;t trading.
        </p>
        <OutcomeFilter value={outcome} onChange={setOutcome} />
      </div>

      {runs.data === undefined || runs.data.length === 0 ? (
        <p className="text-sm text-text-muted">No evaluations for this filter.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-text-secondary">
              <th className="pb-2 pr-3 font-normal">Symbol</th>
              <th className="pb-2 pr-3 font-normal">Timeframe</th>
              <th className="pb-2 pr-3 font-normal">Regime</th>
              <th className="pb-2 pr-3 text-right font-normal">Score</th>
              <th className="pb-2 pr-3 text-right font-normal">Band</th>
              <th className="pb-2 pr-3 text-right font-normal">Time</th>
              <th className="pb-2 text-right font-normal">Outcome</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.map((run) => (
              <tr key={run.id} className="border-t border-border">
                <td className="py-1.5 pr-3">
                  <Link href={`/analysis/${run.id}`} className="text-text-primary hover:text-accent">
                    {run.symbol}
                  </Link>
                </td>
                <td className="py-1.5 pr-3 tabular text-text-secondary">{run.timeframe}</td>
                <td className="py-1.5 pr-3 text-text-secondary">{run.regime}</td>
                <td className="py-1.5 pr-3 tabular text-right text-text-primary">
                  {formatNumber(run.confluence_score, 1)}
                </td>
                <td className="py-1.5 pr-3 text-right text-text-secondary">{run.confluence_band}</td>
                <td className="py-1.5 pr-3 tabular text-right text-xs text-text-muted">
                  {formatDateTime(run.as_of)}
                </td>
                <td className="py-1.5 text-right">
                  <OutcomeBadge outcome={run.outcome} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
