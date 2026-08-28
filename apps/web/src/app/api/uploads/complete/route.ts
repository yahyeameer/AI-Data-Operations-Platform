import { NextResponse } from 'next/server';
import { z } from 'zod';

import { handleRouteError } from '@/lib/api';
import { adminFor, requireWorkspaceAccess } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';
import { RAW_BUCKET } from '@/lib/storage';

/**
 * Step 2 of the upload: confirm the object actually landed, then promote the
 * reservation to `stored` and open the dataset's lineage chain.
 *
 * The server verifies the object independently rather than believing the
 * client's "done" -- a claimed upload with no bytes behind it would otherwise
 * produce a dataset version pointing at nothing, which is exactly the sort of
 * quiet inconsistency section 7's provenance promise cannot survive.
 *
 * The sha256 comes from the browser and is recorded as-is. It is not a security
 * control (the client could lie about it); it is the "did this client send the
 * same file twice?" signal that Week 2's source_signature matching builds on.
 */

const requestSchema = z.object({
  uploadId: z.string().uuid(),
  workspaceId: z.string().uuid(),
  sha256: z
    .string()
    .regex(/^[0-9a-f]{64}$/, 'sha256 must be 64 lowercase hex characters')
    .nullish(),
  // What the accountant told the agent to do with this file. Recorded on the
  // audit row rather than a column: it is a human instruction attached to one
  // upload, and the audit trail is where the reason for a change belongs.
  instructions: z.string().trim().max(4000).nullish(),
});

export async function POST(request: Request) {
  try {
    const body = requestSchema.parse(await request.json());

    const context = await requireWorkspaceAccess(body.workspaceId);
    const supabase = await createServerSupabase();
    const admin = adminFor(context);
    const db = supabase;

    const { data: upload, error: loadError } = await db
      .from('raw_uploads')
      .select('id, workspace_id, dataset_id, storage_path, original_filename, status')
      .eq('id', body.uploadId)
      .eq('workspace_id', context.workspaceId)
      .maybeSingle();

    if (loadError) {
      return NextResponse.json({ error: `Upload lookup failed: ${loadError.message}` }, { status: 500 });
    }
    if (!upload) {
      return NextResponse.json({ error: 'Upload not found' }, { status: 404 });
    }
    if (upload.status !== 'pending') {
      // Completing twice is a retry, not an error. The row is already correct
      // and immutable, so report success without touching it.
      return NextResponse.json({ uploadId: upload.id, status: upload.status, alreadyCompleted: true });
    }

    // Verify the object exists and read its true size from storage.
    const lastSlash = upload.storage_path.lastIndexOf('/');
    const directory = upload.storage_path.slice(0, lastSlash);
    const objectName = upload.storage_path.slice(lastSlash + 1);

    const isServiceRole = Boolean(
      process.env.SUPABASE_SECRET_KEY &&
      !process.env.SUPABASE_SECRET_KEY.startsWith('sb_publishable_') &&
      process.env.SUPABASE_SECRET_KEY !== process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY
    );
    const storageClient = isServiceRole ? admin.storage : db.storage;
    const { data: listed, error: listError } = await storageClient
      .from(RAW_BUCKET)
      .list(directory, { search: objectName, limit: 1 });

    const object = listed?.find((entry) => entry.name === objectName);

    if (listError || !object) {
      await db.from('raw_uploads').update({ status: 'failed' }).eq('id', upload.id);

      await db.rpc('write_audit', {
        p_org_id: context.orgId,
        p_workspace_id: context.workspaceId,
        p_action: 'upload.failed',
        p_entity_type: 'raw_upload',
        p_entity_id: upload.id,
        p_metadata: { reason: 'object missing from storage', storage_path: upload.storage_path },
      });

      return NextResponse.json({ error: 'Uploaded object not found in storage' }, { status: 409 });
    }

    const actualSize = (object.metadata as { size?: number } | null)?.size ?? null;

    const { error: updateError } = await db
      .from('raw_uploads')
      .update({
        status: 'stored',
        byte_size: actualSize,
        sha256: body.sha256 ?? null,
        completed_at: new Date().toISOString(),
      })
      .eq('id', upload.id);

    if (updateError) {
      return NextResponse.json({ error: `Could not finalise upload: ${updateError.message}` }, { status: 500 });
    }

    // Open the lineage chain (section 3). v0 is the raw file exactly as it
    // arrived; Week 2's parser writes v1 with this row as its parent.
    let datasetVersionId: string | null = null;

    if (upload.dataset_id) {
      const { data: version, error: versionError } = await db
        .from('dataset_versions')
        .insert({
          dataset_id: upload.dataset_id,
          parent_version_id: null,
          version_no: 0,
          kind: 'raw',
          raw_upload_id: upload.id,
          created_by: context.user.id,
        })
        .select('id')
        .maybeSingle();

      // A dataset already holding a v0 hits the (dataset_id, version_no) unique
      // constraint. That means a second month's file for the same dataset, and
      // it is not an error -- it simply has no v0 of its own to claim. Week 2
      // gives it a real version number once the parser has run.
      if (versionError && versionError.code !== '23505') {
        return NextResponse.json(
          { error: `Could not record dataset version: ${versionError.message}` },
          { status: 500 },
        );
      }

      datasetVersionId = version?.id ?? null;
    }

    await db.rpc('write_audit', {
      p_org_id: context.orgId,
      p_workspace_id: context.workspaceId,
      p_action: 'upload.stored',
      p_entity_type: 'raw_upload',
      p_entity_id: upload.id,
      p_metadata: {
        original_filename: upload.original_filename,
        storage_path: upload.storage_path,
        byte_size: actualSize,
        sha256: body.sha256 ?? null,
        dataset_id: upload.dataset_id,
        dataset_version_id: datasetVersionId,
        cleaning_instructions: body.instructions || null,
      },
    });

    // Queue the analysis instead of running it.
    //
    // This route used to download the workbook into the function and push the
    // bytes to the parser over HTTP, then wait. That made finishing an upload
    // depend on the parser being awake -- a cold start on the engine surfaced
    // here as a failed upload, for a file that was already safely stored.
    //
    // Now the work becomes a row. The worker claims it whenever it is up and
    // writes the result back, and the browser polls /api/analyze/:id. Nothing
    // on this request waits for the engine, so nothing on it can time out.
    const { data: job, error: enqueueError } = await db.rpc('enqueue_agent_job', {
      p_workspace_id: context.workspaceId,
      p_kind: 'analyze_workbook',
      p_raw_upload_id: upload.id,
      p_dataset_id: upload.dataset_id ?? undefined,
      p_payload: (body.instructions ? { instructions: body.instructions } : {}) as never,
    });

    if (enqueueError) {
      // The upload itself succeeded and is audited. Failing to queue the
      // analysis is worth reporting, but it must not turn a stored file into an
      // error -- the user can press Analyze again.
      console.error('[uploads/complete] could not queue analysis:', enqueueError.message);
    }

    return NextResponse.json({
      uploadId: upload.id,
      status: 'stored',
      byteSize: actualSize,
      datasetVersionId,
      jobId: (job as { id?: string } | null)?.id ?? null,
    });
  } catch (error) {
    return handleRouteError(error);
  }
}
