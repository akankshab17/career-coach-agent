import { NextResponse, type NextRequest } from "next/server";
import { ConfigError, SESSION_COOKIE, verifySessionToken } from "@/lib/session";

// Runs before every matched request (Next 16's name for middleware). Without a valid session, pages redirect to /login
// and API routes answer 401. Route handlers check the session again themselves (lib/http.ts requireSession).
export async function proxy(request: NextRequest) {
  let session = null;
  try {
    session = await verifySessionToken(request.cookies.get(SESSION_COOKIE)?.value);
  } catch (err) {
    if (!(err instanceof ConfigError)) throw err;
    console.error(`[proxy] ${err.message}`); // misconfigured: treat everyone as logged out
  }
  if (session) return NextResponse.next();

  const { pathname, search } = request.nextUrl;
  if (pathname.startsWith("/api/")) return NextResponse.json({ detail: "Please log in." }, { status: 401 });
  const login = new URL("/login", request.url);
  if (pathname !== "/") login.searchParams.set("next", pathname + search);
  return NextResponse.redirect(login);
}

export const config = {
  // Everything except the login page/route, the health check, and static files.
  matcher: ["/((?!login|api/login|api/health|_next/static|_next/image|favicon.ico).*)"],
};
