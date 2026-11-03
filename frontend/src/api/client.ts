import type {
  ArtifactContent,
  ArtifactRef,
  DiffResult,
  Page,
  ProjectInspection,
  ProjectProfile,
  ServiceSnapshot,
  TaskEvent,
  TaskSnapshot,
} from "../types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = payload?.error;
    throw new ApiError(
      typeof error?.message === "string" ? error.message : "请求未能完成。",
      typeof error?.code === "string" ? error.code : "HTTP_ERROR",
      response.status,
    );
  }
  return payload as T;
}

export const inspectProject = (workspace: string) =>
  request<ProjectInspection>("/api/projects/inspect", {
    method: "POST",
    body: JSON.stringify({ workspace }),
  });

export const getProjects = (offset = 0, limit = 100) =>
  request<Page<ProjectProfile>>(`/api/projects?offset=${offset}&limit=${limit}`);

export const saveProject = (input: {
  name: string;
  workspace: string;
  python_executable: string;
  pytest_args: string[];
  extra_checks: Array<Record<string, unknown>>;
}) => request<ProjectProfile>("/api/projects", { method: "POST", body: JSON.stringify(input) });

export const patchProject = (
  id: string,
  expected_revision: number,
  input: Record<string, unknown>,
) =>
  request<ProjectProfile>(`/api/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ expected_revision, ...input }),
  });

export const getTasks = (projectId?: string) => {
  const params = new URLSearchParams({ offset: "0", limit: "100" });
  if (projectId) params.set("project_id", projectId);
  return request<Page<TaskSnapshot>>(`/api/tasks?${params}`);
};

export const createTask = (project_id: string, requirement: string) =>
  request<TaskSnapshot>("/api/tasks", {
    method: "POST",
    body: JSON.stringify({ project_id, requirement }),
  });

export const getTask = (id: string) =>
  request<TaskSnapshot>(`/api/tasks/${encodeURIComponent(id)}`);

export const answerTask = (id: string, answer: string) =>
  request<TaskSnapshot>(`/api/tasks/${encodeURIComponent(id)}/answers`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });

export const dismissTask = (id: string) =>
  request<TaskSnapshot>(`/api/tasks/${encodeURIComponent(id)}/dismiss`, {
    method: "POST",
    body: "{}",
  });

export const subscribeTaskEvents = (
  id: string,
  after: number,
  onEvent: (event: TaskEvent) => void,
  onError?: () => void,
) => {
  const source = new EventSource(
    `/api/tasks/${encodeURIComponent(id)}/events?after=${after}`,
  );
  let lastSeq = after;
  const eventTypes = [
    "task_status_changed",
    "tool_started",
    "tool_completed",
    "tool_error",
    "command_output",
    "command_output_merged",
    "service_status_changed",
    "service_log_updated",
    "clarification_requested",
    "clarification_answered",
    "verification_completed",
    "verification_error",
    "extra_check_completed",
    "artifact_created",
    "git_snapshot",
    "file_changed",
    "implementation_changed",
    "commit_created",
    "commit_error",
    "model_error",
    "protocol_error",
  ];
  for (const type of eventTypes) {
    source.addEventListener(type, (raw) => {
      const event = raw as MessageEvent<string>;
      try {
        const parsed = JSON.parse(event.data) as TaskEvent;
        if (parsed.seq <= lastSeq) return;
        lastSeq = parsed.seq;
        onEvent(parsed);
        if (
          parsed.kind === "task_status_changed" &&
          ["succeeded", "failed", "no_changes", "dismissed", "interrupted"].includes(
            String(parsed.payload.status),
          )
        ) {
          source.close();
        }
      } catch {
        return;
      }
    });
  }
  source.onerror = () => onError?.();
  return () => source.close();
};

export const getTaskDiff = (id: string, scope: "task" | "workspace" = "task") =>
  request<DiffResult>(`/api/tasks/${encodeURIComponent(id)}/diff?scope=${scope}`);

export const getArtifacts = (id: string) =>
  request<Page<ArtifactRef>>(`/api/tasks/${encodeURIComponent(id)}/artifacts?limit=100`);

export const readArtifact = (id: string, offset = 0) =>
  request<ArtifactContent>(`/api/artifacts/${encodeURIComponent(id)}?offset=${offset}`);

export const getServices = () => request<Page<ServiceSnapshot>>("/api/services?limit=100");

export const stopService = (id: string) =>
  request<ServiceSnapshot>(`/api/services/${encodeURIComponent(id)}/stop`, {
    method: "POST",
    body: "{}",
  });
