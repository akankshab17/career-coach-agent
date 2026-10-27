import { LoginForm } from "./LoginForm";

export default function LoginPage() {
  return (
    <main className="mx-auto w-full max-w-sm px-4 py-24">
      <h1 className="text-2xl font-semibold">Interview Brief</h1>
      <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">Enter the access code to continue.</p>
      <LoginForm />
    </main>
  );
}
