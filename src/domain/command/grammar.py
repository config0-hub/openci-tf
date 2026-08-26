# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parse the deliberately small, safe ``tf`` comment grammar."""
import re

from src.core.models import Command


class ParseError(ValueError):
    pass


_PUBLIC_VERBS = frozenset({"plan", "report", "apply", "destroy"})


def accepted_verbs() -> tuple[str, ...]:
    return tuple(sorted(_PUBLIC_VERBS))


def unknown_verb_in_comment(text: str) -> str | None:
    tokens = text.strip().split()
    if len(tokens) < 2 or tokens[0].lower() != "tf":
        return None
    verb = tokens[1].lower()
    if verb in _PUBLIC_VERBS:
        return None
    return verb


_CONFIRM_TOKEN = re.compile(r"^[0-9a-f]{6,8}$")
_PIPELINE_NAME = re.compile(r"^[A-Za-z0-9_./-]+$")
_PIPELINE_STEP = re.compile(r"^[1-9][0-9]*$")


def parse_command(text: str) -> Command:
    tokens = text.strip().split()
    if len(tokens) < 2 or tokens[0].lower() != "tf":
        raise ParseError("expected: tf <verb> [folder-or-csv]")
    verb = tokens[1].lower()
    if verb == "validate":
        raise ParseError("validate is not a supported command; use tf plan")
    if verb not in _PUBLIC_VERBS:
        raise ParseError(f"unknown verb: {verb!r}")

    if verb in {"apply", "destroy"}:
        return _parse_mutating_command(verb, tokens[2:])

    destroy_flag = False
    rest = tokens[2:]
    if rest and rest[0] == "--destroy":
        if verb != "plan":
            raise ParseError("--destroy is only valid with tf plan")
        destroy_flag = True
        rest = rest[1:]

    if rest and rest[0].lower() == "pipeline":
        return _parse_read_only_pipeline_command(verb, rest, destroy_flag=destroy_flag)

    if verb == "report":
        if rest:
            raise ParseError(
                "tf report does not accept folder targets; use tf plan <folder-or-csv> for one or more folders"
            )
        return Command(action=verb, all_flag=True, destroy_flag=destroy_flag)

    if not rest:
        raise ParseError("tf plan requires a folder target")

    if len(rest) > 1:
        raise ParseError("expected: tf plan <folder-or-csv>")

    target = rest[0]
    if target == "all":
        raise ParseError(
            "tf plan all is not supported; use explicit folder CSV targets such as tf plan infra/a,infra/b"
        )

    folders = _parse_folder_list([target])
    if not folders:
        raise ParseError("a folder target is required")
    return Command(action=verb, folders=folders, destroy_flag=destroy_flag)


def _parse_read_only_pipeline_command(
    verb: str,
    tokens: list[str],
    *,
    destroy_flag: bool,
) -> Command:
    if verb == "report":
        raise ParseError("report is not supported for pipelines")
    if len(tokens) != 2:
        raise ParseError("expected: tf <verb> [--destroy] pipeline <name>")
    name = _validated_pipeline_name(tokens[1])
    return Command(action=verb, pipeline=name, destroy_flag=destroy_flag)


def _parse_mutating_command(verb: str, tokens: list[str]) -> Command:
    if not tokens:
        raise ParseError(f"{verb} requires folder targets or 'confirm <token>'")
    if tokens[0].lower() == "pipeline":
        return _parse_mutating_pipeline_command(verb, tokens)
    if tokens[0].lower() == "confirm":
        if len(tokens) != 2:
            raise ParseError(f"expected: tf {verb} confirm <token>")
        token = tokens[1].lower()
        if not _CONFIRM_TOKEN.fullmatch(token):
            raise ParseError("confirm token must be 6-8 lowercase hex characters")
        return Command(action=verb, confirm_token=token)
    if len(tokens) > 1:
        raise ParseError(f"expected: tf {verb} <folders>")
    target = tokens[0]
    if target == "all":
        raise ParseError(f"{verb} does not support 'all'; specify explicit folders")
    folders = _parse_folder_list([target])
    if not folders:
        raise ParseError("a folder target is required")
    return Command(action=verb, folders=folders)


def _parse_mutating_pipeline_command(verb: str, tokens: list[str]) -> Command:
    if verb == "destroy":
        raise ParseError("destroy pipeline is not supported")
    if len(tokens) not in {2, 4}:
        raise ParseError("expected: tf apply pipeline <name> [step <n>]")
    name = _validated_pipeline_name(tokens[1])
    step = 1
    if len(tokens) == 4:
        if tokens[2].lower() != "step":
            raise ParseError("expected: tf apply pipeline <name> [step <n>]")
        if not _PIPELINE_STEP.fullmatch(tokens[3]):
            raise ParseError("pipeline step must be an integer >= 1")
        step = int(tokens[3])
    return Command(action=verb, pipeline=name, pipeline_step=step)


def _validated_pipeline_name(name: str) -> str:
    if not name.strip():
        raise ParseError("pipeline name is required")
    if "," in name:
        raise ParseError("pipeline accepts exactly one name")
    if name == "all":
        raise ParseError("pipeline name 'all' is reserved")
    if not _PIPELINE_NAME.fullmatch(name):
        raise ParseError(f"invalid pipeline name: {name!r}")
    return name


def _parse_folder_list(tokens: list[str]) -> list[str]:
    raw = " ".join(tokens)
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part or any(char.isspace() for char in part) for part in parts):
        return []
    return parts
