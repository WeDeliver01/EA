"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { use } from "react";
import { DirectionBadge, OutcomeBadge } from "@/components/Badges";
import { Panel } from "@/components/Panel";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import clsx from "clsx";

export default function AnalysisDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);

  const run = useQuery({
    queryKey: ["analysis-run", id],
    queryFn: () => api.analysisRun(id),
  });

  if (run.isLoading) {
    return <p className="text-sm text-text-muted">Loading decision…</p>;
  }
  if (run.isError || run.data === undefined) {
    return <p className="text-sm text-short">Could not load this decision.</p>;
  }

  const data = run.data;

  return (
    <div className="space-y-4">
      <Panel title="Decision">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-lg font-medium text-text-primary">{data.symbol}</span>
          <span className="tabular text-sm text-text-secondary">{data.timeframe}</span>
          {data.direction !== null && <DirectionBadge direction={data.direction} />}
          <OutcomeBadge outcome={data.outcome} />
          <span className="text-sm text-text-secondary">
            {data.confluence_band} ({formatNumber(data.confluence_score, 1)})
          </span>
          <span className="text-sm text-text-secondary">{data.regime}</span>
          <span className="tabular ml-auto text-xs text-text-muted">
            {formatDateTime(data.as_of)}
          </span>
        </div>
        {data.direction !== null && (
          <dl className="mt-3 grid grid-cols-2 gap-y-1 text-sm sm:grid-cols-4">
            <dt className="text-text-secondary">Entry</dt>
            <dd className="tabular text-text-primary">
              {data.entry !== null ? formatNumber(data.entry, 2) : "—"}
            </dd>
            <dt className="text-text-secondary">Stop loss</dt>
            <dd className="tabular text-text-primary">
              {data.stop_loss !== null ? formatNumber(data.stop_loss, 2) : "—"}
            </dd>
          </dl>
        )}
      </Panel>

      <Panel title="Narrative">
        <p className="text-sm leading-relaxed text-text-primary">{data.narrative}</p>
      </Panel>

      <Panel title="Evidence">
        {data.evidence.length === 0 ? (
          <p className="text-sm text-text-muted">No evidence recorded.</p>
        ) : (
          <ul className="divide-y divide-border">
            {data.evidence.map((item, i) => (
              <li key={i} className="py-2">
                <div className="flex items-center gap-3 text-sm">
                  <span
                    className={clsx(
                      "tabular text-xs",
                      item.present ? "text-long" : "text-text-muted"
                    )}
                  >
                    {item.present ? "PRESENT" : "ABSENT"}
                  </span>
                  <span className="text-text-primary">{item.type}</span>
                  {item.direction !== null && (
                    <span className="text-xs text-text-secondary">{item.direction}</span>
                  )}
                  <span className="tabular ml-auto text-xs text-text-secondary">
                    weight {formatNumber(item.weight, 2)} · score {formatNumber(item.score, 2)}
                  </span>
                </div>
                {Object.keys(item.detail).length > 0 && (
                  <details className="mt-1">
                    <summary className="cursor-pointer text-xs text-text-muted">detail</summary>
                    <pre className="mt-1 overflow-x-auto rounded bg-surface-raised p-2 text-xs text-text-secondary">
                      {JSON.stringify(item.detail, null, 2)}
                    </pre>
                  </details>
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Gates">
        {data.gates.length === 0 ? (
          <p className="text-sm text-text-muted">No gates evaluated.</p>
        ) : (
          <ul className="divide-y divide-border">
            {data.gates.map((gate, i) => (
              <li key={i} className="py-2">
                <div className="flex items-center gap-3 text-sm">
                  <span
                    className={clsx(
                      "tabular text-xs",
                      gate.passed ? "text-long" : "text-warning"
                    )}
                  >
                    {gate.passed ? "PASS" : "FAIL"}
                  </span>
                  <span className="text-text-primary">{gate.code}</span>
                </div>
                {Object.keys(gate.detail).length > 0 && (
                  <details className="mt-1">
                    <summary className="cursor-pointer text-xs text-text-muted">detail</summary>
                    <pre className="mt-1 overflow-x-auto rounded bg-surface-raised p-2 text-xs text-text-secondary">
                      {JSON.stringify(gate.detail, null, 2)}
                    </pre>
                  </details>
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Market snapshot">
        <details>
          <summary className="cursor-pointer text-xs text-text-muted">
            raw MarketState at decision time
          </summary>
          <pre className="mt-2 max-h-96 overflow-auto rounded bg-surface-raised p-2 text-xs text-text-secondary">
            {JSON.stringify(data.market_snapshot, null, 2)}
          </pre>
        </details>
        <p className="mt-3 text-xs text-text-muted">
          Candle chart and replay-diff are not built yet - see the ADR for what&apos;s deferred.
        </p>
      </Panel>

      <Link href="/signals" className="inline-block text-xs text-accent">
        ← Back to decision feed
      </Link>
    </div>
  );
}
