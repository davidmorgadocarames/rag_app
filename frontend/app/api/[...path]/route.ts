// The BFF (P12-D1, T12.1.5): a thin Next.js route handler. All real logic lives in
// lib/bffProxy.ts + lib/bffConfig.ts (C5), which are tested directly with `node --test`
// against a fake upstream -- this file is intentionally not unit-tested, only wired.
import type { NextRequest } from "next/server";

import { bffConfig } from "@/lib/bffConfig";
import { proxyToBackend } from "@/lib/bffProxy";

export const dynamic = "force-dynamic";

interface RouteParams {
  params: Promise<{ path: string[] }>;
}

async function handle(request: NextRequest, params: RouteParams["params"]): Promise<Response> {
  const { path } = await params;
  return proxyToBackend(
    {
      method: request.method,
      path: "/" + path.join("/"),
      search: request.nextUrl.search,
      headers: request.headers,
      body: request.body,
    },
    bffConfig(),
  );
}

export async function GET(request: NextRequest, { params }: RouteParams): Promise<Response> {
  return handle(request, params);
}

export async function POST(request: NextRequest, { params }: RouteParams): Promise<Response> {
  return handle(request, params);
}

export async function PATCH(request: NextRequest, { params }: RouteParams): Promise<Response> {
  return handle(request, params);
}

export async function PUT(request: NextRequest, { params }: RouteParams): Promise<Response> {
  return handle(request, params);
}

export async function DELETE(request: NextRequest, { params }: RouteParams): Promise<Response> {
  return handle(request, params);
}
