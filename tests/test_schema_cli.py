from __future__ import annotations

import json
from pathlib import Path

from navigator_orchestrator.sdk.cli import main


def _project(tmp_path: Path) -> Path:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "thing.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "navigator-orchestrator.toml").write_text(
        """
[schemas.thing]
backend = "local"
method = "POST"
path = "schemas/thing.json"
source = "file"
""",
        encoding="utf-8",
    )
    return tmp_path


def test_schema_cli_sync_show_and_validate_are_offline(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    good = root / "good.json"
    bad = root / "bad.json"
    good.write_text('{"name":"safe"}', encoding="utf-8")
    bad.write_text('{"name":2}', encoding="utf-8")

    assert main(["schema", "--project", str(root), "sync", "thing"]) == 0
    assert "sha256:" in capsys.readouterr().out

    assert main(["schema", "--project", str(root), "show", "thing"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == "thing"
    assert shown["schema"]["required"] == ["name"]

    assert main(["schema", "--project", str(root), "validate", "thing", str(good)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert main(["schema", "--project", str(root), "validate", "thing", str(bad)]) == 1
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["findings"][0]["path"] == "/name"


def test_schema_diff_is_structured_and_nonzero_on_drift(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    assert main(["schema", "--project", str(root), "sync", "thing"]) == 0
    capsys.readouterr()
    changed = root / "changed.json"
    changed.write_text(
        '{"type":"object","required":["name","version"],'
        '"properties":{"name":{"type":"string"}},"additionalProperties":false}',
        encoding="utf-8",
    )

    assert (
        main(
            [
                "schema",
                "--project",
                str(root),
                "diff",
                "thing",
                "--against",
                str(changed),
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["drift"] is True
    assert result["changes"][0]["path"] == "/required"
