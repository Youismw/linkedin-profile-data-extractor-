"""Command Line Interface for LinkedIn Profile Extractor.

Features:
- Single-command execution: `.\\run` or `.\\extract` (defaults to 'run').
- Shorthand positional syntax: `.\\run [input_file_or_dir] [output_dir]`.
- Persistent path memory: Remembers custom input and output paths in `.run_state.json`.
- Automatic session check: Gauges if LinkedIn cookie is expired and prompts login.
- Single Windows Explorer popup directly into `profiles/` upon completion.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from profile_extractor.config import load_config
from profile_extractor.fetcher import (
    FakeFetcher,
    PlaywrightFetcher,
    is_session_expired,
    run_interactive_login,
)
from profile_extractor.logging_setup import setup_logging
from profile_extractor.orchestrator import run_pipeline

# Local file storing remembered input/output paths across CLI runs
STATE_FILE = Path(".run_state.json")


def _load_run_state() -> dict[str, str]:
    """Load previously saved input and output locations from .run_state.json."""
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_run_state(state: dict[str, str]) -> None:
    """Save last used input and output locations for subsequent runs."""
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    """Construct and return the argument parser for profile_extractor."""
    parser = argparse.ArgumentParser(
        prog="profile_extractor",
        description="Extract profile data from LinkedIn profile URLs listed in an Excel file.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # --------------------------------------------------------------------------
    # 'run' subcommand
    # --------------------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="Run the profile extraction pipeline")
    run_parser.add_argument(
        "input_pos",
        nargs="?",
        default=None,
        help="Optional input Excel file (positional shorthand for -i / --input)",
    )
    run_parser.add_argument(
        "output_pos",
        nargs="?",
        default=None,
        help="Optional output directory anywhere on machine (positional shorthand for -o)",
    )
    run_parser.add_argument(
        "--input",
        "-i",
        dest="input_file",
        help="Path to Excel (.xlsx) file containing profile links",
    )
    run_parser.add_argument(
        "--output",
        "-o",
        dest="output_dir",
        help="Directory to save extracted JSON files and reports (can be anywhere on the machine)",
    )
    run_parser.add_argument(
        "--config",
        "-c",
        dest="config_file",
        help="Path to custom config.toml",
    )
    run_parser.add_argument(
        "--max-profiles",
        type=int,
        default=None,
        help="Maximum profiles to process (default: all)",
    )
    run_parser.add_argument(
        "--fake",
        action="store_true",
        help="Run pipeline using FakeFetcher (useful for testing and demo fallback)",
    )
    run_parser.add_argument(
        "--fast",
        action="store_true",
        help="Fast mode: minimal polite delays (0.5s - 1.0s) for quick testing/demos",
    )

    # --------------------------------------------------------------------------
    # 'login' subcommand (stand-alone session creator)
    # --------------------------------------------------------------------------
    login_parser = subparsers.add_parser(
        "login",
        help="One-time manual login helper to save authenticated session",
    )
    login_parser.add_argument(
        "--auth-path",
        default=None,
        help="Target path to save authenticated session state JSON",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the profile_extractor CLI."""
    if argv is None:
        argv = sys.argv[1:]

    # Default to 'run' subcommand if user typed flags/arguments without a subcommand
    if not argv or (argv[0] not in ["run", "login", "-h", "--help"]):
        argv = ["run", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    # --------------------------------------------------------------------------
    # Handler: login
    # --------------------------------------------------------------------------
    if args.command == "login":
        auth_path = args.auth_path or ".auth/state.json"
        try:
            run_interactive_login(auth_path)
            return 0
        except Exception as e:
            print(f"Login failed: {e}", file=sys.stderr)
            return 1

    # --------------------------------------------------------------------------
    # Handler: run
    # --------------------------------------------------------------------------
    if args.command == "run":
        overrides: dict[str, object] = {}
        input_choice = args.input_file or args.input_pos
        output_choice = args.output_dir or args.output_pos

        run_state = _load_run_state()

        # 1. Resolve Input Spreadsheet Path
        if input_choice:
            input_path = Path(input_choice)
            if input_path.is_dir():
                # Sort by newest modification time to avoid selecting stale files
                xlsx_files = sorted(
                    input_path.glob("*.xlsx"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if xlsx_files:
                    input_path = xlsx_files[0]
            elif not input_path.is_file():
                # Convenient fallback: check user Desktop
                desktop_fallback = Path.home() / "Desktop" / input_path.name
                if desktop_fallback.is_file():
                    input_path = desktop_fallback
            overrides["input_file"] = input_path
            run_state["last_input"] = str(input_path.resolve())
        else:
            # Check previously remembered input, or Desktop / input directory defaults
            last_input = (
                Path(run_state["last_input"]) if run_state.get("last_input") else None
            )
            desktop_profile = Path.home() / "Desktop" / "profiles.xlsx"
            default_input = Path("input/profiles.xlsx")

            if desktop_profile.is_file() and (
                not last_input
                or not last_input.is_file()
                or desktop_profile.stat().st_mtime > last_input.stat().st_mtime
            ):
                overrides["input_file"] = desktop_profile
                run_state["last_input"] = str(desktop_profile.resolve())
            elif last_input and last_input.is_file():
                overrides["input_file"] = last_input
            elif default_input.is_file():
                overrides["input_file"] = default_input
                run_state["last_input"] = str(default_input.resolve())
            else:
                input_dir = Path("input")
                if input_dir.is_dir():
                    xlsx_files = sorted(
                        input_dir.glob("*.xlsx"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if xlsx_files:
                        overrides["input_file"] = xlsx_files[0]
                        run_state["last_input"] = str(xlsx_files[0].resolve())

        # 2. Resolve Output Directory Path
        if output_choice:
            out_path = Path(output_choice)
            overrides["output_dir"] = out_path
            run_state["last_output"] = str(out_path.resolve())
        elif run_state.get("last_output"):
            overrides["output_dir"] = Path(run_state["last_output"])

        # Persist resolved paths for future runs
        _save_run_state(run_state)

        # 3. Apply Optional Operational Flags
        if args.max_profiles is not None:
            overrides["max_profiles"] = args.max_profiles
        if args.fast:
            overrides["delay_seconds_min"] = 0.5
            overrides["delay_seconds_max"] = 1.0

        config = load_config(args.config_file, overrides=overrides)
        setup_logging(level=config.log_level)

        # 4. Instantiate Fetcher with Session Health Check
        if args.fake:
            fetcher = FakeFetcher()
        else:
            # Proactive session check: opens login window if cookies expired
            if is_session_expired(config.auth_state_path):
                if sys.stdin and sys.stdin.isatty():
                    print("\n" + "=" * 70)
                    print("[!] LinkedIn session not found or has expired.")
                    print("    Opening LinkedIn login window now. Please sign in...")
                    print("=" * 70 + "\n")
                    run_interactive_login(config.auth_state_path)
                else:
                    print(
                        f"Error: Session missing/expired at '{config.auth_state_path}'.",
                        file=sys.stderr,
                    )
                    return 1

            fetcher = PlaywrightFetcher(
                auth_state_path=config.auth_state_path,
                headless=config.headless,
                timeout_seconds=config.page_timeout_seconds,
            )

        # 5. Execute Pipeline and Pop Output Folder
        try:
            summary = run_pipeline(config, fetcher)
            print(
                f"Execution finished: {summary.ok_count} OK, "
                f"{summary.unreachable_count} Unreachable, "
                f"{summary.not_processed_count} Not Processed. "
                f"Output saved to {summary.output_dir}"
            )

            # Automatically pop open a single Windows File Explorer window into profiles/
            try:
                profiles_dir = config.output_dir / "profiles"
                abs_output = (
                    profiles_dir.resolve()
                    if profiles_dir.is_dir()
                    else config.output_dir.resolve()
                )
                abs_output.mkdir(parents=True, exist_ok=True)
                print(f"Opening output folder: {abs_output}")
                if sys.platform == "win32":
                    opened = False
                    try:
                        os.startfile(str(abs_output))
                        opened = True
                    except Exception:
                        pass
                    if not opened:
                        try:
                            subprocess.Popen(f'explorer "{abs_output}"', shell=True)
                        except Exception:
                            pass
            except Exception:
                pass

            return 0
        except Exception as e:
            print(f"Error executing pipeline: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
