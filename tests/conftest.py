import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr


def run_cli(args, env=None):
    """Invoke the CLI in-process and return (exit_code, stdout, stderr)."""
    from sud_cli.cli import main

    saved_env = {}
    if env:
        for key, value in env.items():
            saved_env[key] = os.environ.get(key, None)
            os.environ[key] = value

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    exit_code = None
    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            try:
                exit_code = main(args)
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 0
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()
