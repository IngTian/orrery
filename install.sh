#!/bin/bash
set -euo pipefail

echo "Installing orrery..."
uv tool install --force --reinstall .

echo "Registering MCP server..."
if claude mcp get orrery >/dev/null 2>&1; then
    echo "MCP server already registered"
else
    claude mcp add -s user orrery -- orrery mcp
fi

echo ""
echo "Installation complete."
echo ""
orrery --help
echo ""
claude mcp get orrery
