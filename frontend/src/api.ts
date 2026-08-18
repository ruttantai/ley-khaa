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
