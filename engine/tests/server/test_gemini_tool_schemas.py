"""Empty optional enums must be represented by omission for Gemini tool calling."""

from typing import Any, cast

from crucible.envs.oncall.task import TOOLS, LogsArgs, Report


def test_oncall_tool_schemas_have_no_empty_enum_members() -> None:
    def check(value: Any) -> None:
        if isinstance(value, dict):
            if "enum" in value:
                assert "" not in value["enum"]
                assert "default" not in value or value["default"] in value["enum"]
            for child in cast(dict[str, Any], value).values():
                check(child)
        elif isinstance(value, list):
            for child in cast(list[Any], value):
                check(child)

    for tool in TOOLS:
        check(tool.json_schema())


def test_empty_optional_enum_arguments_remain_backwards_compatible() -> None:
    assert LogsArgs(service="checkout").level == ""
    assert LogsArgs(service="checkout", level="").level == ""
    assert Report().fault_kind == ""
    assert Report(fault_kind="").fault_kind == ""
    assert "level" not in LogsArgs.model_json_schema().get("required", [])
    assert "fault_kind" not in Report.model_json_schema().get("required", [])
