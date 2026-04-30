// Shape mirrors crd_sidecar/ipc/handlers.py::remediate_imscc return value.

export type RemediationOptions = {
  include_rendered_scan: boolean;
  include_quiz_pages: boolean;
  sanitize_after_transform: boolean;
  include_document_conversion: boolean;
};

export type PageReport = {
  identifier: string;
  title: string;
  content_type: string;
  file_path: string;
  issues_before: number;
  issues_after: number;
  body_issue_rule_ids: string[];
  rendered_issue_rule_ids: string[];
  skipped_reason: string | null;
};

export type FixSuggestion = {
  id: string;
  issue_id: string;
  page_id: string;
  page_title: string | null;
  page_file_path: string | null;
  rule_id: string;
  category: string;
  original_html: string;
  proposed_html: string;
  original_text: string;
  proposed_text: string;
  rationale: string;
  confidence: number;
};

export type RemediationSummary = {
  job_id: string;
  input_path: string;
  output_path: string;
  course_title: string;
  page_count: number;
  pages_modified: number;
  issues_before: number;
  issues_after: number;
  documents_converted: number;
  documents_skipped: number;
  pages: PageReport[];
  suggestions: FixSuggestion[];
};

// Every field that shows up on a `job.progress` notification is optional —
// the sidecar emits different phases with different shapes.
export type JobProgress = {
  job_id: string;
  phase: string;
  index?: number;
  total?: number;
  identifier?: string;
  title?: string;
  content_type?: string;
  issues_before?: number;
  issues_after?: number;
  course_title?: string;
  page_count?: number;
  input_path?: string;
  converted?: number;
  skipped?: number;
  error?: string;
};

export type OllamaStatus = {
  reachable: boolean;
  base_url: string;
  installed_models: string[];
  error: string | null;
};

// Returned by the `bundled_ollama_status` Tauri command. Describes the state
// of the Ollama instance the app owns — bundled in release, or a fallback
// pointer to the user's system Ollama in dev / missing-resource builds.
export type BundledOllamaStatus = {
  running: boolean;
  bundled: boolean;
  port: number | null;
  base_url: string | null;
  default_model: string;
  default_model_present: boolean;
  installed_models: string[];
  error: string | null;
};

// Mirrors `PullProgressEvent` from src-tauri/src/ollama.rs.
export type PullProgressEvent = {
  model: string;
  status: string | null;
  digest: string | null;
  total: number | null;
  completed: number | null;
  percent: number | null;
  done: boolean;
  error: string | null;
};

// Returned by the `export_acr` Tauri command. Mirrors the dict returned
// by `crd_sidecar.ipc.handlers.export_acr`.
export type AcrExportResult = {
  output_path: string;
  format: "html" | "markdown" | "json";
  conformance_percentage: number;
  overall_status: string;
  total_issues: number;
  pages_analyzed: number;
  criterion_counts: {
    supports: number;
    partially_supports: number;
    does_not_support: number;
    not_applicable: number;
  };
  course_name: string;
};

export const DEFAULT_OPTIONS: RemediationOptions = {
  include_rendered_scan: false,
  include_quiz_pages: false,
  sanitize_after_transform: true,
  include_document_conversion: false,
};
