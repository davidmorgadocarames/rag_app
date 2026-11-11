// T12.4.5: SSR shell (this file) + CSR form (ResetPanel). The backend route this page's
// form eventually calls lands in 12b-C (T12.4.6, password reset tokens) -- see
// ResetPanel.tsx and lib/api.ts for the documented intermediate state.
import ResetPanel from "./ResetPanel";

export default async function ResetPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const { token } = await searchParams;
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6">
      <h1 className="text-2xl font-bold tracking-[0.12em] text-vault-steel-light">
        Reset your password
      </h1>
      <ResetPanel token={token ?? ""} />
    </main>
  );
}
