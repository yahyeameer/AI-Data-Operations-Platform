import { createBrowserSupabase } from '@/lib/supabase/client';
import { mimeForFilename } from '@/lib/storage';

/**
 * The browser half of an upload, shared by the upload panel and the analyzer's
 * chat composer.
 *
 * Both surfaces have to perform the same three steps in the same order --
 * fingerprint, put to a signed URL, record the dataset version -- and a second
 * hand-rolled copy of that sequence in the chat component is exactly how the
 * two drift until one of them stops writing an audit entry. The sequence lives
 * here once; the callers differ only in what they render while it runs.
 */

export type UploadPhase = 'idle' | 'hashing' | 'uploading' | 'finalising';

/**
 * Labels describe what the app is actually doing at that moment.
 *
 * An earlier version showed a "Hermes Agent executing Python/DuckDB cleaning
 * pipeline" step backed by nothing but a 1.2s timer. In a product whose entire
 * claim is that every number is traceable, a progress bar that reports work
 * which never happened is not a cosmetic bug -- it is the product lying about
 * provenance. The parse/replay step returns here when the tool layer
 * (PRD v3 section 7) can actually run it.
 */
export const UPLOAD_PHASE_LABEL: Record<UploadPhase, string> = {
  idle: '',
  hashing: 'Fingerprinting file (SHA-256)…',
  uploading: 'Uploading to encrypted storage…',
  finalising: 'Recording dataset version & audit entry…',
};

export const UPLOAD_PHASE_PROGRESS: Record<UploadPhase, number> = {
  idle: 0,
  hashing: 25,
  uploading: 65,
  finalising: 90,
};

export async function sha256Hex(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', buffer);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

export async function uploadStatement({
  workspaceId,
  file,
  datasetId,
  datasetName,
  instructions,
  onPhase,
}: {
  workspaceId: string;
  file: File;
  /** An existing dataset to add this file to, or null to create one. */
  datasetId: string | null;
  /** Name for the dataset to create. Required when datasetId is null. */
  datasetName: string | null;
  instructions?: string | null;
  onPhase?: (phase: UploadPhase) => void;
}): Promise<{ uploadId: string; datasetId: string | null; jobId: string | null }> {
  const phase = (next: UploadPhase) => onPhase?.(next);

  phase('hashing');
  const sha256 = await sha256Hex(file);

  phase('uploading');
  const signResponse = await fetch('/api/uploads/sign', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      workspaceId,
      filename: file.name,
      byteSize: file.size,
      datasetId,
      datasetName,
    }),
  });

  const signed = await signResponse.json();
  if (!signResponse.ok) throw new Error(signed.error ?? 'Could not start the upload');

  // Re-wrap the file so the object lands with the MIME type the extension
  // implies; browsers report an empty type for .xls often enough to matter.
  const body = new File([file], file.name, { type: mimeForFilename(file.name) });

  const supabase = createBrowserSupabase();
  const { error: uploadError } = await supabase.storage
    .from(signed.bucket)
    .uploadToSignedUrl(signed.storagePath, signed.token, body);

  if (uploadError) throw new Error(uploadError.message);

  phase('finalising');
  const completeResponse = await fetch('/api/uploads/complete', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      uploadId: signed.uploadId,
      workspaceId,
      sha256,
      instructions: instructions?.trim() || null,
    }),
  });

  const completed = await completeResponse.json();
  if (!completeResponse.ok) throw new Error(completed.error ?? 'Could not record the upload');

  // The analysis is now a queued job rather than something this request waited
  // for, so what comes back is an id to watch instead of a result. Null means
  // the file stored but the job could not be queued -- the upload still
  // succeeded, and the caller can offer to try the analysis again.
  return {
    uploadId: signed.uploadId as string,
    datasetId: (signed.datasetId as string) ?? null,
    jobId: (completed.jobId as string) ?? null,
  };
}
