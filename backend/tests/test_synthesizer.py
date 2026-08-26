from ley_khaa.executor.resolver import ResolvedInput
from ley_khaa.executor.sandbox import SandboxResult
from ley_khaa.executor.synthesizer import SynthesizedScript, Synthesizer
from ley_khaa.executor.validator import Verdict
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.router import OPUS


def _spec() -> TaskSpec:
    return TaskSpec(
        intent="find securities in Bloomberg that are missing from FactSet",
        inputs=["Bloomberg universe", "FactSet"],
        operation="set_difference",
        output_format="xlsx",
        certainty=0.9,
    )


def _resolved() -> list[ResolvedInput]:
    return [
        ResolvedInput("Bloomberg universe", "bloomberg_universe.csv", "ticker\nAAA\nBBB\n", "catalog"),
        ResolvedInput("FactSet", "factset_universe.csv", "ticker\nAAA\n", "catalog"),
    ]


def _script(source="print('ok')") -> SynthesizedScript:
    return SynthesizedScript(reasoning="because", source=source)


def test_synthesis_routes_to_opus_with_room_to_write_a_program():
    llm = FakeLLM(responses=[_script()])
    Synthesizer(llm).synthesize(_spec(), _resolved())
    choice = llm.calls[0].choice
    assert choice.model == OPUS
    assert choice.supports_thinking is True
    assert choice.max_tokens == 16000


def test_the_prompt_names_the_files_the_script_will_actually_find():
    llm = FakeLLM(responses=[_script()])
    Synthesizer(llm).synthesize(_spec(), _resolved())
    prompt = llm.calls[0].user
    assert "inputs/bloomberg_universe.csv" in prompt
    assert "inputs/factset_universe.csv" in prompt
    assert "deliverable/output.xlsx" in prompt


def test_the_prompt_shows_a_preview_of_each_input():
    """The model needs the real column names, or it invents plausible ones."""
    llm = FakeLLM(responses=[_script()])
    Synthesizer(llm).synthesize(_spec(), _resolved())
    assert "ticker" in llm.calls[0].user


def test_the_system_prompt_states_the_sandbox_rules():
    llm = FakeLLM(responses=[_script()])
    Synthesizer(llm).synthesize(_spec(), _resolved())
    system = llm.calls[0].system
    assert "no network" in system.lower()
    assert "openpyxl" in system


def test_repair_is_given_the_previous_source_and_the_failure():
    llm = FakeLLM(responses=[_script("fixed")])
    result = SandboxResult(
        exit_code=1, stdout="", stderr="KeyError: 'ticker_id'", duration_ms=12, timed_out=False
    )
    verdict = Verdict(ok=False, reason="The generated script failed while running.", checks={})
    out = Synthesizer(llm).repair(
        _spec(), _resolved(), previous="broken source", result=result, verdict=verdict
    )
    prompt = llm.calls[0].user
    assert "broken source" in prompt
    assert "KeyError: 'ticker_id'" in prompt
    assert out.source == "fixed"


def test_repair_truncates_a_giant_traceback():
    """A runaway script can emit megabytes of stderr; that must not become the
    prompt."""
    llm = FakeLLM(responses=[_script()])
    result = SandboxResult(
        exit_code=1, stdout="", stderr="x" * 50_000, duration_ms=1, timed_out=False
    )
    verdict = Verdict(ok=False, reason="failed", checks={})
    Synthesizer(llm).repair(
        _spec(), _resolved(), previous="src", result=result, verdict=verdict
    )
    assert len(llm.calls[0].user) < 20_000
