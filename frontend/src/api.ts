export type Task = {
  id: string;
  project: string;
  state: string;
  title: string;
};

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export async function fetchTasks(): Promise<Task[]> {
  const res = await fetch(`${BASE}/tasks`);
  if (!res.ok) throw new Error(`fetchTasks failed: ${res.status}`);
  return res.json();
}

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
