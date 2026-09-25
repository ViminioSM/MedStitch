"""Post-process runner for external applications."""

import os
import shlex
import shutil
import subprocess
import threading
from typing import Callable, List, Optional

from core.models.work_directory import WorkDirectory
from core.services.global_logger import logFunc


def _find_executable(app: str) -> Optional[str]:
    """Find executable path, returns None if not found."""
    if not app:
        return None
    if os.path.isabs(app) and os.path.isfile(app):
        return app
    return shutil.which(app)


def _build_popen_kwargs() -> dict:
    """Return platform-specific kwargs to suppress console windows on Windows."""
    if os.name != "nt":
        return {}
    kwargs: dict = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    kwargs["startupinfo"] = startupinfo
    return kwargs


class AsyncPostProcess:
    """Handle to a running post-process, allowing fire-and-forget + wait."""

    def __init__(
        self,
        proc: subprocess.Popen,
        processed_path: str,
        command: list[str],
    ) -> None:
        self._proc = proc
        self._processed_path = processed_path
        self._command = command
        self._stdout_thread: Optional[threading.Thread] = None
        self._stdout_lines: list[str] = []

    def _drain_stdout(self, console_func: Callable[[str], None]) -> None:
        stdout_pipe = self._proc.stdout
        if stdout_pipe is None:
            return
        try:
            for line in stdout_pipe:
                line_str = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
                self._stdout_lines.append(line_str)
                console_func(line_str)
        except Exception:
            pass

    def start(self, console_func: Callable[[str], None] = print) -> None:
        """Start draining stdout in a background thread."""
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, args=(console_func,), daemon=True
        )
        self._stdout_thread.start()

    def wait(self, console_func: Callable[[str], None] = print) -> None:
        """Wait for process to complete, logging output."""
        try:
            if self._stdout_thread is not None:
                self._stdout_thread.join()
            return_code = self._proc.wait()
            if return_code:
                raise subprocess.CalledProcessError(return_code, self._command)
            console_func("\nPost process finished successfully!\n")
        finally:
            if self._proc.stdout is not None:
                try:
                    self._proc.stdout.close()
                except Exception:
                    pass
            if self._proc.poll() is None:
                self._proc.kill()
                self._proc.wait()

    @property
    def done(self) -> bool:
        return self._proc.poll() is not None

    @property
    def stdout_text(self) -> str:
        return "".join(self._stdout_lines)


class PostProcessRunner:
    """Executes external post-processing applications on output directories."""

    def __init__(self) -> None:
        self._pending: list[AsyncPostProcess] = []

    def run(
        self,
        workdirectory: WorkDirectory,
        *,
        postprocess_app: str = "",
        postprocess_args: str = "",
        console_func: Callable[[str], None] = print,
    ) -> None:
        """Run postprocess synchronously (blocks until complete)."""
        handle = self.run_async(
            workdirectory,
            postprocess_app=postprocess_app,
            postprocess_args=postprocess_args,
            console_func=console_func,
        )
        if handle is not None:
            handle.wait(console_func)

    def run_async(
        self,
        workdirectory: WorkDirectory,
        *,
        postprocess_app: str = "",
        postprocess_args: str = "",
        console_func: Callable[[str], None] = print,
    ) -> Optional[AsyncPostProcess]:
        """Launch postprocess asynchronously. Returns handle for later wait."""
        if not postprocess_app:
            raise ValueError("Post process application is required but not configured.")

        executable_path = _find_executable(postprocess_app)
        if executable_path is None:
            raise FileNotFoundError(
                f"Post process application '{postprocess_app}' not found. "
                f"Please verify the application is installed and accessible."
            )

        try:
            extra_args = shlex.split(postprocess_args, posix=False)
        except ValueError:
            extra_args = [postprocess_args] if postprocess_args else []

        token_map = {
            "[stitched]": workdirectory.output_path,
            "[processed]": workdirectory.postprocess_path,
        }

        resolved_args: list[str] = []
        for token in extra_args:
            if len(token) >= 2 and token[0] == token[-1] == '"':
                token = token[1:-1]
            resolved_args.append(token_map.get(token, token))

        command = [executable_path, *resolved_args]
        console_func(f"Executing post process: {' '.join(command)}\n")

        return self._execute_async(
            workdirectory.postprocess_path, command, console_func
        )

    def run_async_bg(
        self,
        workdirectory: WorkDirectory,
        *,
        postprocess_app: str = "",
        postprocess_args: str = "",
        console_func: Callable[[str], None] = print,
    ) -> None:
        """Fire-and-forget: launch postprocess, track internally, don't block."""
        handle = self.run_async(
            workdirectory,
            postprocess_app=postprocess_app,
            postprocess_args=postprocess_args,
            console_func=console_func,
        )
        if handle is not None:
            self._pending.append(handle)

    def wait_all(self, console_func: Callable[[str], None] = print) -> None:
        """Wait for all pending background postprocesses to complete."""
        remaining = [h for h in self._pending if not h.done]
        if not remaining:
            return
        console_func(f"Waiting for {len(remaining)} post-process job(s) to finish...\n")
        for handle in self._pending:
            if not handle.done:
                handle.wait(console_func)
        self._pending.clear()

    @logFunc(inclass=True)
    def _execute_async(
        self,
        processed_path: str,
        command: list[str],
        console_func: Callable[[str], None],
    ) -> AsyncPostProcess:
        os.makedirs(processed_path, exist_ok=True)

        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            universal_newlines=True,
            shell=False,
            **_build_popen_kwargs(),
        )
        console_func("Post process started!\n")

        handle = AsyncPostProcess(proc, processed_path, command)
        handle.start(console_func)
        return handle
