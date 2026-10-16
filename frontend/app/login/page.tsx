// T12.1.9: SSR shell (this file, a server component -- the background/layout reaches the
// browser in the server-rendered HTML, no client JS needed to see it) + CSR form
// (LoginPanel, "use client": the door animation and the register/login request itself).
import LoginPanel from "./LoginPanel";

export default function LoginPage() {
  return (
    <main className="relative flex min-h-screen flex-col items-center justify-center bg-vault-bg px-4">
      <LoginPanel />
    </main>
  );
}
