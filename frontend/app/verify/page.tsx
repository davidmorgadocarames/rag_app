// T12.4.5: SSR shell (this file, a server component -- reads the token server-side from the
// URL, same "GET only renders" shape as app/login/page.tsx) + CSR form (VerifyPanel: the
// button click that actually POSTs the token). The email link points here
// (PUBLIC_APP_URL + "/verify?token=...", rag_app.emailer.verification_link).
import VerifyPanel from "./VerifyPanel";

export default async function VerifyPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const { token } = await searchParams;
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6">
      <h1 className="text-2xl font-bold tracking-[0.12em] text-vault-steel-light">
        Verify your email
      </h1>
      <VerifyPanel token={token ?? ""} />
    </main>
  );
}
