'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

import {
  ACCEPTED_EXTENSIONS,
  MAX_UPLOAD_BYTES,
  formatBytes,
  isAcceptedFilename,
} from '@/lib/storage';
import { UPLOAD_PHASE_LABEL, uploadStatement, type UploadPhase } from '@/lib/upload-client';
import { ErrorText, Spinner, buttonClass, buttonStyle, inputFocusHandler, inputStyle } from '@/components/ui';

type Dataset = { id: string; name: string };

type Download = {
  filename: string;
  url: string;
  format?: string;
  rows?: number;
  expires_in_seconds?: number;
};

type Turn = {
  /** 'note' is the app talking about itself -- an upload that landed, say. */
  role: 'user' | 'assistant' | 'note';
  content: string;
  warnings?: string[];
  downloads?: Download[];
  pending?: boolean;
};

/** Name used when a chat upload has no dataset to join yet. */
const DEFAULT_DATASET_NAME = 'Bank statements';

/**
 * Openers offered before the first question.
 *
 * A blank composer asks the accountant to guess what the tool can be asked,
 * and the usual guess is either far too vague or something the tool layer
 * cannot compute. Each of these maps onto work the tools actually do.
 */
const STARTER_QUESTIONS = [
  'Summarise this statement: total in, total out, closing balance.',
  'What were the ten largest payments, and who did they go to?',
  'Which payments recur every month?',
  'Are there any duplicate transactions?',
];

/**
 * The accountant-facing chat surface (PRD v3 section 4) -- now presented as the
 * AI Bank Statement Analyzer, the app's single screen.
 *
 * What this component deliberately does not do is as important as what it does.
 * It shows the agent's prose and nothing else: no tool payloads, no model name,
 * no endpoint, no system prompt. Those belong to the operator's Hermes console,
 * and section 4 draws that line on purpose.
 *
 * The disclaimer under the composer is not decoration either. Section 17 makes
 * the positioning legally load-bearing -- AnalyzeIt is a copilot and the
 * accountant signs off -- so the interface has to say so where the answers
 * appear, not only in the terms of service.
 *
 * Uploading lives here as well as on the panel above. Attaching the statement
 * you are about to ask about, in the place you ask about it, is the whole
 * gesture; making someone scroll up to a separate form to do it breaks the one
 * thing this screen is for. The upload path is the shared one, so a file
 * attached here is hashed, versioned and audited exactly like any other.
 */
export function HermesChat({
  workspaceId,
  workspaceName,
  datasets,
  statementCount,
}: {
  workspaceId: string;
  workspaceName: string;
  datasets: Dataset[];
  statementCount: number;
}) {
  const router = useRouter();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [upload, setUpload] = useState<{ name: string; phase: UploadPhase } | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [uploadedHere, setUploadedHere] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const uploading = upload !== null;
  const hasStatements = statementCount + uploadedHere > 0;

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [turns, upload]);

  // Wake the parser the instant this screen mounts. On Render's free tier the
  // analysis service sleeps after ~15 min idle; kicking off the cold start now,
  // while the accountant is still reading the page and typing, means their
  // first question usually meets a warm parser instead of paying the 30-60s
  // wake on top of the analysis. Fire-and-forget: the wake is advisory, so its
  // outcome is deliberately ignored and never blocks or interrupts the UI.
  useEffect(() => {
    const controller = new AbortController();
    void fetch('/api/hermes/wake', {
      method: 'POST',
      cache: 'no-store',
      signal: controller.signal,
    }).catch(() => {
      // Waking is best-effort; the keep-warm cron and the defensive send()
      // handler are the real guarantees. A failed ping is a non-event here.
    });
    return () => controller.abort();
  }, []);

  async function send(text?: string) {
    const message = (text ?? draft).trim();
    if (!message || busy) return;

    setError(null);
    setBusy(true);
    setDraft('');

    // The history sent upstream is the state *before* this turn, and carries
    // only the real dialogue: the pending placeholder is not an answer, and an
    // upload note is the app narrating itself, not something the agent said.
    const history = turns
      .filter((turn) => !turn.pending && turn.role !== 'note')
      .map(({ role, content }) => ({ role, content }));

    setTurns((current) => [
      ...current,
      { role: 'user', content: message },
      { role: 'assistant', content: '', pending: true },
    ]);

    try {
      const response = await fetch('/api/hermes/chat', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ workspaceId, message, history }),
      });

      // The body is not guaranteed to be JSON. When the parser cold-starts and
      // the analysis runs past the serverless function's wall-clock cap, Vercel
      // kills the function and returns its own plain-text error page ("An error
      // occurred..."). Calling response.json() on that throws an opaque
      // "Unexpected token 'A'". Read text first, parse defensively, and turn a
      // non-JSON body into a message a user can act on.
      const raw = await response.text();
      let body: {
        error?: string;
        reply?: string;
        warnings?: string[];
        downloads?: unknown;
      } = {};
      try {
        body = raw ? JSON.parse(raw) : {};
      } catch {
        // Non-JSON body: almost always the platform timing out the function.
        throw new Error(
          response.status === 504 || response.status === 502
            ? 'The analysis is taking longer than this plan allows. The engine may be waking up — try again in a moment.'
            : 'The agent was interrupted before it could answer. Please try again in a moment.',
        );
      }
      if (!response.ok) throw new Error(body.error ?? 'The agent could not answer');

      setTurns((current) => [
        ...current.slice(0, -1),
        {
          role: 'assistant',
          content: body.reply || '(no answer returned)',
          warnings: body.warnings,
          downloads: Array.isArray(body.downloads) ? body.downloads : undefined,
        },
      ]);
    } catch (caught) {
      // Drop the placeholder *and* the question. Leaving an unanswered question
      // in the transcript would send it again as history on the next turn.
      setTurns((current) => current.slice(0, -2));
      setDraft(message);
      setError(caught instanceof Error ? caught.message : 'The agent could not answer');
    } finally {
      setBusy(false);
    }
  }

  async function attach(file: File | undefined) {
    if (!file || uploading) return;

    setError(null);
    if (!isAcceptedFilename(file.name)) {
      setError(`Only ${ACCEPTED_EXTENSIONS.join(', ')} files are accepted`);
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(`${file.name} is ${formatBytes(file.size)}; the maximum is ${formatBytes(MAX_UPLOAD_BYTES)}`);
      return;
    }

    setUpload({ name: file.name, phase: 'hashing' });

    try {
      // A chat upload joins the client's existing statement set rather than
      // asking which one; the panel above is where that choice is made.
      await uploadStatement({
        workspaceId,
        file,
        datasetId: datasets[0]?.id ?? null,
        datasetName: datasets[0] ? null : DEFAULT_DATASET_NAME,
        onPhase: (phase) => setUpload((current) => (current ? { ...current, phase } : current)),
      });

      setUploadedHere((count) => count + 1);
      setTurns((current) => [
        ...current,
        {
          role: 'note',
          content: `Uploaded ${file.name} (${formatBytes(file.size)}). Fingerprinted, versioned and stored — you can ask about it now.`,
        },
      ]);
      // The panel above counts this client's statements server-side.
      router.refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Upload failed');
    } finally {
      setUpload(null);
      if (fileRef.current) fileRef.current.value = '';
    }
  }

  return (
    <div className="flex h-[34rem] min-h-[28rem] flex-col">
      <div
        ref={scrollRef}
        onDragOver={(event) => {
          event.preventDefault();
          setDragActive(true);
        }}
        onDragLeave={() => setDragActive(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragActive(false);
          void attach(event.dataTransfer.files?.[0]);
        }}
        className="relative flex-1 space-y-4 overflow-y-auto rounded-2xl border p-5 transition-colors"
        style={{
          borderColor: dragActive ? 'rgba(16,185,129,.5)' : 'var(--az-border)',
          background: dragActive ? 'rgba(16,185,129,.05)' : 'var(--az-bg-sidebar)',
        }}
      >
        {dragActive && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-slate-950/70 text-sm font-bold text-emerald-300">
            Drop the statement to upload it
          </div>
        )}

        {turns.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <p className="text-sm font-semibold text-slate-300">
              Ask about {workspaceName}&apos;s bank statements
            </p>
            <p className="max-w-md text-xs leading-relaxed text-slate-500">
              {hasStatements
                ? 'Questions are answered by running tools over the statements uploaded for this client, so every number traces back to its source rows, dataset version and the file it came from.'
                : 'Attach a statement below — or drop one here — and then ask about it. Answers are computed from the uploaded rows, so every number traces back to its source.'}
            </p>

            {/* Openers. They send on click, so the first question costs one
                tap rather than a blank page. */}
            <div className="flex max-w-lg flex-wrap justify-center gap-2 pt-1">
              {STARTER_QUESTIONS.map((question) => (
                <button
                  key={question}
                  type="button"
                  onClick={() => void send(question)}
                  disabled={busy}
                  className="rounded-full border px-3 py-1.5 text-left text-[11px] font-semibold text-slate-300 transition-colors hover:border-emerald-500/40 hover:text-emerald-300 disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer"
                  style={{ borderColor: 'var(--az-border)', background: 'var(--az-bg-card)' }}
                >
                  {question}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn, index) =>
          turn.role === 'note' ? (
            <div key={index} className="flex justify-center">
              <p
                className="rounded-full border px-3 py-1.5 text-[11px] font-semibold text-emerald-300"
                style={{ borderColor: 'rgba(16,185,129,.3)', background: 'rgba(16,185,129,.1)' }}
              >
                {turn.content}
              </p>
            </div>
          ) : (
            <div
              key={index}
              className={`flex ${turn.role === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              <div
                className="max-w-[85%] rounded-2xl px-4 py-3 text-sm leading-relaxed"
                style={
                  turn.role === 'user'
                    ? { background: 'rgba(16,185,129,.14)', border: '1px solid rgba(16,185,129,.3)', color: '#d1fae5' }
                    : { background: 'var(--az-bg-input)', border: '1px solid var(--az-border)', color: 'var(--az-text)' }
                }
              >
                {turn.pending ? (
                  <span className="flex items-center gap-2 text-slate-400">
                    <Spinner size={14} />
                    Analyzing the statements…
                  </span>
                ) : (
                  <span className="whitespace-pre-wrap">{turn.content}</span>
                )}

                {turn.warnings && turn.warnings.length > 0 && (
                  <ul className="mt-3 space-y-1 border-t pt-2 text-xs text-amber-300" style={{ borderColor: 'var(--az-border)' }}>
                    {turn.warnings.map((warning, i) => (
                      <li key={i}>⚠ {warning}</li>
                    ))}
                  </ul>
                )}

                {turn.downloads && turn.downloads.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-2 border-t pt-3" style={{ borderColor: 'var(--az-border)' }}>
                    {turn.downloads.map((file, i) => (
                      <a
                        key={i}
                        href={file.url}
                        download={file.filename}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 rounded-xl border px-3 py-2 text-xs font-semibold text-emerald-300 transition-colors hover:border-emerald-500/50 hover:bg-emerald-500/10"
                        style={{ borderColor: 'rgba(16,185,129,.35)', background: 'rgba(16,185,129,.06)' }}
                      >
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                          <polyline points="7 10 12 15 17 10" />
                          <line x1="12" y1="15" x2="12" y2="3" />
                        </svg>
                        Download {file.filename}
                        {typeof file.rows === 'number' && (
                          <span className="text-emerald-500/70">· {file.rows} rows</span>
                        )}
                      </a>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )
        )}

        {upload && (
          <div className="flex justify-center">
            <p
              className="flex items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] font-semibold text-slate-300"
              style={{ borderColor: 'var(--az-border)', background: 'var(--az-bg-card)' }}
            >
              <Spinner size={12} />
              {upload.name} — {UPLOAD_PHASE_LABEL[upload.phase]}
            </p>
          </div>
        )}
      </div>

      {error && (
        <div className="mt-3">
          <ErrorText>{error}</ErrorText>
        </div>
      )}

      <div className="mt-4 flex items-end gap-3">
        <input
          ref={fileRef}
          type="file"
          className="hidden"
          accept={ACCEPTED_EXTENSIONS.join(',')}
          onChange={(event) => void attach(event.target.files?.[0])}
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
          title={`Attach a bank statement (${ACCEPTED_EXTENSIONS.join(', ')}, up to ${formatBytes(MAX_UPLOAD_BYTES)})`}
          aria-label="Attach a bank statement"
          className="flex h-[46px] w-[46px] shrink-0 items-center justify-center rounded-xl border text-slate-400 transition-colors hover:border-emerald-500/40 hover:text-emerald-300 disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer"
          style={{ borderColor: 'var(--az-border)', background: 'var(--az-bg-card)' }}
        >
          {uploading ? (
            <Spinner size={16} />
          ) : (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
          )}
        </button>

        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              void send();
            }
          }}
          rows={2}
          placeholder="Ask about this client's bank statements… (Enter to send, Shift+Enter for a new line)"
          className="w-full resize-none rounded-xl px-4 py-3 text-sm outline-none transition-all duration-200 placeholder:text-slate-500"
          style={inputStyle}
          {...inputFocusHandler}
          disabled={busy}
        />
        <button
          type="button"
          onClick={() => void send()}
          disabled={busy || draft.trim().length === 0}
          className={`${buttonClass} shrink-0 disabled:cursor-not-allowed disabled:opacity-50`}
          style={buttonStyle}
        >
          {busy ? <Spinner size={16} /> : 'Send'}
        </button>
      </div>

      <p className="mt-3 text-center text-[11px] leading-relaxed text-slate-500">
        The analyzer proposes and explains; deterministic tools calculate. Every figure traces to
        source rows. Material changes still require your sign-off.
      </p>
    </div>
  );
}
