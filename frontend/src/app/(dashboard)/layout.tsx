import { AuthGuard } from "@/components/AuthGuard";
import { Nav } from "@/components/Nav";
import { StatusBar } from "@/components/StatusBar";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard>
      <StatusBar />
      <Nav />
      <main className="flex-1 p-4">{children}</main>
    </AuthGuard>
  );
}
