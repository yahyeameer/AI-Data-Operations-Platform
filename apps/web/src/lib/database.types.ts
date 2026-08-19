export type Json =
  | string
  | number
  | boolean
  | null
  | { [key: string]: Json | undefined }
  | Json[]

export type Database = {
  graphql_public: {
    Tables: {
      [_ in never]: never
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      graphql: {
        Args: {
          extensions?: Json
          operationName?: string
          query?: string
          variables?: Json
        }
        Returns: Json
      }
    }
    Enums: {
      [_ in never]: never
    }
    CompositeTypes: {
      [_ in never]: never
    }
  }
  public: {
    Tables: {
      agent_jobs: {
        Row: {
          attempts: number
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          dataset_id: string | null
          dataset_version_id: string | null
          error: string | null
          finished_at: string | null
          id: string
          kind: Database["public"]["Enums"]["agent_job_kind"]
          lease_expires_at: string | null
          max_attempts: number
          org_id: string
          payload: Json
          priority: number
          progress: Json
          raw_upload_id: string | null
          requested_by: string | null
          result: Json | null
          started_at: string | null
          status: Database["public"]["Enums"]["agent_job_status"]
          workspace_id: string
        }
        Insert: {
          attempts?: number
          claimed_at?: string | null
          claimed_by?: string | null
          created_at?: string
          dataset_id?: string | null
          dataset_version_id?: string | null
          error?: string | null
          finished_at?: string | null
          id?: string
          kind: Database["public"]["Enums"]["agent_job_kind"]
          lease_expires_at?: string | null
          max_attempts?: number
          org_id: string
          payload?: Json
          priority?: number
          progress?: Json
          raw_upload_id?: string | null
          requested_by?: string | null
          result?: Json | null
          started_at?: string | null
          status?: Database["public"]["Enums"]["agent_job_status"]
          workspace_id: string
        }
        Update: {
          attempts?: number
          claimed_at?: string | null
          claimed_by?: string | null
          created_at?: string
          dataset_id?: string | null
          dataset_version_id?: string | null
          error?: string | null
          finished_at?: string | null
          id?: string
          kind?: Database["public"]["Enums"]["agent_job_kind"]
          lease_expires_at?: string | null
          max_attempts?: number
          org_id?: string
          payload?: Json
          priority?: number
          progress?: Json
          raw_upload_id?: string | null
          requested_by?: string | null
          result?: Json | null
          started_at?: string | null
          status?: Database["public"]["Enums"]["agent_job_status"]
          workspace_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "agent_jobs_claimed_by_fkey"
            columns: ["claimed_by"]
            isOneToOne: false
            referencedRelation: "agent_workers"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "agent_jobs_dataset_id_fkey"
            columns: ["dataset_id"]
            isOneToOne: false
            referencedRelation: "datasets"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "agent_jobs_dataset_version_id_fkey"
            columns: ["dataset_version_id"]
            isOneToOne: false
            referencedRelation: "dataset_versions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "agent_jobs_org_id_fkey"
            columns: ["org_id"]
            isOneToOne: false
            referencedRelation: "organizations"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "agent_jobs_raw_upload_id_fkey"
            columns: ["raw_upload_id"]
            isOneToOne: false
            referencedRelation: "raw_uploads"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "agent_jobs_workspace_id_fkey"
            columns: ["workspace_id"]
            isOneToOne: false
            referencedRelation: "workspaces"
            referencedColumns: ["id"]
          },
        ]
      }
      agent_workers: {
        Row: {
          capabilities: Database["public"]["Enums"]["agent_job_kind"][]
          hostname: string | null
          id: string
          jobs_claimed: number
          last_seen_at: string
          metadata: Json
          started_at: string
          version: string | null
        }
        Insert: {
          capabilities?: Database["public"]["Enums"]["agent_job_kind"][]
          hostname?: string | null
          id: string
          jobs_claimed?: number
          last_seen_at?: string
          metadata?: Json
          started_at?: string
          version?: string | null
        }
        Update: {
          capabilities?: Database["public"]["Enums"]["agent_job_kind"][]
          hostname?: string | null
          id?: string
          jobs_claimed?: number
          last_seen_at?: string
          metadata?: Json
          started_at?: string
          version?: string | null
        }
        Relationships: []
      }
      analysis_runs: {
        Row: {
          created_at: string
          created_by: string | null
          dataset_version_id: string
          duration_ms: number | null
          executed_sql: string
          id: string
          job_id: string | null
          model_used: string | null
          question: string | null
          result: Json
          row_refs: Json
          workspace_id: string
        }
        Insert: {
          created_at?: string
          created_by?: string | null
          dataset_version_id: string
          duration_ms?: number | null
          executed_sql: string
          id?: string
          job_id?: string | null
          model_used?: string | null
          question?: string | null
          result?: Json
          row_refs?: Json
          workspace_id: string
        }
        Update: {
          created_at?: string
          created_by?: string | null
          dataset_version_id?: string
          duration_ms?: number | null
          executed_sql?: string
          id?: string
          job_id?: string | null
          model_used?: string | null
          question?: string | null
          result?: Json
          row_refs?: Json
          workspace_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "analysis_runs_dataset_version_id_fkey"
            columns: ["dataset_version_id"]
            isOneToOne: false
            referencedRelation: "dataset_versions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "analysis_runs_job_id_fkey"
            columns: ["job_id"]
            isOneToOne: false
            referencedRelation: "agent_jobs"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "analysis_runs_workspace_id_fkey"
            columns: ["workspace_id"]
            isOneToOne: false
            referencedRelation: "workspaces"
            referencedColumns: ["id"]
          },
        ]
      }
      audit_logs: {
        Row: {
          action: string
          actor_user_id: string | null
          created_at: string
          entity_id: string | null
          entity_type: string
          id: number
          metadata: Json
          org_id: string
          workspace_id: string | null
        }
        Insert: {
          action: string
          actor_user_id?: string | null
          created_at?: string
          entity_id?: string | null
          entity_type: string
          id?: never
          metadata?: Json
          org_id: string
          workspace_id?: string | null
        }
        Update: {
          action?: string
          actor_user_id?: string | null
          created_at?: string
          entity_id?: string | null
          entity_type?: string
          id?: never
          metadata?: Json
          org_id?: string
          workspace_id?: string | null
        }
        Relationships: [
          {
            foreignKeyName: "audit_logs_org_id_fkey"
            columns: ["org_id"]
            isOneToOne: false
            referencedRelation: "organizations"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "audit_logs_workspace_id_fkey"
            columns: ["workspace_id"]
            isOneToOne: false
            referencedRelation: "workspaces"
            referencedColumns: ["id"]
          },
        ]
      }
      dataset_profiles: {
        Row: {
          column_count: number
          columns: Json
          created_at: string
          dataset_version_id: string
          id: string
          produced_by_job_id: string | null
          row_count: number
          signals: Json
        }
        Insert: {
          column_count: number
          columns?: Json
          created_at?: string
          dataset_version_id: string
          id?: string
          produced_by_job_id?: string | null
          row_count: number
          signals?: Json
        }
        Update: {
          column_count?: number
          columns?: Json
          created_at?: string
          dataset_version_id?: string
          id?: string
          produced_by_job_id?: string | null
          row_count?: number
          signals?: Json
        }
        Relationships: [
          {
            foreignKeyName: "dataset_profiles_dataset_version_id_fkey"
            columns: ["dataset_version_id"]
            isOneToOne: true
            referencedRelation: "dataset_versions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "dataset_profiles_produced_by_job_id_fkey"
            columns: ["produced_by_job_id"]
            isOneToOne: false
            referencedRelation: "agent_jobs"
            referencedColumns: ["id"]
          },
        ]
      }
      dataset_versions: {
        Row: {
          column_hash: string | null
          created_at: string
          created_by: string | null
          dataset_id: string
          id: string
          kind: Database["public"]["Enums"]["dataset_version_kind"]
          parent_version_id: string | null
          parquet_path: string | null
          produced_by_run_id: string | null
          raw_upload_id: string | null
          row_count: number | null
          version_no: number
        }
        Insert: {
          column_hash?: string | null
          created_at?: string
          created_by?: string | null
          dataset_id: string
          id?: string
          kind?: Database["public"]["Enums"]["dataset_version_kind"]
          parent_version_id?: string | null
          parquet_path?: string | null
          produced_by_run_id?: string | null
          raw_upload_id?: string | null
          row_count?: number | null
          version_no: number
        }
        Update: {
          column_hash?: string | null
          created_at?: string
          created_by?: string | null
          dataset_id?: string
          id?: string
          kind?: Database["public"]["Enums"]["dataset_version_kind"]
          parent_version_id?: string | null
          parquet_path?: string | null
          produced_by_run_id?: string | null
          raw_upload_id?: string | null
          row_count?: number | null
          version_no?: number
        }
        Relationships: [
          {
            foreignKeyName: "dataset_versions_dataset_id_fkey"
            columns: ["dataset_id"]
            isOneToOne: false
            referencedRelation: "datasets"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "dataset_versions_parent_version_id_fkey"
            columns: ["parent_version_id"]
            isOneToOne: false
            referencedRelation: "dataset_versions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "dataset_versions_raw_upload_id_fkey"
            columns: ["raw_upload_id"]
            isOneToOne: false
            referencedRelation: "raw_uploads"
            referencedColumns: ["id"]
          },
        ]
      }
      datasets: {
        Row: {
          created_at: string
          created_by: string
          id: string
          name: string
          source_signature: string | null
          workspace_id: string
        }
        Insert: {
          created_at?: string
          created_by: string
          id?: string
          name: string
          source_signature?: string | null
          workspace_id: string
        }
        Update: {
          created_at?: string
          created_by?: string
          id?: string
          name?: string
          source_signature?: string | null
          workspace_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "datasets_workspace_id_fkey"
            columns: ["workspace_id"]
            isOneToOne: false
            referencedRelation: "workspaces"
            referencedColumns: ["id"]
          },
        ]
      }
      organization_members: {
        Row: {
          created_at: string
          org_id: string
          role: Database["public"]["Enums"]["org_role"]
          user_id: string
        }
        Insert: {
          created_at?: string
          org_id: string
          role?: Database["public"]["Enums"]["org_role"]
          user_id: string
        }
        Update: {
          created_at?: string
          org_id?: string
          role?: Database["public"]["Enums"]["org_role"]
          user_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "organization_members_org_id_fkey"
            columns: ["org_id"]
            isOneToOne: false
            referencedRelation: "organizations"
            referencedColumns: ["id"]
          },
        ]
      }
      organizations: {
        Row: {
          created_at: string
          created_by: string
          id: string
          name: string
          slug: string
        }
        Insert: {
          created_at?: string
          created_by: string
          id?: string
          name: string
          slug: string
        }
        Update: {
          created_at?: string
          created_by?: string
          id?: string
          name?: string
          slug?: string
        }
        Relationships: []
      }
      proposed_changes: {
        Row: {
          affected_rows: number
          column_name: string | null
          confidence: Database["public"]["Enums"]["change_confidence"]
          created_at: string
          dataset_version_id: string
          decided_at: string | null
          decided_by: string | null
          decision_note: string | null
          evidence: Json
          group_key: string
          id: string
          job_id: string | null
          materiality_gbp: number | null
          operation: Json
          rationale: string
          status: Database["public"]["Enums"]["proposed_change_status"]
          step_type: string
          title: string
          workspace_id: string
        }
        Insert: {
          affected_rows?: number
          column_name?: string | null
          confidence: Database["public"]["Enums"]["change_confidence"]
          created_at?: string
          dataset_version_id: string
          decided_at?: string | null
          decided_by?: string | null
          decision_note?: string | null
          evidence?: Json
          group_key: string
          id?: string
          job_id?: string | null
          materiality_gbp?: number | null
          operation: Json
          rationale: string
          status?: Database["public"]["Enums"]["proposed_change_status"]
          step_type: string
          title: string
          workspace_id: string
        }
        Update: {
          affected_rows?: number
          column_name?: string | null
          confidence?: Database["public"]["Enums"]["change_confidence"]
          created_at?: string
          dataset_version_id?: string
          decided_at?: string | null
          decided_by?: string | null
          decision_note?: string | null
          evidence?: Json
          group_key?: string
          id?: string
          job_id?: string | null
          materiality_gbp?: number | null
          operation?: Json
          rationale?: string
          status?: Database["public"]["Enums"]["proposed_change_status"]
          step_type?: string
          title?: string
          workspace_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "proposed_changes_dataset_version_id_fkey"
            columns: ["dataset_version_id"]
            isOneToOne: false
            referencedRelation: "dataset_versions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "proposed_changes_job_id_fkey"
            columns: ["job_id"]
            isOneToOne: false
            referencedRelation: "agent_jobs"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "proposed_changes_workspace_id_fkey"
            columns: ["workspace_id"]
            isOneToOne: false
            referencedRelation: "workspaces"
            referencedColumns: ["id"]
          },
        ]
      }
      raw_uploads: {
        Row: {
          byte_size: number | null
          completed_at: string | null
          created_at: string
          dataset_id: string | null
          id: string
          mime_type: string | null
          original_filename: string
          sha256: string | null
          status: Database["public"]["Enums"]["upload_status"]
          storage_path: string
          uploaded_by: string
          workspace_id: string
        }
        Insert: {
          byte_size?: number | null
          completed_at?: string | null
          created_at?: string
          dataset_id?: string | null
          id?: string
          mime_type?: string | null
          original_filename: string
          sha256?: string | null
          status?: Database["public"]["Enums"]["upload_status"]
          storage_path: string
          uploaded_by: string
          workspace_id: string
        }
        Update: {
          byte_size?: number | null
          completed_at?: string | null
          created_at?: string
          dataset_id?: string | null
          id?: string
          mime_type?: string | null
          original_filename?: string
          sha256?: string | null
          status?: Database["public"]["Enums"]["upload_status"]
          storage_path?: string
          uploaded_by?: string
          workspace_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "raw_uploads_dataset_id_fkey"
            columns: ["dataset_id"]
            isOneToOne: false
            referencedRelation: "datasets"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "raw_uploads_workspace_id_fkey"
            columns: ["workspace_id"]
            isOneToOne: false
            referencedRelation: "workspaces"
            referencedColumns: ["id"]
          },
        ]
      }
      workspaces: {
        Row: {
          client_name: string | null
          created_at: string
          created_by: string
          id: string
          name: string
          org_id: string
          status: Database["public"]["Enums"]["workspace_status"]
        }
        Insert: {
          client_name?: string | null
          created_at?: string
          created_by: string
          id?: string
          name: string
          org_id: string
          status?: Database["public"]["Enums"]["workspace_status"]
        }
        Update: {
          client_name?: string | null
          created_at?: string
          created_by?: string
          id?: string
          name?: string
          org_id?: string
          status?: Database["public"]["Enums"]["workspace_status"]
        }
        Relationships: [
          {
            foreignKeyName: "workspaces_org_id_fkey"
            columns: ["org_id"]
            isOneToOne: false
            referencedRelation: "organizations"
            referencedColumns: ["id"]
          },
        ]
      }
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      agent_worker_heartbeat: {
        Args: {
          p_capabilities?: Database["public"]["Enums"]["agent_job_kind"][]
          p_hostname?: string
          p_metadata?: Json
          p_version?: string
          p_worker_id: string
        }
        Returns: {
          capabilities: Database["public"]["Enums"]["agent_job_kind"][]
          hostname: string | null
          id: string
          jobs_claimed: number
          last_seen_at: string
          metadata: Json
          started_at: string
          version: string | null
        }
        SetofOptions: {
          from: "*"
          to: "agent_workers"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      cancel_agent_job: {
        Args: { p_job_id: string }
        Returns: {
          attempts: number
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          dataset_id: string | null
          dataset_version_id: string | null
          error: string | null
          finished_at: string | null
          id: string
          kind: Database["public"]["Enums"]["agent_job_kind"]
          lease_expires_at: string | null
          max_attempts: number
          org_id: string
          payload: Json
          priority: number
          progress: Json
          raw_upload_id: string | null
          requested_by: string | null
          result: Json | null
          started_at: string | null
          status: Database["public"]["Enums"]["agent_job_status"]
          workspace_id: string
        }
        SetofOptions: {
          from: "*"
          to: "agent_jobs"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      claim_agent_job: {
        Args: {
          p_kinds?: Database["public"]["Enums"]["agent_job_kind"][]
          p_lease_seconds?: number
          p_worker_id: string
        }
        Returns: {
          attempts: number
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          dataset_id: string | null
          dataset_version_id: string | null
          error: string | null
          finished_at: string | null
          id: string
          kind: Database["public"]["Enums"]["agent_job_kind"]
          lease_expires_at: string | null
          max_attempts: number
          org_id: string
          payload: Json
          priority: number
          progress: Json
          raw_upload_id: string | null
          requested_by: string | null
          result: Json | null
          started_at: string | null
          status: Database["public"]["Enums"]["agent_job_status"]
          workspace_id: string
        }[]
        SetofOptions: {
          from: "*"
          to: "agent_jobs"
          isOneToOne: false
          isSetofReturn: true
        }
      }
      create_organization: {
        Args: { p_name: string; p_slug: string }
        Returns: {
          created_at: string
          created_by: string
          id: string
          name: string
          slug: string
        }
        SetofOptions: {
          from: "*"
          to: "organizations"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      create_workspace: {
        Args: { p_client_name?: string; p_name: string; p_org_id: string }
        Returns: {
          client_name: string | null
          created_at: string
          created_by: string
          id: string
          name: string
          org_id: string
          status: Database["public"]["Enums"]["workspace_status"]
        }
        SetofOptions: {
          from: "*"
          to: "workspaces"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      decide_proposed_changes: {
        Args: {
          p_approve: boolean
          p_dataset_version_id: string
          p_group_keys: string[]
          p_note?: string
        }
        Returns: number
      }
      enqueue_agent_job: {
        Args: {
          p_dataset_id?: string
          p_dataset_version_id?: string
          p_kind: Database["public"]["Enums"]["agent_job_kind"]
          p_payload?: Json
          p_priority?: number
          p_raw_upload_id?: string
          p_workspace_id: string
        }
        Returns: {
          attempts: number
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          dataset_id: string | null
          dataset_version_id: string | null
          error: string | null
          finished_at: string | null
          id: string
          kind: Database["public"]["Enums"]["agent_job_kind"]
          lease_expires_at: string | null
          max_attempts: number
          org_id: string
          payload: Json
          priority: number
          progress: Json
          raw_upload_id: string | null
          requested_by: string | null
          result: Json | null
          started_at: string | null
          status: Database["public"]["Enums"]["agent_job_status"]
          workspace_id: string
        }
        SetofOptions: {
          from: "*"
          to: "agent_jobs"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      enqueue_agent_job_internal: {
        Args: {
          p_dataset_id?: string
          p_dataset_version_id?: string
          p_kind: Database["public"]["Enums"]["agent_job_kind"]
          p_payload?: Json
          p_priority?: number
          p_raw_upload_id?: string
          p_requested_by?: string
          p_workspace_id: string
        }
        Returns: {
          attempts: number
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          dataset_id: string | null
          dataset_version_id: string | null
          error: string | null
          finished_at: string | null
          id: string
          kind: Database["public"]["Enums"]["agent_job_kind"]
          lease_expires_at: string | null
          max_attempts: number
          org_id: string
          payload: Json
          priority: number
          progress: Json
          raw_upload_id: string | null
          requested_by: string | null
          result: Json | null
          started_at: string | null
          status: Database["public"]["Enums"]["agent_job_status"]
          workspace_id: string
        }
        SetofOptions: {
          from: "*"
          to: "agent_jobs"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      finish_agent_job: {
        Args: {
          p_error?: string
          p_job_id: string
          p_result?: Json
          p_retryable?: boolean
          p_success: boolean
          p_worker_id: string
        }
        Returns: {
          attempts: number
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          dataset_id: string | null
          dataset_version_id: string | null
          error: string | null
          finished_at: string | null
          id: string
          kind: Database["public"]["Enums"]["agent_job_kind"]
          lease_expires_at: string | null
          max_attempts: number
          org_id: string
          payload: Json
          priority: number
          progress: Json
          raw_upload_id: string | null
          requested_by: string | null
          result: Json | null
          started_at: string | null
          status: Database["public"]["Enums"]["agent_job_status"]
          workspace_id: string
        }
        SetofOptions: {
          from: "*"
          to: "agent_jobs"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      has_workspace_access: {
        Args: { p_workspace_id: string }
        Returns: boolean
      }
      heartbeat_agent_job: {
        Args: {
          p_job_id: string
          p_lease_seconds?: number
          p_progress?: Json
          p_worker_id: string
        }
        Returns: boolean
      }
      is_org_member: { Args: { p_org_id: string }; Returns: boolean }
      mark_changes_applied: {
        Args: { p_dataset_version_id: string; p_group_keys: string[] }
        Returns: number
      }
      org_of_workspace: { Args: { p_workspace_id: string }; Returns: string }
      org_role_of: {
        Args: { p_org_id: string }
        Returns: Database["public"]["Enums"]["org_role"]
      }
      record_analysis_run: {
        Args: {
          p_created_by?: string
          p_dataset_version_id: string
          p_duration_ms?: number
          p_executed_sql: string
          p_job_id?: string
          p_model_used?: string
          p_question: string
          p_result: Json
          p_row_refs?: Json
        }
        Returns: {
          created_at: string
          created_by: string | null
          dataset_version_id: string
          duration_ms: number | null
          executed_sql: string
          id: string
          job_id: string | null
          model_used: string | null
          question: string | null
          result: Json
          row_refs: Json
          workspace_id: string
        }
        SetofOptions: {
          from: "*"
          to: "analysis_runs"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      record_dataset_profile: {
        Args: {
          p_column_count: number
          p_columns: Json
          p_dataset_version_id: string
          p_job_id?: string
          p_row_count: number
          p_signals: Json
        }
        Returns: {
          column_count: number
          columns: Json
          created_at: string
          dataset_version_id: string
          id: string
          produced_by_job_id: string | null
          row_count: number
          signals: Json
        }
        SetofOptions: {
          from: "*"
          to: "dataset_profiles"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      record_dataset_version: {
        Args: {
          p_column_hash?: string
          p_created_by?: string
          p_dataset_id: string
          p_kind: Database["public"]["Enums"]["dataset_version_kind"]
          p_metadata?: Json
          p_parent_version_id?: string
          p_parquet_path?: string
          p_produced_by_job?: string
          p_raw_upload_id?: string
          p_row_count?: number
        }
        Returns: {
          column_hash: string | null
          created_at: string
          created_by: string | null
          dataset_id: string
          id: string
          kind: Database["public"]["Enums"]["dataset_version_kind"]
          parent_version_id: string | null
          parquet_path: string | null
          produced_by_run_id: string | null
          raw_upload_id: string | null
          row_count: number | null
          version_no: number
        }
        SetofOptions: {
          from: "*"
          to: "dataset_versions"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      replace_proposed_changes: {
        Args: {
          p_dataset_version_id: string
          p_job_id: string
          p_proposals: Json
        }
        Returns: number
      }
      set_dataset_signature: {
        Args: { p_dataset_id: string; p_signature: string }
        Returns: {
          created_at: string
          created_by: string
          id: string
          name: string
          source_signature: string | null
          workspace_id: string
        }
        SetofOptions: {
          from: "*"
          to: "datasets"
          isOneToOne: true
          isSetofReturn: false
        }
      }
      try_uuid: { Args: { p_text: string }; Returns: string }
      workspace_of_dataset: { Args: { p_dataset_id: string }; Returns: string }
      write_audit: {
        Args: {
          p_action: string
          p_entity_id: string
          p_entity_type: string
          p_metadata?: Json
          p_org_id: string
          p_workspace_id: string
        }
        Returns: number
      }
    }
    Enums: {
      agent_job_kind:
        | "parse_workbook"
        | "profile_dataset"
        | "propose_cleaning"
        | "apply_cleaning"
        | "query_dataset"
        | "reconcile_sources"
        | "generate_report"
      agent_job_status:
        | "queued"
        | "running"
        | "succeeded"
        | "failed"
        | "cancelled"
      change_confidence: "high" | "medium" | "low"
      dataset_version_kind: "raw" | "cleaned"
      org_role: "owner" | "admin" | "member"
      proposed_change_status:
        | "pending"
        | "approved"
        | "rejected"
        | "applied"
        | "superseded"
      upload_status: "pending" | "stored" | "failed"
      workspace_status: "active" | "archived"
    }
    CompositeTypes: {
      [_ in never]: never
    }
  }
}

type DatabaseWithoutInternals = Omit<Database, "__InternalSupabase">

type DefaultSchema = DatabaseWithoutInternals[Extract<keyof Database, "public">]

export type Tables<
  DefaultSchemaTableNameOrOptions extends
    | keyof (DefaultSchema["Tables"] & DefaultSchema["Views"])
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
        DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
      DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])[TableName] extends {
      Row: infer R
    }
    ? R
    : never
  : DefaultSchemaTableNameOrOptions extends keyof (DefaultSchema["Tables"] &
        DefaultSchema["Views"])
    ? (DefaultSchema["Tables"] &
        DefaultSchema["Views"])[DefaultSchemaTableNameOrOptions] extends {
        Row: infer R
      }
      ? R
      : never
    : never

export type TablesInsert<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Insert: infer I
    }
    ? I
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Insert: infer I
      }
      ? I
      : never
    : never

export type TablesUpdate<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Update: infer U
    }
    ? U
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Update: infer U
      }
      ? U
      : never
    : never

export type Enums<
  DefaultSchemaEnumNameOrOptions extends
    | keyof DefaultSchema["Enums"]
    | { schema: keyof DatabaseWithoutInternals },
  EnumName extends DefaultSchemaEnumNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"]
    : never = never,
> = DefaultSchemaEnumNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"][EnumName]
  : DefaultSchemaEnumNameOrOptions extends keyof DefaultSchema["Enums"]
    ? DefaultSchema["Enums"][DefaultSchemaEnumNameOrOptions]
    : never

export type CompositeTypes<
  PublicCompositeTypeNameOrOptions extends
    | keyof DefaultSchema["CompositeTypes"]
    | { schema: keyof DatabaseWithoutInternals },
  CompositeTypeName extends PublicCompositeTypeNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"]
    : never = never,
> = PublicCompositeTypeNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"][CompositeTypeName]
  : PublicCompositeTypeNameOrOptions extends keyof DefaultSchema["CompositeTypes"]
    ? DefaultSchema["CompositeTypes"][PublicCompositeTypeNameOrOptions]
    : never

export const Constants = {
  graphql_public: {
    Enums: {},
  },
  public: {
    Enums: {
      agent_job_kind: [
        "parse_workbook",
        "profile_dataset",
        "propose_cleaning",
        "apply_cleaning",
        "query_dataset",
        "reconcile_sources",
        "generate_report",
      ],
      agent_job_status: [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
      ],
      change_confidence: ["high", "medium", "low"],
      dataset_version_kind: ["raw", "cleaned"],
      org_role: ["owner", "admin", "member"],
      proposed_change_status: [
        "pending",
        "approved",
        "rejected",
        "applied",
        "superseded",
      ],
      upload_status: ["pending", "stored", "failed"],
      workspace_status: ["active", "archived"],
    },
  },
} as const

