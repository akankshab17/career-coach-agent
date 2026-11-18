import Link from "next/link";
import { LogoutButton } from "@/app/LogoutButton";

export function Header() {
  return (
    <header className="border-b border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
      <div className="mx-auto flex h-14 w-full max-w-3xl items-center justify-between px-4">
        <Link href="/" className="font-semibold tracking-tight">
          Interview Brief
        </Link>
        <LogoutButton />
      </div>
    </header>
  );
}
