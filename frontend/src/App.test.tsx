import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";

const candidate = (state: string, title: string) => ({
  id: `c-${state}`,
  conversation_id: "conv-1",
  candidate_key: `k-${state}`,
  title,
  summary: title,
  state,
  message_ids: ["m1"],
  missing_fields: [],
  open_question: null,
  task_id: null,
});

function stubApi(candidates: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => ({
      ok: true,
      json: async () =>
        String(url).includes("/candidates")
          ? candidates
          : [{ id: "t1", project: "default", state: "done", title: "compare universes" }],
    })),
  );
}

beforeEach(() => stubApi([]));
afterEach(cleanup);

test("renders tasks from the API", async () => {
  render(<App />);
  await waitFor(() => expect(screen.getByText("compare universes")).toBeTruthy());
  expect(screen.getByText(/done/)).toBeTruthy();
});

test("Forming lists only candidates that are still forming", async () => {
  stubApi([
    candidate("crystallizing", "Universe reconciliation"),
    candidate("promoted", "Already a task"),
    candidate("abandoned", "Dropped request"),
  ]);
  render(<App />);
  await waitFor(() => expect(screen.getByText("Universe reconciliation")).toBeTruthy());
  expect(screen.queryByText("Already a task")).toBeNull();
  expect(screen.queryByText("Dropped request")).toBeNull();
});
