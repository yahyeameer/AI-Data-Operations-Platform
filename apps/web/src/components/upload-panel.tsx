'use client';

import { useRouter } from 'next/navigation';
import { useRef, useState } from 'react';

import { createBrowserSupabase } from '@/lib/supabase/client';
import {
  ACCEPTED_EXTENSIONS,
  MAX_UPLOAD_BYTES,
  formatBytes,
  isAcceptedFilename,
  mimeForFilename,
} from '@/lib/storage';
import { ErrorText, Field, buttonClass, inputClass } from '@/components/ui';

type Dataset = { id: string; name: string };

type Phase = 'idle' | 'hashing' | 'uploading' | 'finalising';

const PHASE_LABEL: Record<Phase, string> = {
  idle: '',
  hashing: 'Fingerprinting file…',
  uploading: 'Uploading…',
  finalising: 'Recording…',
};

/**
 * Three-step upload: reserve, PUT straight to storage, confirm.
 *
 * The bytes never pass through the Next.js server. Beyond avoiding body-size
 * limits, it keeps the raw customer workbook on exactly one hop between the
 * accountant's machine and private storage.
 */
export function UploadPanel({
  workspaceId,
  datasets,
}: {
  workspaceId: string;
  datasets: Dataset[];
}) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [datasetId, setDatasetId] = useState<string>(datasets[0]?.id ?? '');
  const [datasetName, setDatasetName] = useState('');

  const creatingNewDataset = datasetId === '';
  const busy = phase !== 'idle';

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    const file = inputRef.current?.files?.[0];
    if (!file) {
      setError('Choose a file first');
      return;
    }
    if (!isAcceptedFilename(file.name)) {
      setError(`Only ${ACCEPTED_EXTENSIONS.join(', ')} files are accepted`);
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(`File is ${formatBytes(file.size)}; the limit is ${formatBytes(MAX_UPLOAD_BYTES)}`);
      return;
    }
    if (creatingNewDataset && datasetName.trim().length < 2) {
      setError('Name the recurring dataset this file belongs to');
      return;
    }

    try {
      setPhase('hashing');
      const sha256 = await sha256Hex(file);

      setPhase('uploading');
      const signResponse = await fetch('/api/uploads/sign', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          workspaceId,
          filename: file.name,
          byteSize: file.size,
          datasetId: creatingNewDataset ? null : datasetId,
          datasetName: creatingNewDataset ? datasetName.trim() : null,
        }),
      });

      const signed = await signResponse.json();
      if (!signResponse.ok) throw new Error(signed.error ?? 'Could not start the upload');

      // The storage client ignores its own contentType option when the body is
      // a Blob -- it builds a FormData and the type comes from the blob itself.
      // So set the type on the blob, and derive it from the extension rather
      // than trusting file.type: Windows reports an empty type for .xlsx when
      // the extension is not registered, which the bucket then rejects as
      // application/octet-stream.
      const body = new File([file], file.name, { type: mimeForFilename(file.name) });

      const supabase = createBrowserSupabase();
      const { error: uploadError } = await supabase.storage
        .from(signed.bucket)
        .uploadToSignedUrl(signed.storagePath, signed.token, body);

      if (uploadError) throw new Error(uploadError.message);

      setPhase('finalising');
      const completeResponse = await fetch('/api/uploads/complete', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ uploadId: signed.uploadId, workspaceId, sha256 }),
      });

      const completed = await completeResponse.json();
      if (!completeResponse.ok) throw new Error(completed.error ?? 'Could not record the upload');

      if (inputRef.current) inputRef.current.value = '';
      setDatasetName('');
      if (signed.datasetId) setDatasetId(signed.datasetId);
      router.refresh();
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Upload failed');
    } finally {
      setPhase('idle');
    }
  }

  return (
    // method="post" for the same reason as the sign-in form: an unhydrated page
    // would otherwise submit natively as a GET and put the field values in the
    // URL.
    <form onSubmit={onSubmit} method="post" className="space-y-4">
      <Field
        label="Recurring dataset"
        hint="Group each month's file under the same dataset. The agent fingerprints the layout on the first parse, so next month's file can be matched to it."
      >
        <select
          className={inputClass}
          value={datasetId}
          onChange={(e) => setDatasetId(e.target.value)}
          disabled={busy}
        >
          {datasets.map((dataset) => (
            <option key={dataset.id} value={dataset.id}>
              {dataset.name}
            </option>
          ))}
          <option value="">+ New dataset…</option>
        </select>
      </Field>

      {creatingNewDataset ? (
        <Field label="New dataset name">
          <input
            className={inputClass}
            value={datasetName}
            onChange={(e) => setDatasetName(e.target.value)}
            placeholder="e.g. Monthly sales export"
            maxLength={200}
            disabled={busy}
          />
        </Field>
      ) : null}

      <Field label="File" hint={`${ACCEPTED_EXTENSIONS.join(', ')} · up to ${formatBytes(MAX_UPLOAD_BYTES)}`}>
        <input
          ref={inputRef}
          className={inputClass}
          type="file"
          accept={ACCEPTED_EXTENSIONS.join(',')}
          disabled={busy}
        />
      </Field>

      <ErrorText>{error}</ErrorText>

      <div className="flex items-center gap-3">
        <button className={buttonClass} type="submit" disabled={busy}>
          {busy ? PHASE_LABEL[phase] : 'Upload'}
        </button>
        {busy ? <span className="text-sm opacity-60">{PHASE_LABEL[phase]}</span> : null}
      </div>
    </form>
  );
}

/**
 * SHA-256 of the file, computed in the browser.
 *
 * Not a security control -- the client could send any string. It is the signal
 * for "this client re-sent last month's file", which the deviation engine will
 * want in Week 4 and which is cheap to capture now.
 */
async function sha256Hex(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', buffer);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}
