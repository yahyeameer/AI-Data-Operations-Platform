import { NextResponse } from 'next/server';

import { handleRouteError } from '@/lib/api';
import { requireApiUser } from '@/lib/authz';
import { wakeParser } from '@/lib/hermes';

/**
 * Best-effort wake for the hosted parser, called when the chat screen mounts.
 *
 * Render's free tier sleeps after ~15 min idle. Firing this the moment the user
 * lands on the analyzer starts the cold start early -- while they read the page
 * and type -- so their first real question runs against a warm parser instead
 * of paying the 30-60s wake on top of the analysis and risking the function
 * wall-clock cap.
 *
 * The wake itself can take up to ~70s, so give the route the headroom the plan
 * allows (Hobby caps at 60; Pro/Fluid allow more) rather than the default.
 * Authenticated -- the parser endpoint is operational detail -- but never
 * workspace-scoped, since one parser serves the whole org. Always 200: waking
 * is advisory, and a failed ping is not something the user should see.
 */
export const maxDuration = 60;

export async function POST() {
  try {
    await requireApiUser();
    const awoke = await wakeParser();
    return NextResponse.json({ awoke });
  } catch (error) {
    return handleRouteError(error);
  }
}
