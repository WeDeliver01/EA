"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import clsx from "clsx";
import { api } from "@/lib/api";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/signals", label: "Signals" },
  { href: "/positions", label: "Positions" },
  { href: "/trades", label: "Trades" },
  { href: "/system", label: "System" },
];

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();

  return (
    <nav className="flex h-10 items-center gap-1 border-b border-border bg-background px-4">
      {LINKS.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          className={clsx(
            "rounded px-3 py-1 text-sm",
            pathname === link.href
              ? "bg-surface-raised text-text-primary"
              : "text-text-secondary hover:text-text-primary"
          )}
        >
          {link.label}
        </Link>
      ))}
      <button
        onClick={() => api.logout().then(() => router.replace("/login"))}
        className="ml-auto rounded px-3 py-1 text-sm text-text-secondary hover:text-text-primary"
      >
        Sign out
      </button>
    </nav>
  );
}
