#!/usr/bin/python3
"""Run the upstream verifier's package install in its isolated client Python.

The upstream command has --system. uv deliberately skips venv interpreters for
that flag, so PATH alone is insufficient. Translate only that install option;
the real uv still installs the exact requested package and reports its real exit.
"""
import os
import sys

binary = "/opt/forgeflow/client-venv/bin/uv"
arguments = sys.argv[1:]
if arguments[:2] == ["pip", "install"]:
    arguments = [argument for argument in arguments if argument != "--system"]
    arguments[2:2] = ["--python", "/opt/forgeflow/client-venv/bin/python"]
os.execv(binary, [binary, *arguments])
