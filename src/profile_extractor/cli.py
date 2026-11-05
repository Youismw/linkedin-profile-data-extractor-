"""Command Line Interface for LinkedIn Profile Extractor.

Module Architecture & Inter-Module Communication:
==================================================
This module serves as the primary user-facing entry point and controller for the
entire application. It parses command-line arguments, manages persistent path
state, verifies session authentication, instantiates fetchers, coordinates the
pipeline execution, and provides user feedback (including Windows Explorer launch).

Module Communications:
----------------------
1. Upstream Callers:
   - Command-line users via Windows Batch scripts: `.\\run` (run.bat) or `.\\extract` (extract.bat).
   - Direct execution via Python module launcher: `python -m profile_extractor`.
   - Automated testing harnesses in `tests/test_cli.py`, `tests/test_walking_skeleton.py`.

2. Downstream Callees & Collaborators:
   - `profile_extractor.config`:
     * Calls `read_config_file()` to parse base configurations from `config.toml`.
     * Calls `load_config()` to merge command-line overrides with configuration defaults.
   - `profile_extractor.fetcher`:
     * Calls `is_session_expired()` to check authentication cookie validity before starting.
     * Calls `run_interactive_login()` to launch a browser when manual login is required.
     * Instantiates `PlaywrightFetcher` for authenticated browser automation or `FakeFetcher` for tests.
   - `profile_extractor.logging_setup`:
     * Calls `setup_logging()` to configure console and file loggers according to user verbosity.
   - `profile_extractor.orchestrator`:
     * Calls `run_pipeline(config, fetcher)` to execute the full extraction workflow.
   - Operating System / Subprocess:
     * Reads/writes `.run_state.json` to remember previously selected input and output directories.
     * Uses `os.startfile()` and `subprocess.Popen("explorer ...")` to auto-open output folders on Windows.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# Configuration loaders: read TOML files and merge command-line flags
from profile_extractor.config import load_config, read_config_file

# Fetcher implementations and session authentication utilities
from profile_extractor.fetcher import (
    FakeFetcher,
    Fetcher,
    PlaywrightFetcher,
    is_session_expired,
    run_interactive_login,
)

# Centralized logging setup
from profile_extractor.logging_setup import setup_logging

# Core pipeline execution engine
from profile_extractor.orchestrator import run_pipeline

# ------------------------------------------------------------------------------
# Persistent State Management
# ------------------------------------------------------------------------------
# Local JSON file storing remembered input spreadsheet and output directory paths
# across consecutive CLI runs, eliminating repetitive path typing for users.
STATE_FILE = Path(".run_state.json")


def _load_run_state() -> dict[str, str]:
    """Load previously saved input and output locations from `.run_state.json`.

    Returns:
        Dictionary mapping state keys (e.g., 'last_input', 'last_output') to path strings,
        or an empty dictionary if the state file does not exist or contains invalid JSON.
    """
    # Check if the persistent state file exists on the local disk
    if STATE_FILE.is_file():
        try:
            # Read and parse the JSON state dictionary safely using UTF-8 encoding
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Ensure all keys and values are clean strings
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            # If the state file is corrupted or unreadable, safely degrade to an empty state
            return {}
    return {}


def _save_run_state(state: dict[str, str]) -> None:
    """Save last used input and output locations for subsequent runs.

    Args:
        state: Dictionary containing path mappings to persist.
    """
    try:
        # Serialize the state dictionary with 2-space indentation for human inspectability
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        # File write failures (e.g. read-only filesystem) must not crash the extraction pipeline
        pass


def _clean_output_path(path_str: str) -> str:
    """Strip redundant prefixes like 'output\\', 'output/', 'output:' if present.

    Handles shorthand patterns where users type 'output\\my_folder' or 'out\\my_folder'
    to indicate that the path is an output destination.

    Args:
        path_str: Raw path string passed by the user.

    Returns:
        Normalized path string with prefix stripped.
    """
    # Strip surrounding whitespace for consistent parsing
    raw = path_str.strip()
    raw_lower = raw.lower()

    # Iterate over common user prefix shorthands to strip
    for prefix in ("output\\", "output/", "output:", "out\\", "out/"):
        if raw_lower.startswith(prefix):
            # Remove the detected prefix and return the stripped remaining path
            return raw[len(prefix) :].strip()

    return raw


def _detect_output_intent(
    input_pos: str | None, output_pos: str | None
) -> tuple[str | None, str | None]:
    """Detect if user passed an output path using flexible CLI shorthand syntax.

    Supports multiple user mental models:
    - `.\\run output "C:\\my_output"` -> recognized as setting output, not input.
    - `.\\run "output\\my_dir"` -> recognized as output directory.
    - `.\\run "C:\\path\\output"` -> recognized as output directory when ending in \\output.
    - `.\\run "profiles.xlsx" "out_dir"` -> standard positional input and output.

    Args:
        input_pos: First positional argument, if provided.
        output_pos: Second positional argument, if provided.

    Returns:
        Tuple of (resolved_input_pos, resolved_output_pos).
    """
    # Case 1: If first argument is literally the keyword 'output' or 'out'
    # Example: .\run output "C:\Users\...\my_folder"
    if input_pos and input_pos.strip().lower() in ("output", "out"):
        # The second argument is the actual output directory; input remains unspecified
        cleaned_out = _clean_output_path(output_pos) if output_pos else None
        return None, cleaned_out

    # Case 2: Clean output_pos if explicitly passed with an 'output\' prefix
    if output_pos:
        output_pos = _clean_output_path(output_pos)

    # Case 3: If only one positional argument was provided, inspect if it indicates an output destination
    if input_pos and not output_pos:
        raw = input_pos.strip()
        raw_lower = raw.lower()

        # Check for directory prefixes: "output\", "output/", "output:", "out\", "out/"
        for prefix in ("output\\", "output/", "output:", "out\\", "out/"):
            if raw_lower.startswith(prefix):
                # The single argument is an output folder; input is resolved from state or config
                return None, raw[len(prefix) :].strip()

        # Check for directory suffixes: "\output" or "/output" (ignoring spreadsheets named output.xlsx)
        if not raw_lower.endswith(".xlsx"):
            cleaned_slash = raw.rstrip("\\/")
            if (
                cleaned_slash.lower().endswith(("\\output", "/output"))
                or cleaned_slash.lower() == "output"
            ):
                return None, raw

    # Default: Return arguments in their standard positional order
    return input_pos, output_pos


def build_parser() -> argparse.ArgumentParser:
    """Construct and configure the argument parser for the profile_extractor CLI.

    Configures subcommands:
    - 'run': Executes profile extraction pipeline (default subcommand).
    - 'login': Launches manual interactive browser session helper.
    - 'logout': Clears saved session authentication and/or remembered paths.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="profile_extractor",
        description="Extract profile data from LinkedIn profile URLs listed in an Excel file.",
    )
    # Define subcommands container
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # --------------------------------------------------------------------------
    # Subcommand: 'run' (Core extraction pipeline)
    # --------------------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="Run the profile extraction pipeline")

    # Positional shorthand arguments: allows typing `.\run profiles.xlsx output_dir`
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

    # Explicit flag arguments
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
    run_parser.add_argument(
        "--visible",
        "--headed",
        dest="visible",
        action="store_true",
        help="Launch visible Chromium browser window during extraction",
    )
    run_parser.add_argument(
        "--deep",
        dest="deep_details",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Automatically follow 'Show all' sub-pages for skills, education, "
            "and experience (default: True)"
        ),
    )
    run_parser.add_argument(
        "--prune-stale",
        dest="prune_stale",
        action="store_true",
        default=False,
        help="Prune JSON profiles in output that are not in current input (opt-in)",
    )
    run_parser.add_argument(
        "--disextra",
        dest="disextra",
        action="store_true",
        default=False,
        help="Disable supplemental discovery crawls (activity, interests, etc.) for faster extraction",
    )
    run_parser.add_argument(
        "--scrollsteps",
        "--scroolsteps",
        "--scroll-steps",
        dest="scroll_steps",
        type=int,
        default=None,
        help=(
            "Number of scroll steps per page (0 to 100, default: 25). "
            "More steps = fuller data but more time; fewer steps = faster but emptier."
        ),
    )
    run_parser.add_argument(
        "--steptime",
        "--step-time",
        dest="step_time",
        type=int,
        default=None,
        help=(
            "Delay between scroll steps in milliseconds (50 to 5000, default: 250). "
            "Pros: lower = faster runtime. Cons: too low = dynamic sections may not load in time."
        ),
    )
    run_parser.add_argument(
        "--aboutwait",
        "--about-wait",
        dest="about_wait",
        type=int,
        default=None,
        help="Maximum wait time in ms for About section to hydrate (0 to 30000, default: 1000).",
    )
    run_parser.add_argument(
        "--polite",
        "--delay",
        "--polite-wait",
        dest="polite_delay",
        type=float,
        default=None,
        help="Target polite delay between profiles in seconds (0.0 to 60.0, default: 1.5).",
    )
    run_parser.add_argument(
        "--no-open",
        action="store_true",
        default=False,
        help="Do not automatically open output directory in Windows Explorer when run finishes",
    )

    # --------------------------------------------------------------------------
    # Subcommand: 'login' (Interactive session creation)
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

    # --------------------------------------------------------------------------
    # Subcommand: 'logout' (Session & path reset)
    # --------------------------------------------------------------------------
    logout_parser = subparsers.add_parser(
        "logout",
        aliases=["clean-session", "reset-session", "reset"],
        help="Clear saved LinkedIn session, input location, and output location",
    )
    logout_parser.add_argument(
        "target",
        nargs="?",
        default="all",
        help="Target to reset: 'all' (default: resets all 3), 'session', or 'paths'",
    )
    logout_parser.add_argument(
        "--auth-path",
        default=None,
        help="Path to session state file (defaults to .auth/state.json)",
    )
    logout_parser.add_argument(
        "--all",
        dest="all_flag",
        action="store_true",
        default=False,
        help="Clear session AND remembered input/output paths (default behavior)",
    )
    logout_parser.add_argument(
        "--session-only",
        action="store_true",
        default=False,
        help="Clear only the LinkedIn session cookie, preserving remembered paths",
    )

    # --------------------------------------------------------------------------
    # Subcommand: 'gui' (Graphical user interface)
    # --------------------------------------------------------------------------
    subparsers.add_parser(
        "gui",
        help="Launch the graphical user interface assistant",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Primary entry point for the profile_extractor CLI execution.

    Coordinates command line argument parsing, subcommand routing, path resolution,
    session health checks, pipeline invocation, and output folder presentation.

    Args:
        argv: Command-line arguments list (defaults to sys.argv[1:]).

    Returns:
        Integer exit code (0 for success, non-zero for error).
    """
    # Default to sys.argv if no explicit arguments sequence was passed
    if argv is None:
        argv = sys.argv[1:]

    # Known subcommand identifiers: if the user typed positional arguments or flags
    # directly (e.g. `.\run profiles.xlsx`), automatically default subcommand to 'run'
    known_commands = [
        "run",
        "login",
        "logout",
        "clean-session",
        "reset-session",
        "reset",
        "gui",
        "-h",
        "--help",
    ]
    if not argv or (argv[0] not in known_commands):
        argv = ["run", *argv]

    # Parse command line arguments
    parser = build_parser()
    args = parser.parse_args(argv)

    # If no valid subcommand is resolved, display help information
    if not args.command:
        parser.print_help()
        return 0

    # ==========================================================================
    # Handler: logout / clean-session / reset-session / reset
    # ==========================================================================
    if args.command in ("logout", "clean-session", "reset-session", "reset"):
        # Resolve target authentication path (defaults to .auth/state.json)
        target_auth = Path(args.auth_path or ".auth/state.json")
        target = (getattr(args, "target", None) or "all").lower()

        # Determine whether to clear session cookies
        clear_session = target in ("all", "session") and not getattr(args, "paths_only", False)

        # Determine whether to clear remembered path state (.run_state.json)
        clear_paths = (
            getattr(args, "all_flag", False)
            or getattr(args, "all", False)
            or target in ("all", "paths")
            or not getattr(args, "session_only", False)
        )

        # 1. Delete session state file if requested
        if clear_session:
            if target_auth.is_file():
                target_auth.unlink()
                print(f"[OK] LinkedIn session removed: {target_auth}")
            else:
                print(f"No active session found at: {target_auth}")

        # 2. Delete remembered path history file if requested
        if clear_paths:
            if STATE_FILE.is_file():
                STATE_FILE.unlink()
                print(f"[OK] Remembered path history removed: {STATE_FILE}")
                print("     (both input and output locations reset)")
            else:
                print(f"No remembered path history found at: {STATE_FILE}")

        print("Logout complete. All 3 (session, input location, and output location) are reset.")
        print("Future runs will prompt for login and require specifying an input spreadsheet.")
        return 0

    # ==========================================================================
    # Handler: login (Interactive Session Setup)
    # ==========================================================================
    if args.command == "login":
        login_auth = Path(args.auth_path or ".auth/state.json")
        try:
            # Launch interactive browser window for manual 2FA/login
            run_interactive_login(login_auth)
            return 0
        except Exception as e:
            print(f"Login failed: {e}", file=sys.stderr)
            return 1

    # ==========================================================================
    # Handler: gui (Graphical User Interface)
    # ==========================================================================
    if args.command == "gui":
        from profile_extractor.gui import launch_gui

        return launch_gui()

    # ==========================================================================
    # Handler: run (Extraction Pipeline Execution)
    # ==========================================================================
    if args.command == "run":
        overrides: dict[str, object] = {}

        # Detect and clean flexible input and output positional intents
        input_pos, output_pos = _detect_output_intent(args.input_pos, args.output_pos)
        input_choice = args.input_file or input_pos
        output_choice = args.output_dir or output_pos

        # Load persisted path state and base config file
        run_state = _load_run_state()
        config_data = read_config_file(args.config_file)

        # ----------------------------------------------------------------------
        # 1. Resolve Input Spreadsheet Path
        # Precedence: Explicit CLI arg > Remembered run state > config.toml
        # ----------------------------------------------------------------------
        input_path: Path | None = None
        if input_choice:
            candidate = Path(input_choice)
            # If candidate is a directory, automatically select the newest spreadsheet file
            if candidate.is_dir():
                cand_files = sorted(
                    [
                        p
                        for p in candidate.glob("*")
                        if p.suffix.lower() in (".xlsx", ".xlsm", ".csv")
                        and not p.name.startswith("~$")
                    ],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if cand_files:
                    input_path = cand_files[0]
                else:
                    print(
                        f"\n[ERROR] No candidate files (.xlsx, .csv) found in: {candidate}\n",
                        file=sys.stderr,
                    )
                    return 1
            elif candidate.is_file():
                # Direct file path exists
                input_path = candidate
            else:
                # Desktop fallback: check if user provided only a filename that exists on Desktop
                desktop_fallback = Path.home() / "Desktop" / candidate.name
                if desktop_fallback.is_file():
                    input_path = desktop_fallback
                else:
                    print(
                        f"\n[ERROR] Specified input file does not exist: {candidate}\n",
                        file=sys.stderr,
                    )
                    return 1
            # Persist the newly specified input path
            run_state["last_input"] = str(input_path.resolve())
        elif run_state.get("last_input"):
            # Check previously remembered input path
            remembered = Path(run_state["last_input"])
            if remembered.is_file():
                input_path = remembered
            else:
                # Remove stale entry if file was deleted or moved
                run_state.pop("last_input", None)
                _save_run_state(run_state)
        elif config_data.get("input_file"):
            # Fall back to path configured in config.toml
            cfg_in = Path(str(config_data["input_file"]))
            if cfg_in.is_file():
                input_path = cfg_in
            else:
                print(
                    f"\n[ERROR] input_file configured in config.toml does not exist: {cfg_in}\n",
                    file=sys.stderr,
                )
                return 1

        # Fail honestly if no input file was provided, remembered, or configured
        if not input_path:
            print("\n" + "=" * 70, file=sys.stderr)
            print("[ERROR] No input spreadsheet provided.", file=sys.stderr)
            print(
                "You must specify an Excel (.xlsx/.xlsm) or CSV file with candidate URLs.",
                file=sys.stderr,
            )
            print("\nUsage example:", file=sys.stderr)
            print(r'  .\run "C:\path\to\profiles.xlsx"', file=sys.stderr)
            print(r'  .\run "C:\path\to\profiles.xlsx" "C:\path\to\output_dir"', file=sys.stderr)
            print("=" * 70 + "\n", file=sys.stderr)
            return 1

        overrides["input_file"] = input_path

        # ----------------------------------------------------------------------
        # 2. Resolve Output Directory Path
        # Precedence: Explicit CLI arg > Remembered run state > config.toml > 'output'
        # ----------------------------------------------------------------------
        out_dir_path: Path
        if output_choice:
            candidate_out = Path(output_choice)
            # Guard against specifying an existing file path as the output directory
            if candidate_out.is_file():
                print(
                    f"\n[ERROR] Output directory is a file, not a directory: {candidate_out}\n",
                    file=sys.stderr,
                )
                return 1
            out_dir_path = candidate_out
            run_state["last_output"] = str(out_dir_path.resolve())
        elif run_state.get("last_output"):
            # Check previously remembered output path
            rem_out = Path(run_state["last_output"])
            if rem_out.is_file():
                run_state.pop("last_output", None)
                out_dir_path = Path("output")
            else:
                out_dir_path = rem_out
        elif config_data.get("output_dir"):
            # Fall back to path configured in config.toml
            out_dir_path = Path(str(config_data["output_dir"]))
        else:
            # Default to standard project output/ directory
            out_dir_path = Path("output")

        # Validate that output directory can be created or written to
        try:
            out_dir_path.mkdir(parents=True, exist_ok=True)
            overrides["output_dir"] = out_dir_path
        except Exception as out_err:
            print(
                f"\n[ERROR] Cannot write to output directory '{out_dir_path}': {out_err}\n",
                file=sys.stderr,
            )
            run_state.pop("last_output", None)
            _save_run_state(run_state)
            return 1

        # Persist resolved paths for future runs
        _save_run_state(run_state)

        # ----------------------------------------------------------------------
        # 3. Apply Optional Operational Flags
        # ----------------------------------------------------------------------
        if args.max_profiles is not None:
            overrides["max_profiles"] = args.max_profiles
        if args.fast:
            overrides["delay_seconds_min"] = 0.5
            overrides["delay_seconds_max"] = 1.0
        if getattr(args, "visible", False):
            overrides["headless"] = False
        if getattr(args, "deep_details", None) is not None:
            overrides["deep_details"] = args.deep_details
        if getattr(args, "prune_stale", False):
            overrides["prune_stale"] = True
        if getattr(args, "disextra", False):
            overrides["disextra"] = True
        if getattr(args, "scroll_steps", None) is not None:
            if not (0 <= args.scroll_steps <= 100):
                print(
                    f"Error: --scrollsteps must be between 0 and 100 (got {args.scroll_steps})",
                    file=sys.stderr,
                )
                return 1
            overrides["scroll_steps"] = args.scroll_steps
        if getattr(args, "step_time", None) is not None:
            if not (50 <= args.step_time <= 5000):
                print(
                    f"Error: --steptime must be between 50 and 5000 ms (got {args.step_time})",
                    file=sys.stderr,
                )
                return 1
            overrides["scroll_step_delay_ms"] = args.step_time
        if getattr(args, "about_wait", None) is not None:
            if not (0 <= args.about_wait <= 30000):
                print(
                    f"Error: --aboutwait must be between 0 and 30000 ms (got {args.about_wait})",
                    file=sys.stderr,
                )
                return 1
            overrides["about_wait_ms"] = args.about_wait
        if getattr(args, "polite_delay", None) is not None:
            if not (0.0 <= args.polite_delay <= 60.0):
                print(
                    f"Error: --polite must be between 0.0 and 60.0s (got {args.polite_delay})",
                    file=sys.stderr,
                )
                return 1
            overrides["polite_delay"] = args.polite_delay

        # Load complete runtime configuration with all overrides merged
        config = load_config(args.config_file, overrides=overrides)
        setup_logging(level=config.log_level)
        print(f"Reading candidate spreadsheet: {config.input_file}")
        print(f"Output directory: {config.output_dir}")

        # ----------------------------------------------------------------------
        # 4. Instantiate Fetcher with Session Health Check
        # ----------------------------------------------------------------------
        fetcher: Fetcher
        if args.fake:
            # FakeFetcher returns mock HTML fixtures without network access
            fetcher = FakeFetcher()
        else:
            # Proactive session check: opens login window if cookies are missing or expired
            if is_session_expired(config.auth_state_path):
                print("\n" + "=" * 70)
                print("[!] LinkedIn session not found or has expired.")
                print("    Opening LinkedIn login window now. Please sign in...")
                print("=" * 70 + "\n")
                try:
                    run_interactive_login(config.auth_state_path)
                except Exception as e:
                    print(f"Error during interactive login: {e}", file=sys.stderr)
                    return 1

            # PlaywrightFetcher automates headless Chromium with the authenticated session
            fetcher = PlaywrightFetcher(
                auth_state_path=config.auth_state_path,
                headless=config.headless,
                timeout_seconds=config.page_timeout_seconds,
                max_scroll_steps=config.scroll_steps,
                scroll_step_delay_ms=config.scroll_step_delay_ms,
                about_wait_ms=config.about_wait_ms,
            )

        # ----------------------------------------------------------------------
        # 5. Execute Pipeline and Pop Output Folder in Windows Explorer
        # ----------------------------------------------------------------------
        try:
            # Run the extraction workflow across all rows
            summary = run_pipeline(config, fetcher)
            print(
                f"Execution finished: {summary.ok_count} OK, "
                f"{summary.unreachable_count} Unreachable, "
                f"{summary.not_processed_count} Not Processed. "
                f"Output saved to {summary.output_dir}"
            )

            # Automatically pop open a single Windows File Explorer window directly into profiles/
            # (Skipped if --no-open was requested, preventing Windows from locking the output folder)
            if not getattr(args, "no_open", False):
                try:
                    profiles_dir = config.output_dir / "profiles"
                    abs_output = (
                        profiles_dir.resolve() if profiles_dir.is_dir() else config.output_dir.resolve()
                    )
                    abs_output.mkdir(parents=True, exist_ok=True)
                    print(f"Opening output folder: {abs_output}")
                    if sys.platform == "win32":
                        opened = False
                        # Primary mechanism: os.startfile() invokes native Windows shell association
                        try:
                            os.startfile(str(abs_output))
                            opened = True
                        except Exception:
                            pass
                        # Secondary fallback: launch explorer.exe directly via subprocess
                        if not opened:
                            try:
                                subprocess.Popen(f'explorer "{abs_output}"', shell=True)
                            except Exception:
                                pass
                except Exception:
                    # Explorer popup errors must never prevent returning success code
                    pass

            if summary.rows_valid > 0 and summary.ok_count == 0:
                print(
                    "\n[WARNING] Pipeline completed with 0 successful profiles extracted. "
                    "All candidate profiles were unreachable or blocked.",
                    file=sys.stderr,
                )
                return 2

            return 0
        except Exception as e:
            print(f"Error executing pipeline: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    # Script entry point when executed directly
    sys.exit(main())
