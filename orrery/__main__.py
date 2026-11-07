"""CLI entry point for orrery."""

import argparse
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from orrery import (
    __version__,
    board,
    db,
    export,
    log,
    paths,
    schedule,
    settings,
    store,
    text,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        ArgumentParser with all subcommands configured.
    """
    parser = argparse.ArgumentParser(
        prog="orrery",
        description=(
            "A terminal study-week planner: a TUI, an organizer chat, "
            "and an MCP board for AI mentors."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"orrery {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init subcommand
    subparsers.add_parser("init", help="Initialize orrery data directory")

    # export subcommand
    export_parser = subparsers.add_parser("export", help="Export all data")
    export_parser.add_argument(
        "--all", dest="dest", type=Path, required=True, help="Destination directory"
    )

    # backup subcommand
    backup_parser = subparsers.add_parser("backup", help="Backup database")
    backup_parser.add_argument("dest", type=Path, help="Destination directory")

    # today subcommand
    today_parser = subparsers.add_parser("today", help="Show today's plan")
    today_parser.add_argument("--date", type=date.fromisoformat, help="Date in YYYY-MM-DD format")

    # mcp subcommand
    mcp_parser = subparsers.add_parser("mcp", help="Start MCP server")
    mcp_parser.add_argument(
        "--role", choices=["mentor", "organizer"], default="mentor", help="MCP server role"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point for orrery.

    With no subcommand, starts the TUI.
    Subcommands: init, export, backup, today, mcp, --version.
    """
    parser = build_parser()

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code else 0

    if args.command == "init":
        return _cmd_init()
    elif args.command == "export":
        return _cmd_export(args.dest)
    elif args.command == "backup":
        return _cmd_backup(args.dest)
    elif args.command == "today":
        log.setup_logging()
        return _cmd_today(args.date)
    elif args.command == "mcp":
        from orrery.mcp_server import serve

        log.setup_logging()
        serve(args.role)
        return 0
    else:
        # No command: launch TUI
        from orrery.tui.app import OrreryApp

        log.setup_logging()
        OrreryApp().run()
        return 0


def _cmd_init() -> int:
    """Initialize orrery data directory."""
    root = paths.data_root()
    root.mkdir(parents=True, exist_ok=True)

    # Write config.toml
    config_path = root / "config.toml"
    if not config_path.exists():
        config_path.write_text(settings.CONFIG_TEMPLATE)
        print(f"Created {config_path}")

    # Write fronts.md
    fronts_path = root / "fronts.md"
    if not fronts_path.exists():
        fronts_path.write_text("# Fronts\n")
        print(f"Created {fronts_path}")

    # Load settings to get seed values, before any database file exists
    s = settings.load_settings(root)
    if isinstance(s, store.Unavailable):
        print(f"orrery: data unavailable ({s.reason})")
        return 1

    # Create and seed database
    db_file = db.db_path(root)
    db_existed = db_file.exists()

    try:
        conn = db.open_db(root)
    except RuntimeError as e:
        if str(e) == db.NEWER:
            print(f"orrery: data unavailable ({db.NEWER})")
            return 1
        raise

    with closing(conn):
        if not db_existed:
            print(f"Created {db_file}")
        # Seed if no init has yet: a new file, or one an earlier init left unseeded
        if conn.execute("select 1 from settings where key = 'starts'").fetchone() is None:
            today_date = schedule.today(datetime.now(), s.day_boundary_hour)
            schedule.seed(
                conn,
                subjects=s.seed_subjects,
                kinds=s.seed_kinds,
                cap=s.seed_cap,
                starts=today_date,
                now=datetime.now(),
            )
    return 0


def _cmd_export(dest: Path) -> int:
    """Export all data."""
    root = paths.data_root()
    out = export.export_all(root, dest, datetime.now())
    if isinstance(out, store.Unavailable):
        print(f"orrery: data unavailable ({out.reason})")
        return 1
    print(out)
    return 0


def _cmd_backup(dest: Path) -> int:
    """Backup database."""
    root = paths.data_root()
    out = export.backup(root, dest, datetime.now())
    if isinstance(out, store.Unavailable):
        print(f"orrery: data unavailable ({out.reason})")
        return 1
    print(out)
    return 0


def _cmd_today(date_arg: date | None) -> int:
    """Show today's plan."""
    root = paths.data_root()

    # Load settings
    s = settings.load_settings(root)
    if isinstance(s, store.Unavailable):
        print(f"orrery: data unavailable ({s.reason})")
        return 1

    # Open database
    conn = db.open_existing(root)
    if isinstance(conn, store.Unavailable):
        print(f"orrery: data unavailable ({conn.reason})")
        return 1

    # Calculate today
    now = datetime.now()
    today_date = date_arg if date_arg else schedule.today(now, s.day_boundary_hour)
    monday = schedule.week_start(today_date)

    # Build board and print
    b = board.build(conn, monday, today_date)
    print(text.today_text(b, None, schedule.starts(conn)))

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
