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
        "--input",
        "-i",
        dest="input_file",
        help="Path to Excel (.xlsx) file containing profile links",
    )
    run_parser.add_argument(
        "--output",
        "-o",
        dest="output_dir",
        help="Directory to save extracted JSON files and reports",
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
        if args.input_file:
            input_path = Path(args.input_file)
            if not input_path.is_file():
                # Check Desktop as convenient fallback
                desktop_fallback = Path.home() / "Desktop" / input_path.name
                if desktop_fallback.is_file():
                    input_path = desktop_fallback
            overrides["input_file"] = input_path
        if args.output_dir:
            overrides["output_dir"] = Path(args.output_dir)
        if args.max_profiles is not None:
            overrides["max_profiles"] = args.max_profiles

        config = load_config(args.config_file, overrides=overrides)
        setup_logging(level=config.log_level)

        # Select fetcher
        if args.fake:
            fetcher = FakeFetcher()
        else:
            if not config.auth_state_path.is_file():
                print(
                    f"Error: Authentication session not found at '{config.auth_state_path}'.\n"
                    "Please run 'profile_extractor login' first, or use '--fake' for testing.",
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
