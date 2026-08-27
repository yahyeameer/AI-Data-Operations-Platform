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
// token, common with SSR across tabs), an unbounded getUser() can stall for
// tens of seconds -- and because this runs before every route, it hangs the
// whole app, not one page. Bounding the auth fetch and failing safe turns that
// into an instant, self-healing redirect to a fresh login instead.
const AUTH_TIMEOUT_MS = 4000;

function timeoutFetch(input: RequestInfo | URL, init?: RequestInit) {
  return fetch(input, { ...init, signal: AbortSignal.timeout(AUTH_TIMEOUT_MS) });
}

export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    {
      // Bound every Supabase network call this client makes so a hung auth
      // refresh can never stall the request past AUTH_TIMEOUT_MS.
      global: { fetch: timeoutFetch },
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
  // server instead of trusting whatever the cookie claims. If it errors or the
  // bounded fetch aborts, treat the request as signed-out rather than hanging:
  // a protected page then redirects to /login (which mints a clean session),
  // and public/API traffic proceeds unauthenticated exactly as it would for a
  // logged-out visitor. Never let an auth hiccup block the request.
  let user: Awaited<ReturnType<typeof supabase.auth.getUser>>['data']['user'] = null;
  try {
    const result = await supabase.auth.getUser();
    user = result.data.user;
  } catch {
    user = null;
  }

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
