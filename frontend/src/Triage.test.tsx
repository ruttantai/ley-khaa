import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Triage from "./Triage";
import type { TriageItem } from "./api";

const ITEM: TriageItem = {
  candidate_id: "c1",
  title: "also flag duplicates",
  summary: "s",
  amends_task_id: "t1",
  amends_task_title: "universe check",
  reason: "adds a check to the running task",
  confidence: 0.92,
};

beforeEach(() => vi.restoreAllMocks());
afterEach(cleanup);

const okJson = (body: unknown, status = 200) => ({ ok: true, status, json: async () => body });

// Drives the real API layer (mutateCandidate, fetchTriage) through fetch
// itself, rather than mocking api.ts wholesale — see Task 14's ruling: only
// this shape can prove a 409 detail actually reaches the screen, since a
// mock of api.foldCandidate would swallow whatever mutateCandidate does.
// Faked closely enough that a click's effect (item removed after a
// successful fold/separate) is visible in the next GET /triage.
function stubTriage(initial: TriageItem[], failFold?: { status: number; detail: string }) {
  let current = [...initial];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method ?? "GET";
    if (method === "POST" && u.includes("/fold")) {
      if (failFold) return { ok: false, status: failFold.status, json: async () => ({ detail: failFold.detail }) };
      const id = u.split("/candidates/")[1].split("/fold")[0];
      current = current.filter((i) => i.candidate_id !== id);
      return okJson({ id: "task-x" });
    }
    if (method === "POST" && u.includes("/separate")) {
      const id = u.split("/candidates/")[1].split("/separate")[0];
      current = current.filter((i) => i.candidate_id !== id);
      return okJson({ id: "task-x" });
    }
    return okJson(current);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const fetchCalls = () => (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls;
const triageGetCalls = () =>
  fetchCalls().filter(([url, init]) => String(url).includes("/triage") && (!init?.method || init.method === "GET"));

test("it shows the proposal and why it was made", async () => {
  stubTriage([ITEM]);
  render(<Triage />);
  expect(await screen.findByText(/also flag duplicates/)).toBeTruthy();
  expect(screen.getByText(/universe check/)).toBeTruthy();
  expect(screen.getByText(/adds a check to the running task/)).toBeTruthy();
});

test("folding calls the API and refreshes", async () => {
  stubTriage([ITEM]);
  render(<Triage />);
  fireEvent.click(await screen.findByRole("button", { name: /fold in/i }));
  await waitFor(() =>
    expect(fetchCalls().some(([url, init]) => String(url).includes("/candidates/c1/fold") && init?.method === "POST")).toBe(true),
  );
  // Two GET /triage calls: the initial one and the refresh after the
  // mutation. Without the refresh the tray keeps showing a decision that
  // has already been made.
  await waitFor(() => expect(triageGetCalls().length).toBe(2));
});

test("separating calls the API", async () => {
  stubTriage([ITEM]);
  render(<Triage />);
  fireEvent.click(await screen.findByRole("button", { name: /keep separate/i }));
  await waitFor(() =>
    expect(
      fetchCalls().some(([url, init]) => String(url).includes("/candidates/c1/separate") && init?.method === "POST"),
    ).toBe(true),
  );
});

test("a failed fold shows the reason instead of silently doing nothing", async () => {
  stubTriage([ITEM], { status: 409, detail: "the task has moved on" });
  render(<Triage />);
  fireEvent.click(await screen.findByRole("button", { name: /fold in/i }));
  expect(await screen.findByText(/moved on/)).toBeTruthy();
});
