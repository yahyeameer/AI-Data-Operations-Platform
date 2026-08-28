'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

import {
  ACCEPTED_EXTENSIONS,
  MAX_UPLOAD_BYTES,
  formatBytes,
  isAcceptedFilename,
} from '@/lib/storage';
import {
  UPLOAD_PHASE_LABEL,
  UPLOAD_PHASE_PROGRESS,
  uploadStatement,
  type UploadPhase,
} from '@/lib/upload-client';
import { ErrorText, Field, ProgressBar, Spinner, buttonClass, buttonStyle, inputClass, inputFocusHandler, inputStyle } from '@/components/ui';
import { useAnalysisJob } from '@/lib/analysis-job';
import { AnalysisResult } from '@/components/analysis-result';

type Dataset = { id: string; name: string };

export function UploadPanel({
  workspaceId,
  datasets,
}: {
  workspaceId: string;
  datasets: Dataset[];
}) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<UploadPhase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [datasetId, setDatasetId] = useState<string>(datasets[0]?.id ?? '');
  const [datasetName, setDatasetName] = useState('');
  const [agentInstructions, setAgentInstructions] = useState('');
  const [dragActive, setDragActive] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  // The analysis is a queued job now, so what the upload hands back is an id to
  // watch rather than a result to render.
  const [jobId, setJobId] = useState<string | null>(null);

  const { job, engineAwake, error: pollError } = useAnalysisJob(jobId);

  // Sync datasetId if workspaceId or datasets list changes
  useEffect(() => {
    const valid = datasets.some((d) => d.id === datasetId);
    if (!valid) {
      setDatasetId(datasets[0]?.id ?? '');
    }
  }, [workspaceId, datasets, datasetId]);

  const creatingNewDataset = datasetId === '';
  const busy = phase !== 'idle';

  function handleFileChange(file: File | undefined) {
    if (!file) return;
    setError(null);
    if (!isAcceptedFilename(file.name)) {
      setError(`Only ${ACCEPTED_EXTENSIONS.join(', ')} files are accepted`);
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(`File is ${formatBytes(file.size)}; maximum allowed is ${formatBytes(MAX_UPLOAD_BYTES)}`);
      return;
    }
    setSelectedFile(file);
  }

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    const file = selectedFile || inputRef.current?.files?.[0];
    if (!file) {
      setError('Select a file to upload first');
      return;
    }

    const fallbackName = file.name.replace(/\.[^/.]+$/, '').trim() || 'Bank statements';
    const effectiveDatasetName = creatingNewDataset
      ? (datasetName.trim() || fallbackName)
      : null;

    setJobId(null);

    try {
      const { datasetId: createdDatasetId, jobId: queuedJobId } = await uploadStatement({
        workspaceId,
        file,
        datasetId: creatingNewDataset ? null : datasetId,
        datasetName: effectiveDatasetName,
        instructions: agentInstructions,
        onPhase: setPhase,
      });

      if (inputRef.current) inputRef.current.value = '';
      setSelectedFile(null);
      setDatasetName('');
      setAgentInstructions('');
      if (createdDatasetId) setDatasetId(createdDatasetId);

      // The file is stored either way. A null id means only that queueing the
      // analysis failed, which is worth saying plainly rather than leaving the
      // user watching a panel that will never fill in.
      if (queuedJobId) {
        setJobId(queuedJobId);
      } else {
        setError('The file was uploaded, but the analysis could not be queued. Try again.');
      }

      router.refresh();
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Upload failed');
    } finally {
      setPhase('idle');
    }
  }

  return (
    <form onSubmit={onSubmit} method="post" className="space-y-5">
      <Field
        label="Statement set"
        hint="Group a client's monthly statement exports together under one versioned dataset."
      >
        <select
          className={inputClass}
          style={inputStyle}
          {...inputFocusHandler}
          value={datasetId}
          onChange={(e) => setDatasetId(e.target.value)}
          disabled={busy}
        >
          {datasets.map((dataset) => (
            <option key={dataset.id} value={dataset.id}>
              {dataset.name}
            </option>
          ))}
          <option value="">+ New recurring dataset…</option>
        </select>
      </Field>

      {creatingNewDataset && (
        <Field label="New Dataset Name">
          <input
            className={inputClass}
            style={inputStyle}
            {...inputFocusHandler}
            value={datasetName}
            onChange={(e) => setDatasetName(e.target.value)}
            placeholder="e.g. Barclays Current Account — Monthly"
            maxLength={200}
            disabled={busy}
          />
        </Field>
      )}

      {/* Hermes Agent Prompt Instructions */}
      <Field
        label="Cleaning instructions (optional)"
        hint="Recorded with the upload and used when this statement is processed (e.g. drop carried-forward balance rows, treat bracketed amounts as negative, split combined debit/credit columns)."
      >
        <textarea
          className={inputClass}
          style={{ ...inputStyle, minHeight: '80px' }}
          {...inputFocusHandler}
          value={agentInstructions}
          onChange={(e) => setAgentInstructions(e.target.value)}
          placeholder="e.g. Ignore the running-balance rows, normalize dates to ISO-8601, and treat amounts in brackets as money out."
          disabled={busy}
        />
      </Field>

      {/* Drag & Drop File Zone */}
      <Field label="Bank statement file" hint={`Supports ${ACCEPTED_EXTENSIONS.join(', ')} · Up to ${formatBytes(MAX_UPLOAD_BYTES)}`}>
        <div
          onDragOver={(e) => { e.preventDefault(); setDragActive(true); }}
          onDragLeave={() => setDragActive(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragActive(false);
            const file = e.dataTransfer.files?.[0];
            handleFileChange(file);
          }}
          onClick={() => inputRef.current?.click()}
          className="group relative flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed p-6 text-center transition-all duration-200"
          style={{
            borderColor: dragActive ? 'var(--az-primary-500)' : selectedFile ? 'var(--az-success-500)' : 'var(--az-border)',
            background: dragActive ? 'rgba(99,102,241,.06)' : selectedFile ? 'rgba(16,185,129,.04)' : 'var(--az-bg-card)',
          }}
        >
          <input
            ref={inputRef}
            type="file"
            className="hidden"
            accept={ACCEPTED_EXTENSIONS.join(',')}
            disabled={busy}
            onChange={(e) => handleFileChange(e.target.files?.[0])}
          />

          <div
            className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl transition-transform group-hover:scale-110"
            style={{
              background: selectedFile ? 'rgba(16,185,129,.1)' : 'var(--az-gradient-card)',
              color: selectedFile ? 'var(--az-success-500)' : 'var(--az-primary-500)',
            }}
          >
            {selectedFile ? (
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                <polyline points="22 4 12 14.01 9 11.01" />
              </svg>
            ) : (
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="17 8 12 3 7 8" />
                <line x1="12" y1="3" x2="12" y2="15" />
              </svg>
            )}
          </div>

          {selectedFile ? (
            <div>
              <p className="text-sm font-bold" style={{ color: 'var(--az-text)' }}>
                {selectedFile.name}
              </p>
              <p className="text-xs mt-0.5" style={{ color: 'var(--az-text-muted)' }}>
                {formatBytes(selectedFile.size)} — Click or drop to replace
              </p>
            </div>
          ) : (
            <div>
              <p className="text-sm font-semibold" style={{ color: 'var(--az-text)' }}>
                Click to select or drag and drop a bank statement
              </p>
              <p className="text-xs mt-0.5" style={{ color: 'var(--az-text-subtle)' }}>
                Raw files are hashed, versioned, and queued for analysis
              </p>
            </div>
          )}
        </div>
      </Field>

      {busy && (
        <div className="az-animate-in">
          <ProgressBar progress={UPLOAD_PHASE_PROGRESS[phase]} label={UPLOAD_PHASE_LABEL[phase]} />
        </div>
      )}

      <ErrorText>{error}</ErrorText>

      <button className={`${buttonClass} w-full`} style={buttonStyle} type="submit" disabled={busy || !selectedFile}>
        {busy ? (
          <>
            <Spinner size={18} />
            <span>Uploading…</span>
          </>
        ) : (
          'Upload statement'
        )}
      </button>

      {jobId && (
        <div className="az-animate-in pt-2">
          <AnalysisResult job={job} engineAwake={engineAwake} pollError={pollError} />
        </div>
      )}
    </form>
  );
}
