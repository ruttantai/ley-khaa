import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import Candidates from "./Candidates";

const items = [
  {
    id: "c1",
    conversation_id: "conv-universe",
    candidate_key: "k1",
    title: "Universe reconciliation",
    summary: "Compare Bloomberg vs FactSet",
    state: "crystallizing",
    message_ids: ["m1", "m2"],
    missing_fields: ["output_format"],
    open_question: "Excel or CSV?",
    task_id: null,
  },
];

afterEach(cleanup);

test("renders a candidate with its state and owned message count", () => {
  render(<Candidates items={items} />);
  expect(screen.getByText("Universe reconciliation")).toBeTruthy();
  expect(screen.getByText(/crystallizing/)).toBeTruthy();
  expect(screen.getByText(/2 messages/)).toBeTruthy();
});

test("shows the open question when the candidate is blocked", () => {
  render(<Candidates items={items} />);
  expect(screen.getByText(/Excel or CSV\?/)).toBeTruthy();
});

test("renders an empty state when nothing is forming", () => {
  render(<Candidates items={[]} />);
  expect(screen.getByText(/No candidates forming/)).toBeTruthy();
});
