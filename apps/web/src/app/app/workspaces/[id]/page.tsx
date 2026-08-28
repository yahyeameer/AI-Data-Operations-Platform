import { notFound } from 'next/navigation';

import { AgentPanel, AnalyseButton, ExportButton } from '@/components/agent-panel';
import { ReviewQueue, type ProposedChange } from '@/components/review-queue';
import { UploadPanel } from '@/components/upload-panel';
import { Card, EmptyState, PageHeader, StatusBadge } from '@/components/ui';
import { requireCurrentOrg } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';
import { formatBytes } from '@/lib/storage';

export default async function WorkspacePage({ params }: PageProps<'/app/workspaces/[id]'>) {
  const { id } = await params;
  const { org } = await requireCurrentOrg();
  const supabase = await createServerSupabase();

  const { data: workspace } = await supabase
    .from('workspaces')
    .select('id, name, client_name, org_id')
    .eq('id', id)
    .maybeSingle();

  // RLS already hides other tenants' workspaces, so a miss here is a 404 rather
  // than a 403 -- the API should not confirm that someone else's id is real.
  if (!workspace || workspace.org_id !== org.id) notFound();

  const [{ data: datasets }, { data: uploads }, { data: jobs }, { data: workers }] =
    await Promise.all([
      supabase
        .from('datasets')
        .select('id, name, source_signature')
        .eq('workspace_id', workspace.id)
        .order('created_at', { ascending: true }),
      supabase
        .from('raw_uploads')
        .select('id, original_filename, byte_size, status, created_at, sha256, dataset_id')
        .eq('workspace_id', workspace.id)
        .order('created_at', { ascending: false })
        .limit(50),
      supabase
        .from('agent_jobs')
        .select('id, kind, status, progress, result, error, attempts, created_at, finished_at')
        .eq('workspace_id', workspace.id)
        .order('created_at', { ascending: false })
        .limit(20),
      supabase
        .from('agent_workers')
        .select('id, hostname, version, last_seen_at, jobs_claimed, metadata')
        .order('last_seen_at', { ascending: false })
        .limit(5),
    ]);

  const datasetNames = new Map((datasets ?? []).map((d) => [d.id, d.name]));
  const datasetIds = (datasets ?? []).map((d) => d.id);

  // The review queue belongs to a version, not to the workspace, so find the
  // most recent version that still has something to decide. Anything older has
  // either been dealt with or superseded by a re-analysis.
  let reviewVersionId: string | null = null;
  let changes: ProposedChange[] = [];
  let latestVersion: { id: string; version_no: number; row_count: number | null } | null = null;

  if (datasetIds.length > 0) {
    const { data: versions } = await supabase
      .from('dataset_versions')
      .select('id, version_no, row_count, dataset_id, created_at')
      .in('dataset_id', datasetIds)
      .order('created_at', { ascending: false })
      .limit(1);

    latestVersion = versions?.[0] ?? null;

    const { data: openChanges } = await supabase
      .from('proposed_changes')
      .select(
        'id, group_key, step_type, column_name, title, rationale, confidence, affected_rows, materiality_gbp, status, evidence, dataset_version_id',
      )
      .eq('workspace_id', workspace.id)
      .in('status', ['pending', 'approved'])
      // Descending, because the enum is declared high -> medium -> low and the
      // queue has to lead with the blocking items. It matters beyond
      // presentation: with the limit below, ascending would be the order that
      // truncates away the blockers.
      .order('confidence', { ascending: false })
      .order('materiality_gbp', { ascending: false, nullsFirst: false })
      .limit(100);

    if (openChanges && openChanges.length > 0) {
      reviewVersionId = openChanges[0].dataset_version_id;
      changes = openChanges.filter(
        (change) => change.dataset_version_id === reviewVersionId,
      );
    }
  }

  return (
    <>
      <PageHeader title={workspace.name} subtitle={workspace.client_name ?? 'Client workspace'} />

      <div className="mb-6">
        <AgentPanel
          workspaceId={workspace.id}
          initialJobs={jobs ?? []}
          initialWorkers={workers ?? []}
        />
      </div>

      {reviewVersionId && changes.length > 0 ? (
        <div className="mb-8">
          <ReviewQueue
            workspaceId={workspace.id}
            datasetVersionId={reviewVersionId}
            changes={changes}
          />
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[1fr_1.4fr]">
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide opacity-60">
            Upload a file
          </h2>
          <Card>
            <UploadPanel workspaceId={workspace.id} datasets={datasets ?? []} />
          </Card>
          <p className="mt-3 text-xs opacity-60">
            Files are stored exactly as they arrived. Nothing is modified in place — cleaning writes
            a new version and leaves the original intact.
          </p>

          {latestVersion ? (
            <>
              <p className="mt-3 text-xs opacity-60">
                Latest dataset version: v{latestVersion.version_no}
                {latestVersion.row_count !== null ? ` · ${latestVersion.row_count} rows` : ''}
              </p>
              <div className="mt-2">
                <ExportButton workspaceId={workspace.id} datasetVersionId={latestVersion.id} />
              </div>
            </>
          ) : null}
        </section>

        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide opacity-60">Uploads</h2>

          {!uploads || uploads.length === 0 ? (
            <EmptyState
              title="Nothing uploaded yet"
              body="Upload the file this client sends you every month. Once it is stored, hand it to the agent and it will read it, profile it and tell you what needs fixing."
            />
          ) : (
            <ul className="divide-y divide-black/10 rounded-lg border border-black/10 dark:divide-white/10 dark:border-white/15">
              {uploads.map((upload) => (
                <li key={upload.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{upload.original_filename}</p>
                    <p className="text-xs opacity-60">
                      {upload.dataset_id
                        ? (datasetNames.get(upload.dataset_id) ?? 'Unknown dataset')
                        : 'No dataset'}
                      {' · '}
                      {new Date(upload.created_at).toLocaleString('en-GB')}
                      {' · '}
                      {formatBytes(upload.byte_size)}
                    </p>
                  </div>

                  {upload.status === 'stored' ? (
                    <AnalyseButton
                      workspaceId={workspace.id}
                      uploadId={upload.id}
                      datasetId={upload.dataset_id}
                    />
                  ) : null}

                  <StatusBadge status={upload.status} />
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}
