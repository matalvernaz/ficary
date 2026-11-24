"""Command-line options that only change what another command does.

``--heal``, ``--prune``, ``--clear-library``, ``--apply``, ``--dry-run``
and the rest used on their own fell through to the download path. That
read whatever followed as a story address, so ``ficary --heal ~/Fanfic``
answered "Expected a URL like https://www.fanfiction.net/s/12345 or a
numeric ID", and ``ficary ~/Fanfic`` the same. Each is now refused with
the command it belongs to named.
"""

from __future__ import annotations

import pytest

from ficary import cli

# A value for the modifiers that take one.
VALUES = {"abandoned_after_days": ["30"], "library_dir": ["/home/someone/Fanfic"]}


@pytest.mark.parametrize(
    "dest, flag, partners",
    cli._MODIFIERS_AND_THEIR_COMMANDS,
    ids=[flag for _dest, flag, _partners in cli._MODIFIERS_AND_THEIR_COMMANDS],
)
def test_a_modifier_on_its_own_names_the_command_it_belongs_to(capsys, dest, flag, partners):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([flag, *VALUES.get(dest, [])])
    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert f"{flag} only works together with {partners}." in error
    assert "Expected a URL" not in error


def test_every_modifier_in_the_table_is_a_real_option():
    parser = cli._build_parser()
    known = {action.dest for action in parser._actions}
    for dest, flag, _partners in cli._MODIFIERS_AND_THEIR_COMMANDS:
        assert dest in known, flag
        assert flag in parser._option_string_actions, flag


def test_a_modifier_with_its_command_is_not_refused(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--cache-doctor", "--prune"])
    assert exit_info.value.code in (0, None)
    assert "only works together with" not in capsys.readouterr().err


def test_a_folder_is_not_taken_for_a_story(capsys, tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([str(tmp_path)])
    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert f"{tmp_path} is a folder, not a story address." in error
    assert "--scan-library DIR" in error


def test_a_story_number_is_still_a_story_whatever_folders_are_about(monkeypatch, tmp_path):
    """FanFiction.net takes a plain number. A folder with the same name
    where Ficary was started must not turn it into an error."""
    (tmp_path / "12345").mkdir()
    monkeypatch.chdir(tmp_path)
    asked = []
    monkeypatch.setattr(cli, "_download_one", lambda url, args, output_dir: asked.append(url) or True)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["12345"])
    assert exit_info.value.code == 0
    assert asked == ["12345"]
