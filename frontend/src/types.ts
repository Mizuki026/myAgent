export type ExecutionLimits = {
  max_model_requests: number;
  command_timeout_seconds: number;
  output_limit_bytes: number;
};

export type ProjectProfile = {
  id: string;
  name: string;
  workspace: string;
  python_executable: string;
  pytest_args: string[];
  extra_checks: Array<Record<string, unknown>>;
  limits: ExecutionLimits;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type ProjectInspection = {
  workspace: string;
  is_git_repository: boolean;
  head: string | null;
  candidate_interpreters: string[];
  selected_interpreter: string | null;
  pytest_available: boolean;
  suggested_pytest_args: string[];
  ready: boolean;
  issues: Array<{ code: string; message: string; retryable: boolean }>;
};

export type TaskStatus =
  | "running"
  | "needs_clarification"
  | "succeeded"
  | "failed"
  | "no_changes"
  | "dismissed"
  | "interrupted";

export type TaskSnapshot = {
  id: string;
  project_id: string;
  requirement: string;
  status: TaskStatus;
  phase: string;
  attempt: number;
  model_requests: number;
  summary: string;
  questions: string[];
  commit: string | null;
  last_event_seq: number;
  created_at: string;
  updated_at: string;
  error: { code: string; message: string; retryable: boolean } | null;
};

export type TaskEvent = {
  task_id: string;
  seq: number;
  kind: string;
  timestamp: string;
  payload: Record<string, unknown>;
};

export type ArtifactRef = {
  id: string;
  task_id: string;
  service_id: string | null;
  kind: string;
  media_type: string;
  size_bytes: number;
  mutable: boolean;
  sha256: string | null;
  created_at: string;
};

export type ArtifactContent = {
  artifact: ArtifactRef;
  offset: number;
  next_offset: number;
  size_bytes: number;
  text: string;
  eof: boolean;
  truncated: boolean;
};

export type DiffResult = {
  scope: "task" | "workspace";
  text: string;
  files: Array<{ path: string; source: string | null; change: string }>;
  truncated: boolean;
  artifact: ArtifactRef | null;
};

export type ServiceSnapshot = {
  id: string;
  task_id: string;
  project_id: string;
  name: string;
  status: "starting" | "running" | "stopping" | "stopped" | "exited" | "failed" | "unknown";
  command_summary: string;
  url: string | null;
  stdout_artifact: Record<string, unknown> | null;
  stderr_artifact: Record<string, unknown> | null;
  created_at: string;
  ended_at: string | null;
  exit_code: number | null;
};

export type Page<T> = { items: T[]; total: number; offset: number; limit: number };
