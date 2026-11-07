"""Test that README documents the keys, MCP tools and CLI commands."""

from pathlib import Path

import pytest

from orrery import mcp_server
from orrery.__main__ import build_parser


def test_readme_keys_match_the_keymap():
    """The README's key table lists every key with its label, and its key line is each tab's
    three rows at full width, so the two agree."""
    from orrery.tui import keymap

    readme = (Path(__file__).parent.parent / "README.md").read_text()
    missing = [f"| `{keymap.glyph(k.key)}` | {k.label} |" for k in keymap.KEYMAP]
    missing = [row for row in missing if row not in readme]
    assert not missing, "README.md's key table lacks:\n" + "\n".join(missing)
    for tab in ("week", "report"):
        rows = "\n".join(r.plain for r in keymap.key_rows(tab, 400))
        assert f"```\n{rows}\n```" in readme, tab


@pytest.mark.asyncio
async def test_readme_lists_mcp_tools_and_commands():
    """README.md must list every MCP tool and CLI subcommand.

    MCP tools are discovered from build_server's registered tools (mentor role).
    CLI subcommands are discovered from build_parser()'s subparsers.
    """
    readme_path = Path(__file__).parent.parent / "README.md"
    assert readme_path.exists(), "README.md must exist"
    readme_text = readme_path.read_text()

    # Get MCP tool names from build_server (mentor role has all read tools + write tools)
    srv = mcp_server.build_server(role="mentor")
    tools = await srv.list_tools()
    tool_names = [tool.name for tool in tools]

    # Get CLI subcommand names from build_parser()
    parser = build_parser()
    # Find the _SubParsersAction among parser._actions
    subparsers_action = next(
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    )
    subcommand_names = list(subparsers_action.choices.keys())

    # Check all tool names appear in README
    missing_tools = [name for name in tool_names if name not in readme_text]
    assert not missing_tools, "README.md must document these MCP tools:\n" + "\n".join(
        f"  - {name}" for name in missing_tools
    )

    # Check all subcommand names appear in README
    missing_commands = [name for name in subcommand_names if name not in readme_text]
    assert not missing_commands, "README.md must document these CLI commands:\n" + "\n".join(
        f"  - {name}" for name in missing_commands
    )


@pytest.mark.asyncio
async def test_readme_lists_organizer_tools():
    """README.md must list every organizer-role MCP tool."""
    readme_path = Path(__file__).parent.parent / "README.md"
    assert readme_path.exists(), "README.md must exist"
    readme_text = readme_path.read_text()

    # Get organizer tool names from build_server(role="organizer")
    srv = mcp_server.build_server(role="organizer")
    tools = await srv.list_tools()
    tool_names = [tool.name for tool in tools]

    # Check all tool names appear in README
    missing_tools = [name for name in tool_names if name not in readme_text]
    assert not missing_tools, "README.md must document these organizer MCP tools:\n" + "\n".join(
        f"  - {name}" for name in missing_tools
    )
