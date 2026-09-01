import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import DeadLetters from "./DeadLetters";
import type { DeadLetter } from "./api";

const letter = (overrides: Partial<DeadLetter> = {}): DeadLetter => ({
  id: "dl1",
  source: "slack",
  kind: "inbound",
  reason: "unparsable Slack ts 'nope'",
  payload: '{"event": {"text": "hi"}}',
  created_at: "2026-08-31T09:00:00+00:00",
  ...overrides,
});

// Drives the real api layer through fetch rather than mocking api.ts, the same
// shape Triage.test.tsx uses: only this can prove fetchDeadLetters actually
// reaches the right URL.
function stub(rows: DeadLetter[], ok = true) {
  // Params declared so `mock.calls[0][0]` type-checks: a bare `vi.fn(async () => …)`
  // infers an EMPTY tuple for its arguments, and indexing that is a TS2493.
  const mock = vi.fn(async (..._args: unknown[]) => ({
    ok,
    status: ok ? 200 : 500,
    json: async () => rows,
  }));
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(cleanup);

test("renders nothing at all when there are no dead letters", async () => {
  stub([]);
  const { container } = render(<DeadLetters />);
  // An empty panel with a heading is a permanent scar on a dashboard that is
  // usually healthy. Nothing wrong, nothing shown.
  await waitFor(() => expect(container.textContent).toBe(""));
});

test("shows the source, the kind and the reason", async () => {
  stub([letter()]);
  render(<DeadLetters />);

  expect(await screen.findByText(/unparsable Slack ts/)).toBeTruthy();
  expect(screen.getByText(/slack/)).toBeTruthy();
  expect(screen.getByText(/inbound/)).toBeTruthy();
});

test("fetches from /dead-letters", async () => {
  const mock = stub([letter()]);
  render(<DeadLetters />);

  await waitFor(() => expect(mock).toHaveBeenCalled());
  expect(String(mock.mock.calls[0][0])).toContain("/dead-letters");
});

test("shows a failure to load rather than an empty panel", async () => {
  stub([], false);
  render(<DeadLetters />);

  expect(await screen.findByText(/fetchDeadLetters failed/)).toBeTruthy();
});

test("lists every dead letter it is given", async () => {
  stub([letter(), letter({ id: "dl2", source: "discord", reason: "delivery failed: 403" })]);
  render(<DeadLetters />);

  expect(await screen.findByText(/unparsable Slack ts/)).toBeTruthy();
  expect(screen.getByText(/delivery failed: 403/)).toBeTruthy();
});
