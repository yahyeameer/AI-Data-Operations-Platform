import 'server-only';

import { createHmac } from 'node:crypto';

import { isLocalChatConfigured, localChat } from '@/lib/agent/openrouter-chat';
import { mintScopeToken } from '@/lib/tool-layer/scope-token';

export interface WebhookEventPayload {
  event: string;
  dataset_id: string;
  filename: string;
  tenant_id: string;
  workspace_id: string;
  [key: string]: unknown;
}

/**
 * Sends a webhook event to Hermes Agent when a workbook is uploaded or changed.
 *
 * Auth is HMAC-SHA256 over the exact request body, sent as
 * `X-Hub-Signature-256` -- the scheme the gateway's webhook adapter
 * validates. The raw secret never travels on the wire.
 */
export async function sendHermesWebhook(payload: WebhookEventPayload) {
  const base = (process.env.HERMES_AGENT_ENDPOINT || 'http://srv1927440:8644').replace(/\/+$/, '');
  const webhookUrl = process.env.HERMES_WEBHOOK_URL || `${base}/webhooks/analyzit-workbook-upload`;
  const secret = process.env.HERMES_WEBHOOK_SECRET || process.env.HERMES_API_SECRET;

  if (!secret) {
    return { received: false, skipped: true };
  }

  const body = JSON.stringify(payload);
  const signature = 'sha256=' + createHmac('sha256', secret).update(body, 'utf8').digest('hex');

  const response = await fetch(webhookUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Hub-Signature-256': signature,
      'X-GitHub-Event': payload.event,
      'X-Hermes-Secret': secret,
    },
    body,
    signal: AbortSignal.timeout(5000),
  });

  if (!response.ok) {
    const errorText = await response.text().catch(() => '');
    throw new Error(`Failed to send Hermes Webhook [${response.status}]: ${errorText.slice(0, 200)}`);
  }

  return response.json();
}

/**
 * Directly invokes a tool or chat turn on Hermes Agent.
 */
export async function triggerHermesAction(action: string, payload: Record<string, unknown>) {
  const endpoint = process.env.HERMES_AGENT_ENDPOINT || 'http://srv1927440:8000';
  const secret = process.env.HERMES_API_SECRET;

  const response = await fetch(`${endpoint}/api/v1/${action}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${secret}`,
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`Hermes agent action failed [${response.status}]: ${response.statusText}`);
  }

  return response.json();
}

/** Envelope every Hermes tool returns (PRD v3 section 7). */
export type HermesEnvelope<T = unknown> = {
  status: 'ok' | 'error' | 'blocked';
  result: T;
  evidence?: unknown;
  warnings?: string[];
  execution_metadata?: {
    tool?: string;
    duration_ms?: number;
    model?: string;
    dry_run?: boolean;
    [key: string]: unknown;
  };
};

export type HermesHealth = {
  configured: boolean;
  reachable: boolean;
  status?: string;
  uptime?: string;
  queueDepth?: number;
  activeWorkers?: number;
  /** Present when reachable is false. Safe to show a user; never contains secrets. */
  detail?: string;
};

export type HermesDownload = {
  filename: string;
  url: string;
  format?: string;
  rows?: number;
  expires_in_seconds?: number;
};

export type HermesChatMessage = {
  role: 'user' | 'assistant';
  content: string;
};

export class HermesError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'HermesError';
  }
}

const DEFAULT_TIMEOUT_MS = 300_000;
const HEALTH_TIMEOUT_MS = 5_000;

function endpoint(): string | null {
  const raw = process.env.HERMES_AGENT_ENDPOINT?.trim();
  if (!raw) return null;
  return raw.replace(/\/+$/, '');
}

/**
 * Whether the hosted parser/agent bridge has both halves of its configuration.
 * This is the FULL path (parser with DuckDB/Polars tools).
 */
export function isHermesConfigured(): boolean {
  return Boolean(endpoint() && process.env.HERMES_API_SECRET);
}

/**
 * Whether chat can answer at all -- either via the hosted parser (full,
 * tool-grounded analysis) or the in-process OpenRouter fallback
 * (conversational only). Used by the health indicator and the chat route so
 * the deployed Vercel site reports chat as available even before the parser
 * is hosted.
 */
export function isChatAvailable(): boolean {
  return isHermesConfigured() || isLocalChatConfigured();
}

/**
 * Fire-and-forget wake-up for the hosted parser.
 *
 * Render's free tier sleeps after ~15 min idle and takes 30-60s to wake. If a
 * user's first analysis turn is what wakes it, that turn eats the cold start on
 * top of its own work and can cross the serverless function's wall-clock cap.
 * Calling this the moment the chat screen mounts starts the wake while the user
 * is still reading the page or typing, so by the time they hit send the parser
 * is (usually) already up and the turn runs warm.
 *
 * Deliberately tolerant: any outcome resolves to a boolean and never throws.
 * Waking is best-effort; a failed ping must not surface to the user, and the
 * keep-warm cron plus the defensive chat client are the real guarantees.
 *
 * Uses a longer timeout than the health check (which aborts at 5s and would
 * give up mid-wake) precisely because the point here is to hold the request
 * open long enough for Render to finish spinning up.
 */
export async function wakeParser(): Promise<boolean> {
  const base = endpoint();
  if (!base || !isHermesConfigured()) return false;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 70_000);
  try {
    const response = await fetch(`${base}/health`, {
      headers: { authorization: `Bearer ${process.env.HERMES_API_SECRET}` },
      signal: controller.signal,
      cache: 'no-store',
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function call<T>(
  path: string,
  payload: unknown,
  { timeoutMs = DEFAULT_TIMEOUT_MS }: { timeoutMs?: number } = {},
): Promise<T> {
  const base = endpoint();
  const secret = process.env.HERMES_API_SECRET;

  if (!base || !secret) {
    throw new HermesError(
      'Hermes is not configured. Set HERMES_AGENT_ENDPOINT and HERMES_API_SECRET on the server.',
      503,
    );
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${secret}`,
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
      cache: 'no-store',
    });
  } catch (error) {
    const timedOut = error instanceof Error && error.name === 'AbortError';
    throw new HermesError(
      timedOut
        ? `Hermes did not respond within ${Math.round(timeoutMs / 1000)}s`
        : 'Could not reach the Hermes agent',
      504,
    );
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    console.error(`[hermes] ${path} responded ${response.status}`);
    throw new HermesError(
      response.status === 401 || response.status === 403
        ? 'Hermes rejected the credentials for this deployment'
        : `Hermes returned an error (${response.status})`,
      502,
    );
  }

  return (await response.json()) as T;
}

/**
 * Liveness for the sidebar indicator.
 */
export async function hermesHealth(): Promise<HermesHealth> {
  const base = endpoint();

  // No hosted parser, but in-process OpenRouter chat is available: report as
  // reachable in conversational mode so the deployed site shows chat as live.
  if (!isHermesConfigured()) {
    if (isLocalChatConfigured()) {
      return {
        configured: true,
        reachable: true,
        status: 'conversational',
        detail: 'Conversational mode (analysis engine not connected)',
      };
    }
    return { configured: false, reachable: false, detail: 'Not configured' };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);

  try {
    const response = await fetch(`${base}/health`, {
      headers: { authorization: `Bearer ${process.env.HERMES_API_SECRET}` },
      signal: controller.signal,
      cache: 'no-store',
    });

    if (!response.ok) {
      return { configured: true, reachable: false, detail: `HTTP ${response.status}` };
    }

    const body = (await response.json()) as {
      status?: string;
      uptime?: string;
      queue_depth?: number;
      active_workers?: number;
    };

    return {
      configured: true,
      reachable: true,
      status: body.status,
      uptime: body.uptime,
      queueDepth: body.queue_depth,
      activeWorkers: body.active_workers,
    };
  } catch {
    return { configured: true, reachable: false, detail: 'Unreachable' };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Ask the copilot a question inside one workspace.
 *
 * Two paths, chosen by configuration:
 *  - Hosted parser present (HERMES_AGENT_ENDPOINT set): route to the parser's
 *    /api/v1/chat, which runs DuckDB/Polars tools over the uploaded dataset.
 *    Full, data-grounded analysis.
 *  - Parser absent (e.g. Vercel-only deploy): answer in-process via OpenRouter.
 *    Conversational only -- no dataset access, never fabricates figures.
 */
export async function hermesChat(input: {
  workspaceId: string;
  orgId: string;
  userId: string;
  message: string;
  history: HermesChatMessage[];
}): Promise<HermesEnvelope<{ reply: string; downloads?: HermesDownload[] }>> {
  if (isHermesConfigured()) {
    const scopeToken = mintScopeToken({
      orgId: input.orgId,
      workspaceId: input.workspaceId,
      userId: input.userId,
    });

    return call<HermesEnvelope<{ reply: string; downloads?: HermesDownload[] }>>('/api/v1/chat', {
      workspace_id: input.workspaceId,
      org_id: input.orgId,
      message: input.message,
      history: input.history,
      scope_token: scopeToken,
      tool_layer_url: process.env.TOOL_LAYER_PUBLIC_URL ?? null,
    });
  }

  // Fallback: in-process conversational reply (no dataset tools).
  if (isLocalChatConfigured()) {
    const { reply, model, durationMs } = await localChat({
      message: input.message,
      history: input.history,
    });
    return {
      status: reply ? 'ok' : 'error',
      result: { reply, downloads: [] },
      evidence: { mode: 'conversational', tools_used: [] },
      warnings: [
        'Data-analysis engine not connected: this reply cannot read uploaded files. Answers about specific figures require the analysis service.',
      ],
      execution_metadata: {
        duration_ms: durationMs,
        model,
        dry_run: false,
        mode: 'local-openrouter',
      },
    };
  }

  throw new HermesError(
    'Chat is not configured. Set OPENROUTER_API_KEY (conversational) or HERMES_AGENT_ENDPOINT + HERMES_API_SECRET (full analysis).',
    503,
  );
}

/**
 * Invoke one tool from the contract in PRD v3 section 7.
 */
export async function hermesTool<T = unknown>(
  tool: string,
  params: Record<string, unknown>,
  options: { dryRun?: boolean; timeoutMs?: number } = {},
): Promise<HermesEnvelope<T>> {
  const { dryRun = true, timeoutMs } = options;
  return call<HermesEnvelope<T>>(
    `/api/v1/tools/${tool}`,
    { ...params, dry_run: dryRun },
    { timeoutMs },
  );
}
