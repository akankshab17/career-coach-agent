import { BriefForm } from "@/components/BriefForm";
import { Header } from "@/components/Header";

export default function Home() {
  return (
    <>
      <Header />
      <main className="mx-auto w-full max-w-3xl space-y-8 px-4 py-8 sm:py-12">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Prepare for an interview</h1>
          <p className="mt-2 text-zinc-600 dark:text-zinc-400">
            A brief on the company, the interview process and the role, where every sentence cites the page it came from.
          </p>
        </div>
        <BriefForm />
      </main>
    </>
  );
}
