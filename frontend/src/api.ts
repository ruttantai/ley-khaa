export type TaskSpec = {
  intent: string;
  inputs: string[];
  operation: string;
  output_format: string;
  recipient: string | null;
  urgency: string;
  missing_fields: string[];
  source_message_ids: string[];
  certainty: number;
};

export type Task = {
  id: string;
  project: string;
  state: string;
  title: string;
  spec: TaskSpec | null;
  recommended_mode: string | null;
  mode_override: string | null;
  effective_mode: string | null;
  confidence: number | null;
  risk: number | null;
  autonomy_reason: string | null;
  open_question: string | null;
  failure_reason: string | null;
  workspace_path: string | null;
  execution_verdict: Record<string, unknown> | null;
  // Set when this task's spec came from memory rather than the interpreter.
  // familiarity feeds the autonomy dial; remembered_from_task_id is what the
  // dashboard should let a human find and inspect.
  remembered_from_task_id: string | null;
  familiarity: number;
};

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export async function fetchTasks(): Promise<Task[]> {
  const res = await fetch(`${BASE}/tasks`);
  if (!res.ok) throw new Error(`fetchTasks failed: ${res.status}`);
  return res.json();
}

async function send(path: string, method: string, body?: unknown): Promise<Task> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${method} ${path} failed: ${res.status}`);
  return res.json();
}

export const approveTask = (id: string) => send(`/tasks/${id}/approve`, "POST");
export const rejectTask = (id: string, reason: string) =>
  send(`/tasks/${id}/reject`, "POST", { reason });
export const setTaskMode = (id: string, mode: string | null) =>
  send(`/tasks/${id}/mode`, "POST", { mode });
export const answerTask = (id: string, text: string) =>
  send(`/tasks/${id}/answer`, "POST", { text });
export const patchTaskSpec = (id: string, patch: Record<string, unknown>) =>
  send(`/tasks/${id}/spec`, "PATCH", { patch });

export type Workflow = {
  name: string;
  description: string;
  operation_aliases: string[];
  output_format: string;
  inputs: { role: string; suffixes: string[] }[];
  origin: string;
  promoted_from_task_id: string | null;
  runs_ok: number;
  runs_failed: number;
  quarantined: boolean;
  source_sha256: string;
};

// Deliberately not routed through send(): send() discards the response body
// on failure, but a promotion's 409 detail (a taken name, a run that did not
// pass) is the actionable part — the human needs to read it, not just learn
// that the request failed.
export async function promoteTask(id: string, name: string, description: string): Promise<Workflow> {
  const res = await fetch(`${BASE}/tasks/${id}/promote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `promoteTask failed: ${res.status}`);
  }
  return res.json();
}

export type Bundle = {
  task_id: string;
  root: string;
  manifest: Record<string, any>;
  files: string[];
  deliverables: string[];
};

export async function fetchBundle(id: string): Promise<Bundle> {
  const res = await fetch(`${BASE}/tasks/${id}/bundle`);
  if (!res.ok) throw new Error(`fetchBundle failed: ${res.status}`);
  return res.json();
}

export async function fetchBundleFile(id: string, path: string): Promise<string> {
  const res = await fetch(`${BASE}/tasks/${id}/bundle/file?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(`fetchBundleFile failed: ${res.status}`);
  return (await res.json()).content;
}

// Plain hrefs, not fetches: the browser's own download handling is what should
// deal with a binary body, and an anchor keeps that out of our hands.
export const deliverableUrl = (id: string) => `${BASE}/tasks/${id}/bundle/deliverable`;
export const bundleDownloadUrl = (id: string) => `${BASE}/tasks/${id}/bundle/download`;

export type Candidate = {
  id: string;
  conversation_id: string;
  candidate_key: string;
  title: string;
  summary: string;
  state: string;
  message_ids: string[];
  missing_fields: string[];
  open_question: string | null;
  task_id: string | null;
};

export async function fetchCandidates(): Promise<Candidate[]> {
  const res = await fetch(`${BASE}/candidates`);
  if (!res.ok) throw new Error(`fetchCandidates failed: ${res.status}`);
  return res.json();
}

export async function fetchWorkflows(): Promise<Workflow[]> {
  const res = await fetch(`${BASE}/registry`);
  if (!res.ok) throw new Error(`fetchWorkflows failed: ${res.status}`);
  return res.json();
}

export async function unquarantineWorkflow(name: string): Promise<Workflow> {
  const res = await fetch(`${BASE}/registry/${encodeURIComponent(name)}/unquarantine`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`unquarantineWorkflow failed: ${res.status}`);
  return res.json();
}

export async function deleteWorkflow(name: string): Promise<void> {
  const res = await fetch(`${BASE}/registry/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`deleteWorkflow failed: ${res.status}`);
}
