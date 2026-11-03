"""Command Line Interface for LinkedIn Profile Extractor."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from profile_extractor.config import load_config
from profile_extractor.fetcher import FakeFetcher, PlaywrightFetcher, run_interactive_login
from profile_extractor.logging_setup import setup_logging
from profile_extractor.orchestrator import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    """Construct and return the argument parser for profile-extractor."""
    parser = argparse.ArgumentParser(
        prog="profile_extractor",
        description="Extract profile data from LinkedIn profile URLs listed in an Excel file.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # 'run' subcommand
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

    # 'login' subcommand
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

    # Default to 'run' subcommand if not specified
    if not argv or (argv[0] not in ["run", "login", "-h", "--help"]):
        argv = ["run", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "login":
        auth_path = args.auth_path or ".auth/state.json"
        try:
            run_interactive_login(auth_path)
            return 0
        except Exception as e:
            print(f"Login failed: {e}", file=sys.stderr)
            return 1

    if args.command == "run":
        overrides: dict[str, object] = {}
        input_choice = args.input_file or args.input_pos
        output_choice = args.output_dir or args.output_pos

        if input_choice:
            input_path = Path(input_choice)
            if input_path.is_dir():
                xlsx_files = list(input_path.glob("*.xlsx"))
                if xlsx_files:
                    input_path = xlsx_files[0]
            elif not input_path.is_file():
                # Check Desktop as convenient fallback
                desktop_fallback = Path.home() / "Desktop" / input_path.name
                if desktop_fallback.is_file():
                    input_path = desktop_fallback
            overrides["input_file"] = input_path
        else:
            default_input = Path("input/profiles.xlsx")
            if not default_input.is_file():
                input_dir = Path("input")
                if input_dir.is_dir():
                    xlsx_files = list(input_dir.glob("*.xlsx"))
                    if xlsx_files:
                        overrides["input_file"] = xlsx_files[0]

        if output_choice:
            overrides["output_dir"] = Path(output_choice)
        if args.max_profiles is not None:
            overrides["max_profiles"] = args.max_profiles
        if args.fast:
            overrides["delay_seconds_min"] = 0.5
            overrides["delay_seconds_max"] = 1.0

        config = load_config(args.config_file, overrides=overrides)
        setup_logging(level=config.log_level)

        # Select fetcher
        if args.fake:
            fetcher = FakeFetcher()
        else:
            from profile_extractor.fetcher.login import is_session_expired

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

        try:
            summary = run_pipeline(config, fetcher)
            print(
                f"Execution finished: {summary.ok_count} OK, "
                f"{summary.unreachable_count} Unreachable, "
                f"{summary.not_processed_count} Not Processed. "
                f"Output saved to {summary.output_dir}"
            )

            # Automatically pop open the output folder in Windows File Explorer
            try:
                abs_output = config.output_dir.resolve()
                if sys.platform == "win32":
                    try:
                        import os

                        os.startfile(str(abs_output))
                    except Exception:
                        import subprocess

                        subprocess.Popen(["explorer.exe", str(abs_output)])
            except Exception:
                pass

            return 0
        except Exception as e:
            print(f"Error executing pipeline: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
