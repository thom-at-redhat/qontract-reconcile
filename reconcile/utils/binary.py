import re
import shutil
import subprocess
from functools import wraps


def binary(binaries=None):
    """Check that a binary exists before execution."""
    if binaries is None:
        binaries = []

    def deco_binary(f):
        @wraps(f)
        def f_binary(*args, **kwargs):
            for b in binaries:
                if not shutil.which(b):
                    raise Exception(
                        f"Aborting: Could not find binary: {b}. "
                        + f"Hint: https://command-not-found.com/{b}"
                    )
            f(*args, **kwargs)

        return f_binary

    return deco_binary


def binary_version(binary, version_args, search_regex, expected_versions):
    """Check that a binary exists and is a desired version"""

    def deco_binary_version(f):
        @wraps(f)
        def f_binary_version(*args, **kwargs):
            regex = re.compile(search_regex)

            cmd = [binary]
            cmd.extend(version_args)
            try:
                result = subprocess.run(cmd, capture_output=True, check=True)
            except subprocess.CalledProcessError as e:
                msg = (
                    f"Could not execute binary '{binary}' for binary version check: {e}"
                )
                raise Exception(msg) from e

            found = False
            match = None
            for line in result.stdout.splitlines():
                match = regex.search(line.decode("utf-8"))
                if match is not None:
                    found = True
                    break

            if not found:
                raise Exception(
                    f"Could not find version for binary '{binary}' via regex "
                    f"for binary version check: "
                    f"regex did not match: '{search_regex}'"
                )

            version = match.group(1)
            if version not in expected_versions:
                raise Exception(
                    f"Binary version check for binary {binary} failed! "
                    f"Expected: {expected_versions}, found: {version}"
                )

            f(*args, **kwargs)

        return f_binary_version

    return deco_binary_version
