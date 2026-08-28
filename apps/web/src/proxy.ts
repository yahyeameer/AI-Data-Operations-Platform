import { createServerClient } from '@supabase/ssr';
import { NextResponse, type NextRequest } from 'next/server';

/**
 * Refreshes the Supabase session cookie on every request and keeps signed-out
 * traffic away from the application shell.
 *
 * This is a convenience redirect, not a security boundary -- the proxy runs
 * before the route and can be reasoned about by an attacker. Every page and
 * route handler re-establishes the user itself (lib/authz.ts).
 *
 * Named `proxy` rather than `middleware`: Next.js 16 deprecated the middleware
 * file convention and renamed it, with identical semantics.
 */
const PROTECTED_PREFIXES = ['/app', '/onboarding'];
const AUTH_ROUTES = ['/login', '/signup'];

// The Supabase auth call in the proxy runs on EVERY request. If the session
// cookie has drifted into a bad-refresh state (a rotated/"already used" refresh
// token, common with SSR across tabs), getUser() can stall for tens of seconds
// -- and because this runs before every route, it hangs the whole app, not one
// page. We bound the TOTAL getUser() time with a hard deadline (a per-fetch
// abort is worse: the auth library treats an aborted fetch as a retryable
// network error and retries it with backoff, summing to ~30s). On timeout or
// error we fail safe: treat the request as signed-out.
const AUTH_DEADLINE_MS = 5000;

async function getUserBounded(
  supabase: ReturnType<typeof createServerClient>,
): Promise<Awaited<ReturnType<typeof supabase.auth.getUser>>['data']['user']> {
  const deadline = new Promise<null>((resolve) =>
    setTimeout(() => resolve(null), AUTH_DEADLINE_MS),
  );
  const lookup = supabase.auth
    .getUser()
    .then((r: Awaited<ReturnType<typeof supabase.auth.getUser>>) => r.data.user)
    .catch(() => null);
  return Promise.race([lookup, deadline]);
}

export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet) {
          for (const { name, value } of cookiesToSet) {
            request.cookies.set(name, value);
          }
          response = NextResponse.next({ request });
          for (const { name, value, options } of cookiesToSet) {
            response.cookies.set(name, value, options);
          }
        },
      },
    },
  );

  // getUser() rather than getSession(): it revalidates the token with the auth
  // server instead of trusting whatever the cookie claims. Bounded + fail-safe:
  // if it can't answer within the deadline, we treat the request as signed-out,
  // so a protected page redirects to /login (minting a clean session) instead
  // of hanging, and public/API traffic proceeds unauthenticated as it would for
  // any logged-out visitor. An auth hiccup must never block the request.
  const user = await getUserBounded(supabase);

  const { pathname } = request.nextUrl;
  const isProtected = PROTECTED_PREFIXES.some((p) => pathname.startsWith(p));

  if (!user && isProtected) {
    const url = request.nextUrl.clone();
    url.pathname = '/login';
    url.searchParams.set('next', pathname);
    return NextResponse.redirect(url);
  }

  if (user && AUTH_ROUTES.includes(pathname)) {
    const url = request.nextUrl.clone();
    url.pathname = '/app';
    url.search = '';
    return NextResponse.redirect(url);
  }

  return response;
}

export const config = {
  // Excludes /_next entirely, not just /_next/static and /_next/image. The dev
  // HMR endpoint lives at /_next/hmr and is a WebSocket upgrade: routing it
  // through here breaks the upgrade, and hot reload then fails on every retry.
  matcher: ['/((?!_next/|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)'],
};
