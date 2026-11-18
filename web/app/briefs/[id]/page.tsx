import { Suspense } from "react";
import { Header } from "@/components/Header";
import { BriefScreen } from "./BriefScreen";

export default function BriefPage({ params }: PageProps<"/briefs/[id]">) {
  return (
    <>
      <Header />
      <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:py-10">
        <Suspense fallback={null}>
          <BriefLoader params={params} />
        </Suspense>
      </main>
    </>
  );
}

async function BriefLoader({ params }: { params: PageProps<"/briefs/[id]">["params"] }) {
  const { id } = await params;
  return <BriefScreen id={id} />;
}
