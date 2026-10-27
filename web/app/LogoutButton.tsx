"use client";

import { useRouter } from "next/navigation";

export function LogoutButton() {
  const router = useRouter();
  async function logout() {
    await fetch("/api/logout", { method: "POST" });
    router.replace("/login");
  }
  return (
    <button onClick={logout} className="text-sm text-zinc-600 underline dark:text-zinc-400">
      Log out
    </button>
  );
}
