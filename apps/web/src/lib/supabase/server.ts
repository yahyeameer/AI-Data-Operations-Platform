import { createServerClient } from '@supabase/ssr';
import { createClient } from '@supabase/supabase-js';
import { cookies } from 'next/headers';

import type { Database } from '@/lib/database.types';

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!;

// Bound Supabase network calls so a hung auth refresh (a drifted/"already used"
// session cookie) fails fast instead of stalling a server render. The proxy is
// the primary guard, but pages re-establish the user themselves, so give that
// path the same ceiling.
const SERVER_FETCH_TIMEOUT_MS = 5000;

function timeoutFetch(input: RequestInfo | URL, init?: RequestInit) {
  return fetch(input, { ...init, signal: AbortSignal.timeout(SERVER_FETCH_TIMEOUT_MS) });
}

/**
 * Server client for Server Components, Server Actions and Route Handlers.
 *
 * Server Components cannot write cookies, so setAll swallows the error there;
 * the proxy (src/proxy.ts) is what actually refreshes the session cookie on
 * every request.
 */
export async function createServerSupabase() {
  const cookieStore = await cookies();

  return createServerClient<Database>(SUPABASE_URL, PUBLISHABLE_KEY, {
    global: { fetch: timeoutFetch },
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        try {
          for (const { name, value, options } of cookiesToSet) {
            cookieStore.set(name, value, options);
          }
        } catch {
          // Called from a Server Component; the proxy refreshes instead.
        }
      },
    },
  });
}

/**
 * Service-role client. Bypasses RLS entirely, so it is only ever constructed
 * after the caller's access has been established -- see `adminFor` in
 * lib/authz.ts, which takes a proven context as its argument precisely so that
 * ordering is visible at every call site.
 *
 * The throw is deliberate: a missing secret should fail the request loudly
 * rather than silently degrade to an unauthenticated client.
 */
export function createAdminSupabase() {
  const secret = process.env.SUPABASE_SECRET_KEY;

  if (!secret) {
    throw new Error(
      'SUPABASE_SECRET_KEY is not set. The write paths need the service role: ' +
        'the datasets and raw_uploads tables grant only SELECT to authenticated, ' +
        'so falling back to the publishable key fails with an RLS violation.',
    );
  }

  return createClient<Database>(SUPABASE_URL, secret, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}
