import clsx from "clsx";

export function DirectionBadge({ direction }: { direction: string }) {
  const isLong = direction === "LONG";
  return (
    <span className={clsx("tabular text-xs font-medium", isLong ? "text-long" : "text-short")}>
      {direction}
    </span>
  );
}

export function OutcomeBadge({ outcome }: { outcome: string }) {
  const isTrade = outcome === "TRADE";
  return (
    <span
      className={clsx(
        "rounded px-1.5 py-0.5 text-xs font-medium",
        isTrade ? "bg-long/10 text-long" : "bg-off/10 text-text-muted"
      )}
    >
      {outcome}
    </span>
  );
}

export function PnlText({ value, currency }: { value: string; currency?: string }) {
  const n = Number(value);
  const formatted = currency
    ? new Intl.NumberFormat("en-US", { style: "currency", currency }).format(n)
    : n.toFixed(2);
  return <span className={clsx("tabular", n >= 0 ? "text-long" : "text-short")}>{formatted}</span>;
}
