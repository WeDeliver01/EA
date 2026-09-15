"use client";

import clsx from "clsx";

const OPTIONS = ["ALL", "TRADE", "WAIT"] as const;
export type OutcomeFilterValue = (typeof OPTIONS)[number];

export function OutcomeFilter({
  value,
  onChange,
}: {
  value: OutcomeFilterValue;
  onChange: (value: OutcomeFilterValue) => void;
}) {
  return (
    <div className="flex gap-1">
      {OPTIONS.map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => onChange(option)}
          className={clsx(
            "rounded px-2 py-1 text-xs font-medium",
            value === option
              ? "bg-accent/20 text-accent"
              : "text-text-secondary hover:text-text-primary"
          )}
        >
          {option}
        </button>
      ))}
    </div>
  );
}
