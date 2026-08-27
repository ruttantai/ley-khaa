from ley_khaa.executor.resolver import ResolvedInput
from ley_khaa.persistence.orm import WorkflowRow
from ley_khaa.registry.binder import bind


def _workflow(roles):
    return WorkflowRow(
        id="w", name="w", description="", operation_aliases=["w"], output_format="csv",
        inputs=roles, source="", source_sha256="", origin="seed",
    )


def _resolved(name, filename):
    return ResolvedInput(name=name, filename=filename, content="ticker\nAAA\n", source="catalog")


def test_roles_bind_positionally_to_this_run_s_files():
    """The frozen script reads params["inputs"]["left"]. Binding is what puts
    THIS run's filename behind that name."""
    workflow = _workflow([
        {"role": "left", "suffixes": [".csv"]},
        {"role": "right", "suffixes": [".csv"]},
    ])
    resolved = [_resolved("bloomberg universe", "b.csv"), _resolved("factset universe", "f.csv")]

    assert bind(workflow, resolved) == {"left": "b.csv", "right": "f.csv"}


def test_a_suffix_mismatch_is_a_refusal_not_a_coercion():
    """A workflow that parses CSV handed an .xlsx will not fail cleanly — it
    will produce garbage that the validator may well accept."""
    workflow = _workflow([{"role": "left", "suffixes": [".csv"]}])
    assert bind(workflow, [_resolved("book", "b.xlsx")]) is None


def test_a_count_mismatch_is_a_refusal():
    workflow = _workflow([{"role": "left", "suffixes": [".csv"]}])
    resolved = [_resolved("a", "a.csv"), _resolved("b", "b.csv")]

    assert bind(workflow, resolved) is None
    assert bind(workflow, []) is None


def test_a_role_with_no_declared_suffixes_accepts_anything():
    """An empty list is 'no opinion', the same convention formats.py uses."""
    workflow = _workflow([{"role": "any", "suffixes": []}])
    assert bind(workflow, [_resolved("x", "x.docx")]) == {"any": "x.docx"}


def test_duplicate_roles_are_refused():
    """Two roles with one name means one of them silently wins in params.json,
    and the frozen script reads a file it was not given."""
    workflow = _workflow([
        {"role": "left", "suffixes": [".csv"]},
        {"role": "left", "suffixes": [".csv"]},
    ])
    resolved = [_resolved("a", "a.csv"), _resolved("b", "b.csv")]

    assert bind(workflow, resolved) is None


def test_a_malformed_role_declaration_is_a_refusal_not_a_crash():
    """A row can be hand-edited in the database. The matcher must survive it."""
    workflow = _workflow([{"suffixes": [".csv"]}])
    assert bind(workflow, [_resolved("a", "a.csv")]) is None
