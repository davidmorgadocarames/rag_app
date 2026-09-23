import Link from "next/link";

import VaultDoor from "@/components/VaultDoor";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col items-center justify-center gap-10 bg-vault-bg px-6 py-16">
      <div className="flex flex-col items-center gap-5">
        <VaultDoor mode="static" />
        <Link
          href="/login"
          className="border border-vault-amber px-8 py-2.5 text-xs tracking-[0.18em] text-vault-amber transition-colors hover:text-vault-amber-bright"
        >
          ENTER
        </Link>
      </div>

      <div className="flex flex-col items-center gap-6 text-center">
        <span className="w-fit border border-vault-steel-dark px-3 py-1 text-[11px] uppercase tracking-[0.22em] text-vault-steel">
          OWASP security vault
        </span>
        <h1 className="text-3xl font-bold tracking-[0.12em] text-vault-steel-light">SecRAG</h1>
        <p className="max-w-xl text-sm leading-relaxed text-vault-steel">
          An agentic RAG assistant for OWASP security guidance — version-aware, it cites its
          sources and is honest about what it doesn&apos;t know.
        </p>
      </div>

      {/* What this assistant knows — derived from the real corpus. */}
      <section className="w-full max-w-xl border border-vault-steel-dark p-6 text-sm text-vault-steel">
        <h2 className="mb-4 text-xs uppercase tracking-[0.22em] text-vault-amber">
          Inside the vault
        </h2>
        <ul className="flex flex-col gap-3">
          <li>
            <span className="text-vault-steel-light">OWASP Top 10</span> across the{" "}
            <span className="text-vault-steel-light">2017, 2021 and 2025</span> editions, plus
            OWASP <span className="text-vault-steel-light">Cheat Sheets</span>.
          </li>
          <li>
            Depth on <span className="text-vault-steel-light">Injection</span> (SQL &amp; OS command)
            and <span className="text-vault-steel-light">Broken Access Control</span>.
          </li>
          <li>
            A <span className="text-vault-steel-light">version-drift</span> showcase: Injection ranked{" "}
            <span className="text-vault-amber">A1 (2017)</span> →{" "}
            <span className="text-vault-amber">A03 (2021)</span> →{" "}
            <span className="text-vault-amber">A05 (2025)</span>.
          </li>
          <li>
            Answers are <span className="text-vault-steel-light">version-aware</span>, always{" "}
            <span className="text-vault-steel-light">cite their sources</span>, and{" "}
            <span className="text-vault-steel-light">abstain</span> when the corpus doesn&apos;t cover
            a question.
          </li>
        </ul>
      </section>
    </main>
  );
}
