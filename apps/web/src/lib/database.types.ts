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
          id: string
          jobs_claimed: number
          last_seen_at: string
          started_at: string
          version: string | null
        }
        Insert: {
          id: string
          jobs_claimed?: number
          last_seen_at?: string
          started_at?: string
          version?: string | null
        }
        Update: {
          id?: string
          jobs_claimed?: number
          last_seen_at?: string
          started_at?: string
          version?: string | null
        }
        Relationships: []
      }
      analysis_runs: {
        Row: {
          created_at: string
          created_by: string | null
          dataset_version_id: string | null
          id: string
          question: string | null
          result_json: Json
          source_row_ids: Json
          sql_executed: string | null
          workspace_id: string
        }
        Insert: {
          created_at?: string
          created_by?: string | null
          dataset_version_id?: string | null
          id?: string
          question?: string | null
          result_json?: Json
          source_row_ids?: Json
          sql_executed?: string | null
          workspace_id: string
        }
        Update: {
          created_at?: string
          created_by?: string | null
          dataset_version_id?: string | null
          id?: string
          question?: string | null
          result_json?: Json
          source_row_ids?: Json
          sql_executed?: string | null
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
      cleaning_recipes: {
        Row: {
          created_at: string
          created_by: string | null
          dataset_id: string | null
          id: string
          name: string
          source_signature: string | null
          template_origin_id: string | null
          workspace_id: string
        }
        Insert: {
          created_at?: string
          created_by?: string | null
          dataset_id?: string | null
          id?: string
          name: string
          source_signature?: string | null
          template_origin_id?: string | null
          workspace_id: string
        }
        Update: {
          created_at?: string
          created_by?: string | null
          dataset_id?: string | null
          id?: string
          name?: string
          source_signature?: string | null
          template_origin_id?: string | null
          workspace_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "cleaning_recipes_dataset_id_fkey"
            columns: ["dataset_id"]
            isOneToOne: false
            referencedRelation: "datasets"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "cleaning_recipes_template_origin_id_fkey"
            columns: ["template_origin_id"]
            isOneToOne: false
            referencedRelation: "cleaning_recipes"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "cleaning_recipes_workspace_id_fkey"
            columns: ["workspace_id"]
            isOneToOne: false
            referencedRelation: "workspaces"
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
      deviations: {
        Row: {
          affected_rows: number
          created_at: string
          evidence_json: Json
          id: string
          materiality_gbp: number
          resolution: Database["public"]["Enums"]["deviation_resolution"]
          resolved_at: string | null
          resolved_by: string | null
          run_id: string
          severity: Database["public"]["Enums"]["confidence_tier"]
          type: string
        }
        Insert: {
          affected_rows?: number
          created_at?: string
          evidence_json?: Json
          id?: string
          materiality_gbp?: number
          resolution?: Database["public"]["Enums"]["deviation_resolution"]
          resolved_at?: string | null
          resolved_by?: string | null
          run_id: string
          severity?: Database["public"]["Enums"]["confidence_tier"]
          type: string
        }
        Update: {
          affected_rows?: number
          created_at?: string
          evidence_json?: Json
          id?: string
          materiality_gbp?: number
          resolution?: Database["public"]["Enums"]["deviation_resolution"]
          resolved_at?: string | null
          resolved_by?: string | null
          run_id?: string
          severity?: Database["public"]["Enums"]["confidence_tier"]
          type?: string
        }
        Relationships: [
          {
            foreignKeyName: "deviations_run_id_fkey"
            columns: ["run_id"]
            isOneToOne: false
            referencedRelation: "recipe_runs"
            referencedColumns: ["id"]
          },
        ]
      }
      mapping_entries: {
        Row: {
          confirmed_by: string | null
          created_at: string
          hit_count: number
          id: string
          kind: string
          learned_from_run_id: string | null
          lookup_key: string
          mapped_value: string
          metadata: Json
          updated_at: string
          workspace_id: string
        }
        Insert: {
          confirmed_by?: string | null
          created_at?: string
          hit_count?: number
          id?: string
          kind: string
          learned_from_run_id?: string | null
          lookup_key: string
          mapped_value: string
          metadata?: Json
          updated_at?: string
          workspace_id: string
        }
        Update: {
          confirmed_by?: string | null
          created_at?: string
          hit_count?: number
          id?: string
          kind?: string
          learned_from_run_id?: string | null
          lookup_key?: string
          mapped_value?: string
          metadata?: Json
          updated_at?: string
          workspace_id?: string
        }
        Relationships: [
          {
            foreignKeyName: "mapping_entries_learned_from_run_id_fkey"
            columns: ["learned_from_run_id"]
            isOneToOne: false
            referencedRelation: "recipe_runs"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "mapping_entries_workspace_id_fkey"
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
      recipe_runs: {
        Row: {
          auto_corrections: number
          automation_rate: number | null
          dataset_version_in: string
          dataset_version_out: string | null
          deviations_count: number
          finished_at: string | null
          id: string
          invariant_status: Database["public"]["Enums"]["invariant_status"]
          recipe_version_id: string
          rows_matched: number | null
          rows_processed: number | null
          started_at: string
          started_by: string | null
          status: Database["public"]["Enums"]["recipe_run_status"]
        }
        Insert: {
          auto_corrections?: number
          automation_rate?: number | null
          dataset_version_in: string
          dataset_version_out?: string | null
          deviations_count?: number
          finished_at?: string | null
          id?: string
          invariant_status?: Database["public"]["Enums"]["invariant_status"]
          recipe_version_id: string
          rows_matched?: number | null
          rows_processed?: number | null
          started_at?: string
          started_by?: string | null
          status?: Database["public"]["Enums"]["recipe_run_status"]
        }
        Update: {
          auto_corrections?: number
          automation_rate?: number | null
          dataset_version_in?: string
          dataset_version_out?: string | null
          deviations_count?: number
          finished_at?: string | null
          id?: string
          invariant_status?: Database["public"]["Enums"]["invariant_status"]
          recipe_version_id?: string
          rows_matched?: number | null
          rows_processed?: number | null
          started_at?: string
          started_by?: string | null
          status?: Database["public"]["Enums"]["recipe_run_status"]
        }
        Relationships: [
          {
            foreignKeyName: "recipe_runs_dataset_version_in_fkey"
            columns: ["dataset_version_in"]
            isOneToOne: false
            referencedRelation: "dataset_versions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "recipe_runs_dataset_version_out_fkey"
            columns: ["dataset_version_out"]
            isOneToOne: false
            referencedRelation: "dataset_versions"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "recipe_runs_recipe_version_id_fkey"
            columns: ["recipe_version_id"]
            isOneToOne: false
            referencedRelation: "recipe_versions"
            referencedColumns: ["id"]
          },
        ]
      }
      recipe_versions: {
        Row: {
          change_note: string | null
          created_at: string
          created_by: string | null
          id: string
          invariants_json: Json
          recipe_id: string
          steps_json: Json
          version_no: number
        }
        Insert: {
          change_note?: string | null
          created_at?: string
          created_by?: string | null
          id?: string
          invariants_json?: Json
          recipe_id: string
          steps_json?: Json
          version_no: number
        }
        Update: {
          change_note?: string | null
          created_at?: string
          created_by?: string | null
          id?: string
          invariants_json?: Json
          recipe_id?: string
          steps_json?: Json
          version_no?: number
        }
        Relationships: [
          {
            foreignKeyName: "recipe_versions_recipe_id_fkey"
            columns: ["recipe_id"]
            isOneToOne: false
            referencedRelation: "cleaning_recipes"
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
        Args: { p_version?: string; p_worker_id: string }
        Returns: {
          id: string
          jobs_claimed: number
          last_seen_at: string
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
        Args: { p_lease_seconds?: number; p_worker_id: string }
        Returns: {
          attempts: number
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          dataset_id: string | null
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
      enqueue_agent_job: {
        Args: {
          p_dataset_id?: string
          p_kind: Database["public"]["Enums"]["agent_job_kind"]
          p_payload?: Json
          p_raw_upload_id?: string
          p_workspace_id: string
        }
        Returns: {
          attempts: number
          claimed_at: string | null
          claimed_by: string | null
          created_at: string
          dataset_id: string | null
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
      org_of_workspace: { Args: { p_workspace_id: string }; Returns: string }
      org_role_of: {
        Args: { p_org_id: string }
        Returns: Database["public"]["Enums"]["org_role"]
      }
      reap_expired_agent_jobs: { Args: never; Returns: number }
      try_uuid: { Args: { p_text: string }; Returns: string }
      workspace_of_dataset: { Args: { p_dataset_id: string }; Returns: string }
      workspace_of_recipe: { Args: { p_recipe_id: string }; Returns: string }
      workspace_of_recipe_version: {
        Args: { p_version_id: string }
        Returns: string
      }
      workspace_of_run: { Args: { p_run_id: string }; Returns: string }
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
      agent_job_kind: "analyze_workbook" | "chat_turn"
      agent_job_status:
        | "queued"
        | "running"
        | "succeeded"
        | "failed"
        | "cancelled"
      confidence_tier: "auto" | "review" | "block"
      dataset_version_kind: "raw" | "cleaned"
      deviation_resolution: "unresolved" | "accepted" | "rejected" | "corrected"
      invariant_status: "pending" | "passed" | "failed"
      org_role: "owner" | "admin" | "member"
      recipe_run_status: "running" | "succeeded" | "blocked" | "failed"
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
      agent_job_kind: ["analyze_workbook", "chat_turn"],
      agent_job_status: [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
      ],
      confidence_tier: ["auto", "review", "block"],
      dataset_version_kind: ["raw", "cleaned"],
      deviation_resolution: ["unresolved", "accepted", "rejected", "corrected"],
      invariant_status: ["pending", "passed", "failed"],
      org_role: ["owner", "admin", "member"],
      recipe_run_status: ["running", "succeeded", "blocked", "failed"],
      upload_status: ["pending", "stored", "failed"],
      workspace_status: ["active", "archived"],
    },
  },
} as const

