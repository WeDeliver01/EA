"use client";

import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { Panel } from "@/components/Panel";
import { api } from "@/lib/api";
import { usePrimaryAccount } from "@/lib/use-account";

function StatusValue({ ok, onLabel, offLabel }: { ok: boolean; onLabel: string; offLabel: string }) {
  return (
    <span className={clsx("tabular text-sm font-medium", ok ? "text-long" : "text-short")}>
      {ok ? onLabel : offLabel}
    </span>
  );
}

export default function SystemPage() {
  const { account } = usePrimaryAccount();
  const accountId = account?.id;

  const status = useQuery({
    queryKey: ["system-status", accountId],
    queryFn: () => api.systemStatus(accountId),
  });

  if (status.isLoading) {
    return <p className="text-sm text-text-muted">Loading system status…</p>;
  }
  if (status.isError || status.data === undefined) {
    return <p className="text-sm text-short">Could not load system status.</p>;
  }

  const s = status.data;

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Panel title="Engine">
        <dl className="grid grid-cols-2 gap-y-2 text-sm">
          <dt className="text-text-secondary">Global trading</dt>
          <dd className="text-right">
            <StatusValue ok={s.global_trading_enabled} onLabel="ENABLED" offLabel="DISABLED" />
          </dd>
          <dt className="text-text-secondary">Scheduler</dt>
          <dd className="text-right">
            <StatusValue ok={s.scheduler_enabled} onLabel="RUNNING" offLabel="STOPPED" />
          </dd>
          {s.agent_connected !== undefined && (
            <>
              <dt className="text-text-secondary">Agent</dt>
              <dd className="text-right">
                <StatusValue ok={s.agent_connected} onLabel="CONNECTED" offLabel="DISCONNECTED" />
              </dd>
            </>
          )}
          {s.unresolved_discrepancy_count !== undefined && (
            <>
              <dt className="text-text-secondary">Unresolved discrepancies</dt>
              <dd className="tabular text-right">
                <span
                  className={clsx(
                    s.unresolved_discrepancy_count > 0 ? "text-warning" : "text-text-primary"
                  )}
                >
                  {s.unresolved_discrepancy_count}
                </span>
              </dd>
            </>
          )}
          <dt className="text-text-secondary">Environment</dt>
          <dd className="text-right text-text-primary">{s.environment}</dd>
          <dt className="text-text-secondary">Build</dt>
          <dd className="tabular text-right text-xs text-text-muted">{s.git_sha}</dd>
        </dl>
      </Panel>

      <Panel title="Streams">
        <dl className="grid grid-cols-2 gap-y-2 text-sm">
          <dt className="text-text-secondary">bars_closed pending</dt>
          <dd className="tabular text-right text-text-primary">
            {s.streams.bars_closed_length}
          </dd>
          <dt className="text-text-secondary">intents_pending pending</dt>
          <dd className="tabular text-right text-text-primary">
            {s.streams.intents_pending_length}
          </dd>
        </dl>
        <p className="mt-3 text-xs text-text-muted">
          A number that keeps climbing means a worker is stuck, not that there is more to do.
        </p>
      </Panel>
    </div>
  );
}
