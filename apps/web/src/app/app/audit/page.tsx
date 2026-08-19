import { EmptyState, PageHeader } from '@/components/ui';
import { requireCurrentOrg } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';

export const metadata = { title: 'Audit log · AI Data Operations' };

/**
 * The audit trail required by section 13. Append-only in the database, so what
 * is shown here is what happened -- there is no code path, including this
 * application's own, that can rewrite it.
 */
export default async function AuditPage() {
  const { org } = await requireCurrentOrg();
  const supabase = await createServerSupabase();

  const [{ data: entries, error }, { data: workspaces }] = await Promise.all([
    supabase
      .from('audit_logs')
      .select('id, action, entity_type, entity_id, workspace_id, actor_user_id, metadata, created_at')
      .eq('org_id', org.id)
      .order('created_at', { ascending: false })
      .limit(200),
    supabase.from('workspaces').select('id, name').eq('org_id', org.id),
  ]);

  if (error) throw new Error(`Could not load the audit log: ${error.message}`);

  const workspaceNames = new Map((workspaces ?? []).map((w) => [w.id, w.name]));

  return (
    <>
      <PageHeader
        title="Audit log"
        subtitle="Every action, in order, with who did it and when. Entries cannot be edited or deleted."
      />

      {!entries || entries.length === 0 ? (
        <EmptyState title="No activity yet" body="Actions appear here as soon as anyone takes them." />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-black/10 dark:border-white/15">
          <table className="w-full min-w-[46rem] text-sm">
            <thead className="border-b border-black/10 text-left text-xs uppercase tracking-wide opacity-60 dark:border-white/15">
              <tr>
                <th className="px-4 py-2 font-medium">When</th>
                <th className="px-4 py-2 font-medium">Action</th>
                <th className="px-4 py-2 font-medium">Workspace</th>
                <th className="px-4 py-2 font-medium">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-black/5 dark:divide-white/10">
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <td className="whitespace-nowrap px-4 py-2 align-top opacity-70">
                    {new Date(entry.created_at).toLocaleString('en-GB')}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2 align-top font-medium">{entry.action}</td>
                  <td className="px-4 py-2 align-top opacity-70">
                    {entry.workspace_id ? workspaceNames.get(entry.workspace_id) ?? '—' : '—'}
                  </td>
                  <td className="px-4 py-2 align-top">
                    <code className="break-all text-xs opacity-70">
                      {summarise(entry.metadata)}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/**
 * Metadata is free-form jsonb; show the fields a human actually scans for.
 *
 * A whitelist rather than a dump. Some of these payloads carry the whole
 * evidence tree behind a proposal, and a log line that wraps over six rows is
 * one nobody reads — which defeats the point of having a log.
 */
const INTERESTING_KEYS = [
  // Week 1: organizations, workspaces, uploads.
  'name',
  'original_filename',
  'client_name',
  'slug',
  'byte_size',
  'reason',
  // The agent.
  'kind',
  'version_no',
  'row_count',
  'count',
  'attempt',
  'worker',
  'group_keys',
  'model',
  'question',
  'error',
];

function summarise(metadata: unknown): string {
  if (!metadata || typeof metadata !== 'object') return '—';

  const record = metadata as Record<string, unknown>;

  const parts = INTERESTING_KEYS.filter(
    (key) => record[key] !== undefined && record[key] !== null,
  ).map((key) => {
    const value = record[key];
    // An approval can cover a dozen groups. The count is what is scannable;
    // the group names are in the proposals themselves.
    if (Array.isArray(value)) {
      return value.length <= 3
        ? `${key}=${value.join(',')}`
        : `${key}=${value.length} groups`;
    }
    return `${key}=${String(value)}`;
  });

  return parts.length > 0 ? parts.join('  ') : '—';
}
