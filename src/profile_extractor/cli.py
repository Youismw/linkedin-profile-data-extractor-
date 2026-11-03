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

from profile_extractor.config import load_config, read_config_file
from profile_extractor.fetcher import (
    FakeFetcher,
    Fetcher,
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
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            return {}
    return {}


def _save_run_state(state: dict[str, str]) -> None:
    """Save last used input and output locations for subsequent runs."""
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _clean_output_path(path_str: str) -> str:
    """Strip redundant prefixes like 'output\\', 'output/', 'output:' if present."""
    raw = path_str.strip()
    raw_lower = raw.lower()
    for prefix in ("output\\", "output/", "output:", "out\\", "out/"):
        if raw_lower.startswith(prefix):
            return raw[len(prefix) :].strip()
    return raw


def _detect_output_intent(
    input_pos: str | None, output_pos: str | None
) -> tuple[str | None, str | None]:
    """Detect if user passed an output path (e.g. 'output\\...', '\\output', or 'output <path>')."""
    # 1. If input_pos is literally "output" or "out" (e.g. .\run output "C:\Users\...\nnn")
    if input_pos and input_pos.strip().lower() in ("output", "out"):
        cleaned_out = _clean_output_path(output_pos) if output_pos else None
        return None, cleaned_out

    # 2. Clean output_pos if it was explicitly provided with an 'output\' prefix
    if output_pos:
        output_pos = _clean_output_path(output_pos)

    # 3. If only one positional argument was provided, inspect if it signifies an output location
    if input_pos and not output_pos:
        raw = input_pos.strip()
        raw_lower = raw.lower()

        # Check for prefix: "output\", "output/", "output:", "out\", "out/"
        for prefix in ("output\\", "output/", "output:", "out\\", "out/"):
            if raw_lower.startswith(prefix):
                return None, raw[len(prefix) :].strip()

        # Check for suffix: "\output", "/output" (ignoring files like output.xlsx)
        if not raw_lower.endswith(".xlsx"):
            cleaned_slash = raw.rstrip("\\/")
            if (
                cleaned_slash.lower().endswith(("\\output", "/output"))
                or cleaned_slash.lower() == "output"
            ):
                return None, raw

    return input_pos, output_pos


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

    # --------------------------------------------------------------------------
    # 'logout' subcommand (manually clean / reset saved session and remembered paths)
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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the profile_extractor CLI."""
    if argv is None:
        argv = sys.argv[1:]

    # Default to 'run' subcommand if user typed flags/arguments without a subcommand
    known_commands = [
        "run",
        "login",
        "logout",
        "clean-session",
        "reset-session",
        "reset",
        "-h",
        "--help",
    ]
    if not argv or (argv[0] not in known_commands):
        argv = ["run", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    # --------------------------------------------------------------------------
    # Handler: logout / clean-session / reset-session / reset
    # --------------------------------------------------------------------------
    if args.command in ("logout", "clean-session", "reset-session", "reset"):
        target_auth = Path(args.auth_path or ".auth/state.json")
        target = (getattr(args, "target", None) or "all").lower()
        clear_session = target in ("all", "session") and not getattr(args, "paths_only", False)
        clear_paths = (
            getattr(args, "all_flag", False)
            or getattr(args, "all", False)
            or target in ("all", "paths")
            or not getattr(args, "session_only", False)
        )

        if clear_session:
            if target_auth.is_file():
                target_auth.unlink()
                print(f"[OK] LinkedIn session removed: {target_auth}")
            else:
                print(f"No active session found at: {target_auth}")

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

    # --------------------------------------------------------------------------
    # Handler: login
    # --------------------------------------------------------------------------
    if args.command == "login":
        login_auth = Path(args.auth_path or ".auth/state.json")
        try:
            run_interactive_login(login_auth)
            return 0
        except Exception as e:
            print(f"Login failed: {e}", file=sys.stderr)
            return 1

    # --------------------------------------------------------------------------
    # Handler: run
    # --------------------------------------------------------------------------
    if args.command == "run":
        overrides: dict[str, object] = {}
        input_pos, output_pos = _detect_output_intent(args.input_pos, args.output_pos)
        input_choice = args.input_file or input_pos
        output_choice = args.output_dir or output_pos

        run_state = _load_run_state()
        config_data = read_config_file(args.config_file)

        # 1. Resolve Input Spreadsheet Path
        # Precedence: Explicit CLI arg > Remembered run state > config.toml
        input_path: Path | None = None
        if input_choice:
            candidate = Path(input_choice)
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
                input_path = candidate
            else:
                desktop_fallback = Path.home() / "Desktop" / candidate.name
                if desktop_fallback.is_file():
                    input_path = desktop_fallback
                else:
                    print(
                        f"\n[ERROR] Specified input file does not exist: {candidate}\n",
                        file=sys.stderr,
                    )
                    return 1
            run_state["last_input"] = str(input_path.resolve())
        elif run_state.get("last_input"):
            remembered = Path(run_state["last_input"])
            if remembered.is_file():
                input_path = remembered
            else:
                run_state.pop("last_input", None)
                _save_run_state(run_state)
        elif config_data.get("input_file"):
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

        # 2. Resolve Output Directory Path
        # Precedence: Explicit CLI arg > Remembered run state > config.toml > 'output'
        out_dir_path: Path
        if output_choice:
            candidate_out = Path(output_choice)
            if candidate_out.is_file():
                print(
                    f"\n[ERROR] Output directory is a file, not a directory: {candidate_out}\n",
                    file=sys.stderr,
                )
                return 1
            out_dir_path = candidate_out
            run_state["last_output"] = str(out_dir_path.resolve())
        elif run_state.get("last_output"):
            rem_out = Path(run_state["last_output"])
            if rem_out.is_file():
                run_state.pop("last_output", None)
                out_dir_path = Path("output")
            else:
                out_dir_path = rem_out
        elif config_data.get("output_dir"):
            out_dir_path = Path(str(config_data["output_dir"]))
        else:
            # Default to standard project output/ directory
            out_dir_path = Path("output")

        # Validate output directory can be created
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

        # 3. Apply Optional Operational Flags
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

        config = load_config(args.config_file, overrides=overrides)
        setup_logging(level=config.log_level)
        print(f"Reading candidate spreadsheet: {config.input_file}")
        print(f"Output directory: {config.output_dir}")

        # 4. Instantiate Fetcher with Session Health Check
        fetcher: Fetcher
        if args.fake:
            fetcher = FakeFetcher()
        else:
            # Proactive session check: opens login window if cookies expired
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
                    profiles_dir.resolve() if profiles_dir.is_dir() else config.output_dir.resolve()
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
