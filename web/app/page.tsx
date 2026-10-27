import { LogoutButton } from "./LogoutButton";

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-2xl px-4 py-16">
      <div className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold">Interview Brief</h1>
        <LogoutButton />
      </div>
      <p className="mt-2 text-zinc-600 dark:text-zinc-400">The new web app is under construction.</p>
    </main>
  );
}
