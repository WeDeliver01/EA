export function Panel({
  title,
  children,
  className,
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-md border border-border bg-surface p-4 ${className ?? ""}`}>
      <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-text-secondary">
        {title}
      </h2>
      {children}
    </section>
  );
}
