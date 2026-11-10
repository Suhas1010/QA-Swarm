export interface PersonaInfo {
  name: string;
  goal: string;
  viewport: string;
  has_touch: boolean;
}

export interface RunSummary {
  steps_used: number;
  detected_total: number;
  detecting_queue: number;
  verifying_count: number;
  verified_count: number;
  rejected_count: number;
  reported_count: number;
  total_cost_usd: number;
  time_to_first_verified_sec: number | null;
}

export interface RunRow {
  id: string;
  target_url: string;
  mode: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  first_verified_at: string | null;
  total_cost_usd: number;
  summary?: RunSummary;
}

export interface WorkerTile {
  id: string;
  persona: string;
  goal?: string;
  viewport: string;
  status: string;
  steps_used: number;
  max_steps: number;
  current_url: string;
  last_action: string;
  thought?: string;
  latest_image_url?: string | null;
  frame_b64?: string | null;
}

export interface TraceStepItem {
  step_index: number;
  action_type: string;
  locator?: Record<string, unknown>;
  input_value?: string | null;
  url: string;
  screenshot_path?: string;
}

export interface SuspectItem {
  id: string;
  worker_id: string;
  persona: string;
  category: string;
  error_signature: string;
  title_hint: string;
  url: string;
  trigger_step_index: number;
  status: "Detected" | "Verifying" | "Verified" | "Rejected";
  repro_count: number;
  rejection_reason: string | null;
  trace_slice?: TraceStepItem[];
  console_logs?: Array<Record<string, unknown>>;
  network_logs?: Array<Record<string, unknown>>;
  evidence?: Record<string, unknown>;
}

export interface BugReportItem {
  id: string;
  run_id: string;
  primary_suspect_id: string;
  title: string;
  severity: "Critical" | "High" | "Medium" | "Low";
  category: string;
  body_markdown: string;
  github_issue_url: string | null;
  github_issue_number: number | null;
  markdown_export_path: string | null;
  video_path: string | null;
  video_url?: string | null;
}

export interface BugDetailResponse {
  bug: BugReportItem;
  suspect: SuspectItem | null;
  verification_attempts: Array<{
    id: string;
    attempt_num: number;
    reproduced: number;
    matched_signature: string | null;
    notes: string;
    duration_ms: number;
  }>;
}

export interface BenchmarkResults {
  available?: boolean;
  generated_at?: string;
  total_seeded_bugs?: number;
  single_agent?: {
    steps_used: number;
    matched_bugs: number;
    recall_pct: number;
    cost_usd: number;
  };
  swarm_no_verifier?: {
    total_filed: number;
    true_positives: number;
    false_positives: number;
    false_positive_rate_pct: number;
    recall_pct: number;
  };
  swarm_with_verifier?: {
    steps_used: number;
    total_filed: number;
    true_positives: number;
    false_positives: number;
    rejected_count: number;
    false_positive_rate_pct: number;
    recall_pct: number;
    cost_usd: number;
    time_to_first_verified_sec: number | null;
    matched_bug_ids: string[];
  };
}

