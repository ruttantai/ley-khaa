import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Projects from "./Projects";
import * as api from "./api";

beforeEach(() => vi.restoreAllMocks());

// Projects polls via setInterval — unmounting after each test clears that
// timer so it does not leak into later tests in the run.
afterEach(cleanup);

test("it shows each project's queue depth", async () => {
  vi.spyOn(api, "fetchProjects").mockResolvedValue([
    { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 2, in_flight: null },
  ]);
  render(<Projects />);
  expect(await screen.findByText("Acme")).toBeTruthy();
  expect(screen.getByText(/2 queued/)).toBeTruthy();
});

test("it names the task currently in flight", async () => {
  vi.spyOn(api, "fetchProjects").mockResolvedValue([
    { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 0, in_flight: "task-7" },
  ]);
  render(<Projects />);
  await waitFor(() => expect(screen.getByText(/running/i)).toBeTruthy());
});

test("it says so when there is nothing queued", async () => {
  vi.spyOn(api, "fetchProjects").mockResolvedValue([
    { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 0, in_flight: null },
  ]);
  render(<Projects />);
  expect(await screen.findByText(/idle/i)).toBeTruthy();
});
