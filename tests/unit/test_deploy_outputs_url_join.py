"""The $default HTTP API stage invoke_url ends with "/"; the deploy outputs must
not join it into "//webhook" (a "//webhook/<id>" hook URL does not match the
"POST /webhook/{trigger_id}" route)."""

from pathlib import Path

OUTPUTS = Path("infra/deploy/modules/openci_tf/outputs.tf").read_text(encoding="utf-8")


def _output_value(name: str) -> str:
    lines = OUTPUTS[OUTPUTS.index(f'output "{name}"'):].splitlines()
    return next(line for line in lines if line.strip().startswith("value"))


def test_webhook_url_trims_the_stage_invoke_url_before_appending_webhook() -> None:
    value = _output_value("webhook_url")
    assert 'trimsuffix(aws_apigatewayv2_stage.default.invoke_url, "/")' in value
    assert value.rstrip().endswith('/webhook"')
    assert "//webhook" not in value


def test_api_url_has_no_trailing_slash() -> None:
    assert 'trimsuffix(aws_apigatewayv2_stage.default.invoke_url, "/")' in _output_value("api_url")
