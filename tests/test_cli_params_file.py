"""`--params FILE` (PLAN-EDW-R2-003 stage A).

The other half of `make respond params=runs/monday.json`. The Makefile's job is
only to forward it; deciding what it *means* is the CLI's, and that is what is
asserted here — including the precedence rule, which is the part someone will
rely on without reading the docs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navigator_orchestrator.sdk.cli import _load_params_file, _parse_params

WORKFLOW = (
    'WORKFLOW = "doc-qa"\n\n\ndef answer(ctx, question, sources):\n    return f"A: {question}"\n'
)


def merged(file_params: dict[str, object], extras: list[str]) -> dict[str, object]:
    """What `_cmd_run` composes. A file is a default; a flag overrides it."""
    return {**file_params, **_parse_params(extras)}


def test_a_json_params_file_becomes_run_parameters(tmp_path: Path) -> None:
    target = tmp_path / "monday.json"
    target.write_text(json.dumps({"request_id": "RQ1", "limit": 5}), encoding="utf-8")

    assert _load_params_file(str(target)) == {"request_id": "RQ1", "limit": 5}


def test_a_yaml_params_file_works_too(tmp_path: Path) -> None:
    target = tmp_path / "monday.yaml"
    target.write_text("request_id: RQ1\nsites:\n  - bbcgoodfood.com\n", encoding="utf-8")

    assert _load_params_file(str(target)) == {"request_id": "RQ1", "sites": ["bbcgoodfood.com"]}


def test_hyphenated_keys_become_python_names(tmp_path: Path) -> None:
    """`--request-id` and `request_id` are the same parameter on the command
    line; a file must not be the one place where they are not."""
    target = tmp_path / "p.json"
    target.write_text('{"request-id": "RQ1"}', encoding="utf-8")

    assert _load_params_file(str(target)) == {"request_id": "RQ1"}


def test_an_explicit_flag_beats_the_file(tmp_path: Path) -> None:
    """A file is a default, not a mandate — otherwise changing one value means
    copying the file."""
    target = tmp_path / "p.json"
    target.write_text('{"request_id": "RQ1", "limit": 5}', encoding="utf-8")

    result = merged(_load_params_file(str(target)), ["--request-id", "RQ2"])
    assert result == {"request_id": "RQ2", "limit": 5}


def test_a_malformed_file_names_the_file_and_the_error(tmp_path: Path) -> None:
    target = tmp_path / "broken.json"
    target.write_text('{"request_id": "RQ1",\n', encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        _load_params_file(str(target))

    assert "broken.json" in str(caught.value)
    assert "Expecting" in str(caught.value), "the parser's own diagnosis, not a generic message"


def test_a_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="cannot read params file"):
        _load_params_file(str(tmp_path / "absent.json"))


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "list.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(SystemExit, match="expected a mapping"):
        _load_params_file(str(target))


def test_a_params_file_with_a_bom_loads(tmp_path: Path) -> None:
    """Windows tooling writes them, and a BOM is a real character in the string
    — here it would make the first key unmatchable."""
    target = tmp_path / "bom.json"
    target.write_bytes(b'\xef\xbb\xbf{"request_id": "RQ1"}')

    assert _load_params_file(str(target)) == {"request_id": "RQ1"}
