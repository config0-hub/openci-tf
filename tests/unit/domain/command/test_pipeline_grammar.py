import pytest

from src.domain.command.grammar import ParseError, parse_command


@pytest.mark.parametrize(
    "text,action,destroy",
    [
        ("tf plan pipeline data/primary", "plan", False),
        ("tf plan --destroy pipeline data/primary", "plan", True),
        ("tf drift pipeline data/primary", "drift", False),
    ],
)
def test_pipeline_safe_grammar_accepts(text: str, action: str, destroy: bool) -> None:
    command = parse_command(text)

    assert command.action == action
    assert command.pipeline == "data/primary"
    assert command.pipeline_step is None
    assert command.folders == []
    assert command.all_flag is False
    assert command.affected_flag is False
    assert command.destroy_flag is destroy


@pytest.mark.parametrize(
    "text,step",
    [
        ("tf apply pipeline data/primary", 1),
        ("tf apply pipeline data/primary step 3", 3),
    ],
)
def test_apply_pipeline_grammar_accepts_step_cursor(text: str, step: int) -> None:
    command = parse_command(text)

    assert command.action == "apply"
    assert command.pipeline == "data/primary"
    assert command.pipeline_step == step
    assert command.folders == []


@pytest.mark.parametrize(
    "text,match",
    [
        ("tf report pipeline data/primary", "report is not supported"),
        ("tf plan pipeline data/primary all", "pipeline <name>"),
        ("tf plan all pipeline data/primary", "expected"),
        ("tf plan infra/vpc pipeline data/primary", "expected"),
        ("tf destroy pipeline data/primary", "destroy pipeline is not supported"),
        ("tf apply pipeline data/primary step ²", "pipeline step must be an integer"),
        ("tf apply pipeline data/primary step 0", "pipeline step must be an integer"),
        ("tf apply pipeline data/primary step 01", "pipeline step must be an integer"),
        ("tf apply pipeline data/primary step nope", "pipeline step must be an integer"),
        ("tf plan pipeline data/primary step 2", "pipeline <name>"),
        ("tf plan pipeline all", "pipeline name 'all' is reserved"),
        ("tf plan pipeline data/primary,infra/vpc", "pipeline accepts exactly one name"),
    ],
)
def test_pipeline_grammar_rejects_unsupported_combinations(text: str, match: str) -> None:
    with pytest.raises(ParseError, match=match):
        parse_command(text)
