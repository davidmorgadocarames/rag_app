// T12.4.6: SSR shell (this file) + CSR form (ForgotPasswordPanel), same split as /reset and
// /verify.
import ForgotPasswordPanel from "./ForgotPasswordPanel";

export default function ForgotPasswordPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6">
      <h1 className="text-2xl font-bold tracking-[0.12em] text-vault-steel-light">
        Reset your password
      </h1>
      <ForgotPasswordPanel />
    </main>
  );
}
