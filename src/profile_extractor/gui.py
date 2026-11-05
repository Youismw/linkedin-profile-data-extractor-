"""Graphical User Interface for LinkedIn Profile Extractor.

Module Architecture & Inter-Module Communication:
==================================================
This module provides a lightweight, native, dependency-free GUI (via standard library
tkinter / ttk) to configure and run the extraction pipeline without requiring manual
terminal flag syntax.

Module Communications:
----------------------
1. Upstream Callers:
   - `gui.bat` / `run_gui.bat`: Windows batch launchers that start Python with
     `profile_extractor.gui`.
   - `profile_extractor.cli`: Dispatches `.\\run gui` directly to `launch_gui()`.
   - Direct execution: `python -m profile_extractor.gui`.

2. Downstream Modules Called:
   - `profile_extractor.cli`:
     * Reads and updates `STATE_FILE` (.run_state.json) to synchronize remembered paths
       between terminal and GUI runs.
     * Invokes extraction and login subcommands via non-blocking subprocesses.
   - `profile_extractor.config`:
     * Reads default scroll steps, delays, and timeout settings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

# Standard library GUI modules (built into Python on Windows)
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

# Path to persistent run state file (shared with CLI)
STATE_FILE = Path(".run_state.json")


def _load_run_state() -> dict[str, str]:
    """Load previously saved input/output path history from disk."""
    if STATE_FILE.is_file():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            return {}
    return {}


def _save_run_state(state: dict[str, str]) -> None:
    """Persist input/output path history to disk."""
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


class ExtractorApp:
    """Main Application Window for the LinkedIn Profile Extractor GUI."""

    def __init__(self, root: tk.Tk) -> None:
        """Initialize and lay out the GUI application window.

        Args:
            root: Root Tkinter instance.
        """
        self.root = root
        self.root.title("LinkedIn Candidate Profile Extractor - Assistant")
        self.root.geometry("840x840")
        self.root.minsize(740, 700)

        # Apply native Windows look-and-feel if available
        self.style = ttk.Style()
        available_themes = self.style.theme_names()
        if "vista" in available_themes:
            self.style.theme_use("vista")
        elif "clam" in available_themes:
            self.style.theme_use("clam")

        # Custom styling for section headers and explanation hints
        self.style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"))
        self.style.configure("SubHeader.TLabel", font=("Segoe UI", 9), foreground="#555555")
        self.style.configure("Hint.TLabel", font=("Segoe UI", 8, "italic"), foreground="#666666")
        self.style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))

        # Active background worker process reference
        self.process: subprocess.Popen[str] | None = None
        self._is_running = False

        # Load remembered paths
        run_state = _load_run_state()
        default_input = run_state.get("last_input", "")
        if not default_input and Path("C:/Users/ROHIT CHAUHAN/Desktop/profiles.xlsx").is_file():
            default_input = "C:/Users/ROHIT CHAUHAN/Desktop/profiles.xlsx"
        default_output = run_state.get("last_output", "output")

        # Reactive Variables
        self.input_var = tk.StringVar(value=default_input)
        self.output_var = tk.StringVar(value=default_output)

        # Flares & Speed Toggles
        self.disextra_var = tk.BooleanVar(value=True)  # Default ON for fast resume extraction
        self.fast_var = tk.BooleanVar(value=False)      # Single-page fast mode
        self.visible_var = tk.BooleanVar(value=False)   # Show Chromium browser
        self.prune_var = tk.BooleanVar(value=False)     # Clear old output files

        # Numeric Sliders
        self.scroll_steps_var = tk.IntVar(value=25)     # Default 25 (0 to 100)
        self.step_time_var = tk.IntVar(value=250)       # Default 250 ms (50 to 2000)
        self.about_wait_var = tk.IntVar(value=1000)     # Default 1000 ms (0 to 5000)
        self.polite_delay_var = tk.DoubleVar(value=1.5) # Default 1.5 s (0.0 to 10.0)

        # Status text
        self.status_var = tk.StringVar(value="Ready")

        # Build UI Sections
        self._build_layout()

        # Handle window close while extraction is active
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_layout(self) -> None:
        """Construct the widget hierarchy within the root window."""
        main_container = ttk.Frame(self.root, padding=12)
        main_container.pack(fill=tk.BOTH, expand=True)

        # ----------------------------------------------------------------------
        # 1. Header Banner
        # ----------------------------------------------------------------------
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=tk.X, pady=(0, 8))

        title_lbl = ttk.Label(
            header_frame,
            text="LinkedIn Profile Extractor",
            style="Header.TLabel",
        )
        title_lbl.pack(anchor=tk.W)

        subtitle_lbl = ttk.Label(
            header_frame,
            text="Headless Extraction Pipeline - Configure runtime flares and execution speed.",
            style="SubHeader.TLabel",
        )
        subtitle_lbl.pack(anchor=tk.W, pady=(2, 0))

        ttk.Separator(main_container, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(3, 8))

        # ----------------------------------------------------------------------
        # 2. File & Directory Locations Frame
        # ----------------------------------------------------------------------
        paths_frame = ttk.LabelFrame(main_container, text=" File & Folder Locations ", padding=8)
        paths_frame.pack(fill=tk.X, pady=(0, 8))

        # Candidate Spreadsheet Row
        ttk.Label(paths_frame, text="Candidate Spreadsheet (.xlsx, .csv):").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 8), pady=3
        )
        input_entry = ttk.Entry(paths_frame, textvariable=self.input_var, width=54)
        input_entry.grid(row=0, column=1, sticky=tk.EW, padx=(0, 8), pady=3)
        browse_input_btn = ttk.Button(paths_frame, text="Browse...", command=self._browse_input)
        browse_input_btn.grid(row=0, column=2, pady=3)

        # Input Explanation Hint
        input_hint = ttk.Label(
            paths_frame,
            text="Select an Excel or CSV file containing candidate LinkedIn profile URLs.",
            style="Hint.TLabel",
        )
        input_hint.grid(row=1, column=1, sticky=tk.W, pady=(0, 4))

        # Output Directory Row
        ttk.Label(paths_frame, text="Output Directory:").grid(
            row=2, column=0, sticky=tk.W, padx=(0, 8), pady=3
        )
        output_entry = ttk.Entry(paths_frame, textvariable=self.output_var, width=54)
        output_entry.grid(row=2, column=1, sticky=tk.EW, padx=(0, 8), pady=3)

        btn_box = ttk.Frame(paths_frame)
        btn_box.grid(row=2, column=2, sticky=tk.W, pady=3)
        browse_output_btn = ttk.Button(btn_box, text="Browse...", command=self._browse_output)
        browse_output_btn.pack(side=tk.LEFT, padx=(0, 4))
        open_output_btn = ttk.Button(btn_box, text="Open Folder", command=self._open_output_dir)
        open_output_btn.pack(side=tk.LEFT)

        # Output Explanation Hint
        output_hint = ttk.Label(
            paths_frame,
            text="Destination folder where extracted profile JSON files and reports are stored.",
            style="Hint.TLabel",
        )
        output_hint.grid(row=3, column=1, sticky=tk.W, pady=(0, 2))

        paths_frame.columnconfigure(1, weight=1)

        # ----------------------------------------------------------------------
        # 3. Flare Settings & Speed Controls Frame
        # ----------------------------------------------------------------------
        flares_frame = ttk.LabelFrame(
            main_container, text=" Crawler Flares & Speed Controls ", padding=8
        )
        flares_frame.pack(fill=tk.X, pady=(0, 8))

        # Left Column: Checkbox Toggles
        left_col = ttk.Frame(flares_frame)
        left_col.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 15))

        # Flare: Disable Extra Feeds (--disextra)
        disextra_cb = ttk.Checkbutton(
            left_col,
            text="Disable Supplemental Feeds (--disextra)",
            variable=self.disextra_var,
        )
        disextra_cb.pack(anchor=tk.W)
        ttk.Label(
            left_col,
            text="Skips /recent-activity/ and /details/interests/ for 3x–4x faster run.",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, padx=(20, 0), pady=(0, 6))

        # Flare: Single-Page Fast Mode (--fast / --no-deep)
        fast_cb = ttk.Checkbutton(
            left_col,
            text="Single-Page Fast Mode (--fast)",
            variable=self.fast_var,
        )
        fast_cb.pack(anchor=tk.W)
        ttk.Label(
            left_col,
            text="Extracts top card only without visiting deep subpages (~5s/profile).",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, padx=(20, 0), pady=(0, 6))

        # Flare: Visible Browser (--visible)
        visible_cb = ttk.Checkbutton(
            left_col,
            text="Show Chromium Browser Window (--visible)",
            variable=self.visible_var,
        )
        visible_cb.pack(anchor=tk.W)
        ttk.Label(
            left_col,
            text="Opens browser visibly so you can watch live page navigation.",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, padx=(20, 0), pady=(0, 6))

        # Flare: Prune Stale Profiles (--prune)
        prune_cb = ttk.Checkbutton(
            left_col,
            text="Prune Old Profiles Before Run (--prune)",
            variable=self.prune_var,
        )
        prune_cb.pack(anchor=tk.W)
        ttk.Label(
            left_col,
            text="Clears old JSON profiles from output directory before starting.",
            style="Hint.TLabel",
        ).pack(anchor=tk.W, padx=(20, 0))

        # Right Column: Numeric Sliders
        right_col = ttk.Frame(flares_frame)
        right_col.grid(row=0, column=1, sticky=tk.NSEW, padx=(15, 0))

        # Flare: Scroll Steps (--scrollsteps)
        self.scroll_lbl = ttk.Label(
            right_col,
            text=f"Scroll Depth Steps: {self.scroll_steps_var.get()} steps",
            font=("Segoe UI", 9, "bold"),
        )
        self.scroll_lbl.pack(anchor=tk.W)

        scroll_scale = ttk.Scale(
            right_col,
            from_=0,
            to=100,
            variable=self.scroll_steps_var,
            orient=tk.HORIZONTAL,
            command=self._on_scroll_slider_change,
        )
        scroll_scale.pack(fill=tk.X, pady=(1, 1))

        ttk.Label(
            right_col,
            text="25 steps gets ~80% data; 40 steps gets ~90%+. Less = faster, more = fuller.",
            style="Hint.TLabel",
            wraplength=350,
        ).pack(anchor=tk.W, pady=(0, 6))

        # Flare: Step Time Delay (--steptime)
        self.step_time_lbl = ttk.Label(
            right_col,
            text=f"Scroll Step Delay: {self.step_time_var.get()} ms",
            font=("Segoe UI", 9, "bold"),
        )
        self.step_time_lbl.pack(anchor=tk.W)

        step_scale = ttk.Scale(
            right_col,
            from_=50,
            to=1000,
            variable=self.step_time_var,
            orient=tk.HORIZONTAL,
            command=self._on_steptime_slider_change,
        )
        step_scale.pack(fill=tk.X, pady=(1, 1))

        ttk.Label(
            right_col,
            text="Delay between scrolls. Faster vs safer. Keep >= 150ms to avoid bot rate limits.",
            style="Hint.TLabel",
            wraplength=350,
        ).pack(anchor=tk.W, pady=(0, 6))

        # Flare: About Section Wait (--aboutwait)
        self.about_wait_lbl = ttk.Label(
            right_col,
            text=f"About Section Ready Wait: {self.about_wait_var.get()} ms",
            font=("Segoe UI", 9, "bold"),
        )
        self.about_wait_lbl.pack(anchor=tk.W)

        about_scale = ttk.Scale(
            right_col,
            from_=0,
            to=5000,
            variable=self.about_wait_var,
            orient=tk.HORIZONTAL,
            command=self._on_aboutwait_slider_change,
        )
        about_scale.pack(fill=tk.X, pady=(1, 1))

        ttk.Label(
            right_col,
            text=(
                "Max wait for About card to hydrate. "
                "Stops immediately once loaded. (Default: 1000ms)"
            ),
            style="Hint.TLabel",
            wraplength=350,
        ).pack(anchor=tk.W, pady=(0, 6))

        # Flare: Polite Delay Between Profiles (--polite)
        self.polite_delay_lbl = ttk.Label(
            right_col,
            text=f"Polite Delay Between Profiles: {self.polite_delay_var.get():.1f} s",
            font=("Segoe UI", 9, "bold"),
        )
        self.polite_delay_lbl.pack(anchor=tk.W)

        polite_scale = ttk.Scale(
            right_col,
            from_=0.0,
            to=10.0,
            variable=self.polite_delay_var,
            orient=tk.HORIZONTAL,
            command=self._on_polite_slider_change,
        )
        polite_scale.pack(fill=tk.X, pady=(1, 1))

        ttk.Label(
            right_col,
            text=(
                "Pause between visiting candidates. "
                "Lower = faster batch, higher = safer against bot tracking."
            ),
            style="Hint.TLabel",
            wraplength=350,
        ).pack(anchor=tk.W)

        flares_frame.columnconfigure(0, weight=1)
        flares_frame.columnconfigure(1, weight=1)

        # ----------------------------------------------------------------------
        # 4. Action Toolbar & Status Bar
        # ----------------------------------------------------------------------
        action_frame = ttk.Frame(main_container)
        action_frame.pack(fill=tk.X, pady=(0, 8))

        self.start_btn = ttk.Button(
            action_frame,
            text="▶  Start Extraction",
            command=self._start_extraction,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8), ipadx=8, ipady=4)

        self.stop_btn = ttk.Button(
            action_frame,
            text="⏹  Stop",
            command=self._stop_extraction,
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8), ipadx=4, ipady=4)

        self.login_btn = ttk.Button(
            action_frame,
            text="🔑  LinkedIn Login",
            command=self._launch_login,
        )
        self.login_btn.pack(side=tk.LEFT, padx=(0, 8), ipadx=4, ipady=4)

        # Status text indicator on the right
        status_box = ttk.Frame(action_frame)
        status_box.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Label(status_box, text="Status: ", style="SubHeader.TLabel").pack(side=tk.LEFT)
        self.status_display = ttk.Label(
            status_box,
            textvariable=self.status_var,
            style="Status.TLabel",
            foreground="#007acc",
        )
        self.status_display.pack(side=tk.LEFT)

        # ----------------------------------------------------------------------
        # 5. Live Console Log Area
        # ----------------------------------------------------------------------
        console_frame = ttk.LabelFrame(main_container, text=" Live Pipeline Console ", padding=8)
        console_frame.pack(fill=tk.BOTH, expand=True)

        console_bar = ttk.Frame(console_frame)
        console_bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(
            console_bar,
            text="Real-time execution logs and progress audits:",
            style="Hint.TLabel",
        ).pack(side=tk.LEFT)
        clear_btn = ttk.Button(console_bar, text="Clear Log", command=self._clear_log)
        clear_btn.pack(side=tk.RIGHT)

        self.log_text = ScrolledText(
            console_frame,
            wrap=tk.WORD,
            background="#1e1e1e",
            foreground="#d4d4d4",
            insertbackground="#ffffff",
            font=("Consolas", 9),
            padx=8,
            pady=8,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Setup syntax highlighting tags
        self.log_text.tag_config("info", foreground="#569cd6")
        self.log_text.tag_config("warning", foreground="#ce9178")
        self.log_text.tag_config("error", foreground="#f44747")
        self.log_text.tag_config("success", foreground="#4ec9b0")
        self.log_text.tag_config("dim", foreground="#808080")

        self._log_line(
            "System ready. Verify candidate spreadsheet path and click 'Start Extraction'.\n",
            tag="dim",
        )

    # --------------------------------------------------------------------------
    # Event Handlers & Sliders
    # --------------------------------------------------------------------------
    def _on_scroll_slider_change(self, val: str) -> None:
        """Update label text when scroll depth slider moves."""
        steps = int(float(val))
        self.scroll_lbl.config(text=f"Scroll Depth Steps: {steps} steps")

    def _on_steptime_slider_change(self, val: str) -> None:
        """Update label text when step delay slider moves."""
        ms = int(float(val))
        self.step_time_lbl.config(text=f"Scroll Step Delay: {ms} ms")

    def _on_aboutwait_slider_change(self, val: str) -> None:
        """Update label text when About wait slider moves."""
        ms = int(float(val))
        self.about_wait_lbl.config(text=f"About Section Ready Wait: {ms} ms")

    def _on_polite_slider_change(self, val: str) -> None:
        """Update label text when polite delay slider moves."""
        sec = float(val)
        self.polite_delay_lbl.config(text=f"Polite Delay Between Profiles: {sec:.1f} s")

    def _browse_input(self) -> None:
        """Open file picker dialog for candidate spreadsheet."""
        chosen = filedialog.askopenfilename(
            title="Select Candidate Spreadsheet",
            filetypes=[
                ("Excel & CSV Spreadsheets", "*.xlsx;*.xlsm;*.csv"),
                ("Excel Spreadsheets (*.xlsx)", "*.xlsx"),
                ("Macro-Enabled Excel (*.xlsm)", "*.xlsm"),
                ("CSV Files (*.csv)", "*.csv"),
                ("All Files (*.*)", "*.*"),
            ],
        )
        if chosen:
            self.input_var.set(chosen)

    def _browse_output(self) -> None:
        """Open folder picker dialog for output destination."""
        chosen = filedialog.askdirectory(title="Select Output Directory")
        if chosen:
            self.output_var.set(chosen)

    def _open_output_dir(self) -> None:
        """Open the currently specified output directory in Windows Explorer."""
        out_path = Path(self.output_var.get().strip() or "output")
        try:
            out_path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(out_path.resolve()))
            else:
                subprocess.Popen(["xdg-open", str(out_path.resolve())])
        except Exception as e:
            messagebox.showerror("Error Opening Folder", f"Could not open directory:\n{e}")

    def _clear_log(self) -> None:
        """Clear console log output."""
        self.log_text.delete("1.0", tk.END)

    def _log_line(self, line: str, tag: str | None = None) -> None:
        """Append a single line of log text with appropriate color coding.

        Args:
            line: Log string to append.
            tag: Optional explicit tag style name.
        """
        # Auto-detect tag if not explicitly passed
        if not tag:
            lower = line.lower()
            if "[error]" in lower or "traceback" in lower or "failed" in lower:
                tag = "error"
            elif "[warning]" in lower or "blocked" in lower or "rate limit" in lower:
                tag = "warning"
            elif "ok" in lower or "completed" in lower or "execution finished" in lower:
                tag = "success"
            elif "[info]" in lower:
                tag = "info"

        if tag is not None:
            self.log_text.insert(tk.END, line, tag)
        else:
            self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)

    # --------------------------------------------------------------------------
    # Process Lifecycle & Threaded Streaming
    # --------------------------------------------------------------------------
    def _start_extraction(self) -> None:
        """Validate inputs and launch the extraction pipeline subprocess."""
        if self._is_running:
            return

        input_path_str = self.input_var.get().strip()
        output_path_str = self.output_var.get().strip()

        if not input_path_str:
            messagebox.showwarning(
                "Missing Input", "Please select a candidate spreadsheet (.xlsx or .csv)."
            )
            return

        input_path = Path(input_path_str)
        if not input_path.is_file():
            messagebox.showerror(
                "File Not Found", f"Specified input spreadsheet does not exist:\n{input_path}"
            )
            return

        if not output_path_str:
            output_path_str = "output"
            self.output_var.set(output_path_str)

        # Persist remembered state for future CLI and GUI sessions
        _save_run_state({"last_input": str(input_path.resolve()), "last_output": output_path_str})

        # Assemble CLI argument list
        cmd: list[str] = [
            sys.executable,
            "-u",  # Unbuffered standard output for instantaneous streaming
            "-m",
            "profile_extractor",
            "run",
            str(input_path.resolve()),
            output_path_str,
        ]

        # Apply Flares & Flags
        if self.disextra_var.get():
            cmd.append("--disextra")
        if self.fast_var.get():
            cmd.append("--no-deep")
        if self.visible_var.get():
            cmd.append("--visible")
        if self.prune_var.get():
            cmd.append("--prune")

        scroll_steps = int(self.scroll_steps_var.get())
        step_time = int(self.step_time_var.get())
        about_wait = int(self.about_wait_var.get())
        polite_delay = float(self.polite_delay_var.get())

        cmd.extend(["--scrollsteps", str(scroll_steps)])
        cmd.extend(["--steptime", str(step_time)])
        cmd.extend(["--aboutwait", str(about_wait)])
        cmd.extend(["--polite", f"{polite_delay:.1f}"])

        # Update UI state
        self._set_running_state(True)
        self.status_var.set("Running Pipeline...")
        self.status_display.config(foreground="#007acc")

        self._log_line("\n" + "=" * 65 + "\n", tag="dim")
        self._log_line(f"Launching pipeline command:\n{' '.join(cmd)}\n\n", tag="info")

        # Launch background process with hidden console window on Windows
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags,
            )
        except Exception as e:
            self._set_running_state(False)
            self.status_var.set("Failed to Launch")
            self.status_display.config(foreground="#f44747")
            messagebox.showerror("Execution Error", f"Failed to start extraction process:\n{e}")
            return

        # Start background reader thread
        threading.Thread(target=self._stream_output, daemon=True).start()

    def _launch_login(self) -> None:
        """Launch interactive login helper in a separate process."""
        if self._is_running:
            messagebox.showinfo(
                "Pipeline Active", "Please wait for or stop the active run before logging in."
            )
            return

        self._set_running_state(True)
        self.status_var.set("Login Window Active...")
        self.status_display.config(foreground="#dcdcaa")

        self._log_line("\n" + "=" * 65 + "\n", tag="dim")
        self._log_line("Opening LinkedIn interactive login window...\n", tag="info")

        cmd = [sys.executable, "-u", "-m", "profile_extractor", "login"]

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            self._set_running_state(False)
            messagebox.showerror("Login Error", f"Could not launch login window:\n{e}")
            return

        threading.Thread(target=self._stream_output, daemon=True).start()

    def _stream_output(self) -> None:
        """Continuously read process standard output and stream to GUI log."""
        proc = self.process
        if not proc or not proc.stdout:
            return

        try:
            for raw_line in iter(proc.stdout.readline, ""):
                if not raw_line:
                    break
                # Schedule safe UI update on main Tkinter thread
                self.root.after(0, self._log_line, raw_line)
        except Exception as e:
            self.root.after(0, self._log_line, f"\n[Stream Error]: {e}\n", "error")
        finally:
            if proc.stdout:
                proc.stdout.close()
            rc = proc.wait()
            self.root.after(0, self._on_process_finished, rc)

    def _on_process_finished(self, returncode: int) -> None:
        """Handle completion of the background extraction process.

        Args:
            returncode: Integer exit code from the child process.
        """
        self._set_running_state(False)
        self.process = None

        if returncode == 0:
            self.status_var.set("Completed Successfully")
            self.status_display.config(foreground="#4ec9b0")
            self._log_line("\n[SUCCESS] Extraction run finished cleanly.\n", tag="success")
        else:
            self.status_var.set(f"Finished (Exit Code {returncode})")
            self.status_display.config(foreground="#f44747")
            msg = f"\n[NOTICE] Process exited with return code {returncode}.\n"
            self._log_line(msg, tag="warning")

    def _stop_extraction(self) -> None:
        """Forcefully terminate the running extraction subprocess and child browsers."""
        if not self.process:
            return

        try:
            pid = self.process.pid
            self._log_line(f"\n[INFO] Stopping process tree (PID {pid})...\n", tag="warning")
            if sys.platform == "win32":
                # Cleanly terminate entire process tree (Python + Playwright Chromium)
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                self.process.terminate()
        except Exception as e:
            self._log_line(f"[WARNING] Could not kill process tree: {e}\n", tag="warning")

        self.status_var.set("Stopped by User")
        self.status_display.config(foreground="#ce9178")
        self._set_running_state(False)
        self.process = None

    def _set_running_state(self, running: bool) -> None:
        """Toggle UI button enabled/disabled states based on execution status.

        Args:
            running: True if a background worker process is currently running.
        """
        self._is_running = running
        if running:
            self.start_btn.config(state=tk.DISABLED)
            self.login_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
        else:
            self.start_btn.config(state=tk.NORMAL)
            self.login_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)

    def _on_close(self) -> None:
        """Handle window close event, ensuring child processes are killed."""
        if self._is_running and self.process:
            if messagebox.askyesno(
                "Extraction in Progress",
                "An extraction job is currently running.\nDo you want to terminate it and exit?",
            ):
                self._stop_extraction()
                self.root.destroy()
        else:
            self.root.destroy()


def launch_gui() -> int:
    """Initialize Tk root and run the main application event loop.

    Returns:
        Exit code 0.
    """
    root = tk.Tk()
    # Set default app font family to Segoe UI on Windows
    root.option_add("*Font", "SegoeUI 9")
    _ = ExtractorApp(root)
    root.mainloop()
    return 0


def main() -> int:
    """CLI script entry point for `python -m profile_extractor.gui`."""
    return launch_gui()


if __name__ == "__main__":
    sys.exit(main())
