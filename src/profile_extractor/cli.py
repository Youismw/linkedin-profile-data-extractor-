"""Command Line Interface for LinkedIn Profile Extractor."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from profile_extractor.config import load_config
from profile_extractor.fetcher import FakeFetcher
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
        # Placeholder until Phase 5
        print(
            "Login helper will be launched in Phase 5. "
            "It will open Chromium for the consenting employee to log in."
        )
        return 0

    if args.command == "run":
        overrides: dict[str, object] = {}
        if args.input_file:
            overrides["input_file"] = Path(args.input_file)
        if args.output_dir:
            overrides["output_dir"] = Path(args.output_dir)
        if args.max_profiles is not None:
            overrides["max_profiles"] = args.max_profiles

        config = load_config(args.config_file, overrides=overrides)
        setup_logging(level=config.log_level)

        # Default to FakeFetcher until PlaywrightFetcher is implemented in Phase 5
        fetcher = FakeFetcher()
        try:
            summary = run_pipeline(config, fetcher)
            print(
                f"Execution finished: {summary.ok_count} OK, "
                f"{summary.unreachable_count} Unreachable, "
                f"{summary.not_processed_count} Not Processed. "
                f"Output saved to {summary.output_dir}"
            )
            return 0
        except Exception as e:
            print(f"Error executing pipeline: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
