"""In-app account enrollment.

Clauculate does not sign anyone in. It launches Claude Code's own
`auth login` with CLAUDE_CONFIG_DIR pointed at a new folder, relays what that
process says, and passes back the code you paste. The browser step is yours;
no password ever reaches this app.

That does mean the child process writes a credential store. Clauculate's own
process still never writes inside a Claude directory, and readonly_guard still
enforces that. The distinction is real and the README states it.

Observed behaviour of `claude auth login` (2.1.239, spawned with pipes):

    Opening browser to sign in…
    If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?...
    Paste code here if prompted > Login successful.

and on a bad code:

    Paste code here if prompted > Invalid code. Please make sure the full code
    was copied.

It needs no TTY, and it creates CLAUDE_CONFIG_DIR when the folder is absent.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

IS_WINDOWS = sys.platform.startswith("win")

# Windows: PowerShell's execution policy blocks the .ps1 shim on a default
# install, so the .cmd is the reliable entry point.
CLI_CANDIDATES = (
    [os.path.expandvars(r"%APPDATA%\npm\claude.cmd"), "claude.cmd", "claude"]
    if IS_WINDOWS else ["claude"]
)

# A private window is the only reliable way to choose WHICH account signs in.
# --email is only a login_hint: the browser's cached session overrides it, which
# is how one account ends up enrolled several times.
PRIVATE_BROWSERS = (
    (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", "--incognito"),
    (r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", "--incognito"),
    (r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe", "--incognito"),
    (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", "--inprivate"),
    (r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe", "--inprivate"),
    (r"%ProgramFiles%\Mozilla Firefox\firefox.exe", "-private-window"),
)

MAC_PRIVATE = (
    ("Google Chrome", ["--args", "--incognito"]),
    ("Microsoft Edge", ["--args", "--inprivate"]),
)


def open_private(url: str) -> str | None:
    """Open a URL in a private window. Returns the browser name, or None.

    Falls back to nothing rather than a normal window: a normal window is what
    causes the wrong account to be granted, so silently doing that would be
    worse than telling the user we could not.
    """
    if IS_WINDOWS:
        for raw, flag in PRIVATE_BROWSERS:
            path = os.path.expandvars(raw)
            if os.path.exists(path):
                try:
                    subprocess.Popen([path, flag, url])
                    return os.path.basename(path)
                except OSError:
                    continue
        return None

    if sys.platform == "darwin":
        for app, args in MAC_PRIVATE:
            try:
                result = subprocess.run(
                    ["open", "-na", app] + args + [url],
                    capture_output=True, timeout=10,
                )
                if result.returncode == 0:
                    return app
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    for binary, flag in (("google-chrome", "--incognito"),
                         ("chromium", "--incognito"),
                         ("firefox", "-private-window")):
        import shutil
        if shutil.which(binary):
            try:
                subprocess.Popen([binary, flag, url])
                return binary
            except OSError:
                continue
    return None


URL_PATTERN = re.compile(r"https://\S+oauth/authorize\S*")

# How long to wait for the whole flow before giving up on it.
LOGIN_TIMEOUT = 300.0


class State:
    IDLE = "idle"
    LAUNCHING = "launching"
    BROWSER = "browser"          # waiting for the user to approve
    CODE = "code"                # a code has to be pasted back
    FINISHING = "finishing"      # login reported success, credentials landing
    VERIFYING = "verifying"      # asking the API who we actually got
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


def find_cli() -> str | None:
    """The claude executable, or None if it is not installed."""
    import shutil

    for candidate in CLI_CANDIDATES:
        if os.path.isabs(candidate):
            if os.path.exists(candidate):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


@dataclass
class LoginResult:
    ok: bool
    message: str
    config_dir: Path | None = None
    profile: Any = None
    duplicate_of: str | None = None
    output: list[str] = field(default_factory=list)


class LoginSession:
    """One `claude auth login` run, driven from the UI.

    Every state change is reported through `on_event(state, detail)`, which the
    caller must marshal onto the Tk thread.
    """

    def __init__(
        self,
        config_dir: Path,
        email: str,
        on_event: Callable[[str, str], None],
        user_agent: str = "",
        known_identities: dict[str, str] | None = None,
    ):
        self.config_dir = Path(config_dir)
        self.email = email.strip()
        self.on_event = on_event
        self.user_agent = user_agent
        self.known_identities = known_identities or {}

        self.state = State.IDLE
        self.url: str | None = None
        self.output: list[str] = []
        self.result: LoginResult | None = None

        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._started = 0.0

    # ------------------------------------------------------------------ run

    def start(self) -> None:
        cli = find_cli()
        if cli is None:
            self._finish(LoginResult(
                False,
                "Claude Code is not installed, or `claude` is not on PATH. "
                "Install it first, then try again.",
            ))
            return

        self.config_dir.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        # The child writes here. Clauculate itself never does.
        env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)

        command = [cli, "auth", "login"]
        if self.email:
            command += ["--email", self.email]

        self._set_state(State.LAUNCHING, "starting sign-in")
        try:
            self._proc = subprocess.Popen(
                command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if IS_WINDOWS else 0,
            )
        except OSError as exc:
            self._finish(LoginResult(False, "Could not start sign-in: %s" % exc))
            return

        self._started = time.time()
        threading.Thread(target=self._read, name="login-reader", daemon=True).start()
        threading.Thread(target=self._watchdog, name="login-timeout", daemon=True).start()

    def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for chunk in self._proc.stdout:
                line = chunk.rstrip("\n")
                if line:
                    self.output.append(line)
                self._interpret(line)
        except Exception as exc:
            self.output.append("<reader error: %s>" % exc)
        finally:
            self._on_exit()

    def _interpret(self, line: str) -> None:
        lowered = line.lower()

        match = URL_PATTERN.search(line)
        if match:
            self.url = match.group(0)
            self._set_state(State.BROWSER, "approve the sign-in in your browser")
            return

        # The prompt and the outcome often arrive on one line, so check the
        # decisive strings before the prompt itself.
        if "login successful" in lowered:
            self._set_state(State.FINISHING, "signed in, checking which account")
            return
        if "invalid code" in lowered:
            self._set_state(State.CODE, "that code was not accepted, try again")
            return
        if "paste code" in lowered:
            self._set_state(State.CODE, "paste the code from your browser")
            return
        if "already logged in" in lowered:
            self._set_state(State.FINISHING, "this folder was already signed in")

    def submit_code(self, code: str) -> None:
        code = code.strip()
        if not code or self._proc is None or self._proc.poll() is not None:
            return
        try:
            self._proc.stdin.write(code + "\n")
            self._proc.stdin.flush()
            self._set_state(State.FINISHING, "checking the code")
        except (OSError, ValueError) as exc:
            self._finish(LoginResult(False, "Could not send the code: %s" % exc,
                                     output=list(self.output)))

    def cancel(self) -> None:
        self._set_state(State.CANCELLED, "sign-in cancelled")
        self._kill()

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _watchdog(self) -> None:
        while True:
            time.sleep(1.0)
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            if self.state in (State.DONE, State.FAILED, State.CANCELLED):
                return
            if time.time() - self._started > LOGIN_TIMEOUT:
                self._kill()
                self._finish(LoginResult(
                    False,
                    "Sign-in timed out after 5 minutes. Nothing was changed.",
                    output=list(self.output),
                ))
                return

    # -------------------------------------------------------------- finish

    def _on_exit(self) -> None:
        if self.state in (State.CANCELLED, State.FAILED, State.DONE):
            return

        credentials = self.config_dir / ".credentials.json"
        # The file can land a moment after the process reports success.
        for _ in range(20):
            if credentials.exists():
                break
            time.sleep(0.1)

        if not credentials.exists():
            tail = " ".join(self.output[-2:])[:160]
            self._finish(LoginResult(
                False,
                "Sign-in did not complete. " + (tail or "No credentials were written."),
                output=list(self.output),
            ))
            return

        self._set_state(State.VERIFYING, "confirming which account signed in")
        self._verify()

    def _verify(self) -> None:
        """Ask the API who actually signed in, and catch a duplicate."""
        try:
            from .profile import fetch_profile
            profile = fetch_profile(self.config_dir, self.user_agent)
        except Exception as exc:
            self._finish(LoginResult(
                True,
                "Signed in, but the account could not be identified (%s). "
                "Run a scan to check it." % exc,
                config_dir=self.config_dir, output=list(self.output),
            ))
            return

        duplicate = self.known_identities.get(profile.identity)
        if duplicate:
            self._finish(LoginResult(
                False,
                "That signed in as %s, which you already monitor as \"%s\". "
                "Your browser reused its existing session. Sign out of "
                "claude.ai, or use a private window, then try again."
                % (profile.email or "an account", duplicate),
                config_dir=self.config_dir, profile=profile,
                duplicate_of=duplicate, output=list(self.output),
            ))
            return

        if self.email and profile.email and \
                profile.email.lower() != self.email.lower():
            self._finish(LoginResult(
                True,
                "Signed in as %s, not the %s you typed. Your browser chose the "
                "account. Keep it, or remove it and retry from a private window."
                % (profile.email, self.email),
                config_dir=self.config_dir, profile=profile,
                output=list(self.output),
            ))
            return

        self._finish(LoginResult(
            True, "Signed in as %s." % (profile.email or "an account"),
            config_dir=self.config_dir, profile=profile, output=list(self.output),
        ))

    def _finish(self, result: LoginResult) -> None:
        with self._lock:
            if self.result is not None:
                return
            self.result = result
        self.state = State.DONE if result.ok else State.FAILED
        self._emit(self.state, result.message)

    def _set_state(self, state: str, detail: str) -> None:
        if self.state in (State.DONE, State.FAILED, State.CANCELLED):
            return
        self.state = state
        self._emit(state, detail)

    def _emit(self, state: str, detail: str) -> None:
        try:
            self.on_event(state, detail)
        except Exception:
            pass
