#!/usr/bin/env python3
"""Local Ollama agent for Music Butler.

A small, stdlib-only chat loop that loads the Music Butler skill
(skills/music-butler/SKILL.md) as its system prompt and exposes the five
``scripts/butler.sh`` commands, plus a structured request writer, as tools.
Give it the exact source URL(s) and metadata in plain chat; it writes the
request file, validates it, and imports it. It has no web access, so it cannot
search for sources itself.

Requires a running Ollama server (default http://localhost:11434) and a
pulled model. Configuration is via environment variables:

    OLLAMA_MODEL      model to use                  (default: qwen2.5:7b)
    OLLAMA_BASE_URL   Ollama server URL             (default: http://localhost:11434)
    BUTLER_TIMEOUT    timeout in seconds for Ollama replies and short butler
                      commands (validate/status/drives)  (default: 3600)

Imports and backups have no agent-side timeout: the importer bounds every
download/encode step itself, and killing a half-finished run from here would
only orphan its container.

Run:  python3 scripts/agent.py
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
TIMEOUT = int(os.environ.get("BUTLER_TIMEOUT", "3600"))

SKILL_PATH = os.path.join(REPO, "skills", "music-butler", "SKILL.md")
REQUESTS_DIR = os.path.join(REPO, "requests")
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_TOOL_ROUNDS = 6

ID_PARAMS = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "32 lowercase hex request id returned by butler_write_request.",
            "pattern": "^[0-9a-f]{32}$",
        }
    },
    "required": ["id"],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "butler_write_request",
            "description": (
                "Write an approved request file requests/approved-<id>.json and "
                "return its id. Use only URLs the owner gave you or confirmed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["song", "album"]},
                    "artist": {"type": "string"},
                    "album": {"type": "string", "description": "Release or single name."},
                    "format": {"type": "string", "enum": ["mp3", "flac"]},
                    "quality": {
                        "type": "string",
                        "description": "mp3: 128/192/256/320 (default 192); flac: 0-8 (default 5).",
                    },
                    "tracks": {
                        "type": "array",
                        "description": "Exactly one track for a song; all tracks in order for an album.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string", "description": "Exact HTTPS audio URL."},
                                "artwork_url": {"type": "string", "description": "Optional image URL."},
                            },
                            "required": ["title", "url"],
                        },
                    },
                },
                "required": ["kind", "artist", "album", "tracks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "butler_validate",
            "description": (
                "Offline-validate an approved request file "
                "requests/approved-<id>.json without downloading anything."
            ),
            "parameters": ID_PARAMS,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "butler_import",
            "description": (
                "Import an approved request file into the library. This is the "
                "long-running download; it may take minutes. Never call it twice "
                "for the same id."
            ),
            "parameters": ID_PARAMS,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "butler_status",
            "description": "Navidrome health and the list of releases in the library.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "butler_drives",
            "description": "List mounted external drives (macOS /Volumes, Linux /media).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "butler_backup",
            "description": "Encrypted restic backup; optionally to a named mounted drive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "drive": {
                        "type": "string",
                        "description": "Mounted drive name (optional).",
                    }
                }
            },
        },
    },
]

REQUEST_KEYS = {"kind", "artist", "album", "format", "quality", "tracks"}
TRACK_KEYS = {"title", "url", "artwork_url"}


class ToolError(Exception):
    """A bad tool call; the message goes back to the model, not a traceback."""


def system_prompt() -> str:
    with open(SKILL_PATH, encoding="utf-8") as fh:
        skill = fh.read()
    return (
        "You are the Music Butler agent. You help the owner download music "
        "into their private library. You run on the owner's own machine; you "
        "are not a cloud service. Follow the skill below exactly. The skill "
        "describes writing a request file and running `scripts/butler.sh "
        "<command>`; you instead call the tools butler_write_request, "
        "butler_validate, butler_import, butler_status, butler_drives and "
        "butler_backup.\n"
        "You have NO web access and cannot search. To add music you need an "
        "exact HTTPS audio URL on an allowlisted host from the owner. If they "
        "did not give one, ask for it; never guess or construct a URL. Then "
        "call butler_write_request, then butler_validate with the returned id, "
        "then butler_import.\n\n"
        "=== SKILL ===\n" + skill
    )


def ollama_chat(messages: list[dict]) -> dict:
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS,
                       "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + "/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Ollama answers e.g. 404 {"error": "model 'x' not found"} when the server is up.
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except (ValueError, AttributeError, OSError):
            detail = ""
        raise RuntimeError("Ollama returned HTTP %d: %s (try `ollama pull %s`?)"
                           % (exc.code, detail or exc.reason, MODEL)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Cannot reach Ollama at %s (%s). Is `ollama serve` running?"
            % (BASE_URL, exc.reason)
        ) from exc
    except (TimeoutError, ValueError) as exc:
        raise RuntimeError("No usable reply from Ollama: %s" % exc) from exc
    if not isinstance(reply.get("message"), dict):
        raise RuntimeError("Unexpected Ollama reply: %s" % reply.get("error", "no message"))
    return reply


def stop_group(proc: subprocess.Popen) -> None:
    """Stop butler.sh and the docker compose client it started.

    SIGTERM first so `docker compose run` can stop its container and the importer
    can roll back staging; SIGKILL only if that does not finish.
    """
    for sig, grace in ((signal.SIGTERM, 60), (getattr(signal, "SIGKILL", signal.SIGTERM), 10)):
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def run_butler(args: list[str], timeout: float | None) -> tuple[int, str]:
    cmd = ["bash", os.path.join("scripts", "butler.sh"), *args]
    # Own process group: a timeout or Ctrl+C must reach docker compose, not just bash.
    proc = subprocess.Popen(cmd, cwd=REPO, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        stop_group(proc)
        raise
    out = (stdout or "") + (("\n[stderr]\n" + stderr) if stderr else "")
    return proc.returncode, out.strip()


def request_id(arguments: dict) -> str:
    rid = arguments.get("id")
    if not isinstance(rid, str) or not REQUEST_ID.match(rid):
        raise ToolError("argument 'id' must be the 32 lowercase hex id from butler_write_request")
    return rid


def write_request(request: object) -> str:
    """Store a request exactly as given; music_butler.py remains the validator."""
    if not isinstance(request, dict):
        raise ToolError("request must be a JSON object")
    request = {k: v for k, v in request.items() if k in REQUEST_KEYS and v not in (None, "")}
    tracks = request.get("tracks")
    if isinstance(tracks, list):
        request["tracks"] = [
            {k: v for k, v in t.items() if k in TRACK_KEYS and v not in (None, "")}
            if isinstance(t, dict) else t
            for t in tracks
        ]
    os.makedirs(REQUESTS_DIR, exist_ok=True)
    rid = uuid.uuid4().hex
    path = os.path.join(REQUESTS_DIR, "approved-%s.json" % rid)
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(request, fh, indent=2, ensure_ascii=False)
    return rid


def call_tool(name: str, arguments: object) -> str:
    if isinstance(arguments, str):  # some models send JSON text instead of an object
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return "error: tool arguments are not valid JSON"
    if not isinstance(arguments, dict):
        return "error: tool arguments must be an object"
    try:
        if name == "butler_write_request":
            rid = write_request(arguments)
            return "wrote requests/approved-%s.json\nid: %s\nNext: butler_validate with this id." % (rid, rid)
        if name == "butler_validate":
            code, out = run_butler(["validate", request_id(arguments)], TIMEOUT)
        elif name == "butler_import":
            code, out = run_butler(["import", request_id(arguments)], None)
        elif name == "butler_status":
            code, out = run_butler(["status"], TIMEOUT)
        elif name == "butler_drives":
            code, out = run_butler(["drives"], TIMEOUT)
        elif name == "butler_backup":
            drive = arguments.get("drive")
            if drive is not None and not isinstance(drive, str):
                raise ToolError("argument 'drive' must be a string")
            code, out = run_butler(["backup", drive] if drive else ["backup"], None)
        else:
            return "Unknown tool: %s" % name
    except ToolError as exc:
        return "error: %s" % exc
    except subprocess.TimeoutExpired:
        return ("Tool %s timed out after %d seconds and was stopped. Run butler_status "
                "before trying again." % (name, TIMEOUT))
    except OSError as exc:
        return "error: could not run scripts/butler.sh (%s)" % exc
    return "exit %d\n%s" % (code, out or "(no output)")


def respond(messages: list[dict]) -> None:
    """Let the model answer, running tools for up to MAX_TOOL_ROUNDS rounds."""
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            reply = ollama_chat(messages)
        except RuntimeError as exc:
            print("\n[error] %s\n" % exc)
            return
        msg = reply["message"]
        messages.append(msg)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            print("\nbutler> %s\n" % msg.get("content", ""))
            return
        for tc in tool_calls:
            fn = (tc.get("function") or {}).get("name", "")
            args = (tc.get("function") or {}).get("arguments") or {}
            print("\nbutler> calling %s(%s) ..." % (fn, args))
            result = call_tool(fn, args)
            print(result[:4000])
            messages.append({"role": "tool", "content": result, "tool_name": fn})
    print("(stopped after %d tool rounds; say 'continue' to let it go on)" % MAX_TOOL_ROUNDS)


def read_pasted_json() -> object | None:
    """Read lines until they form one JSON value; a lone '.' cancels."""
    print("Paste the JSON request (a line with just '.' cancels):")
    lines: list[str] = []
    while True:
        try:
            line = input("  ")
        except EOFError:
            break
        if line.strip() == "." and not lines:
            return None
        if line.strip() == ".":
            break
        lines.append(line)
        try:
            return json.loads("\n".join(lines))
        except ValueError:
            continue  # incomplete so far, keep reading (blank lines are fine)
    try:
        return json.loads("\n".join(lines))
    except ValueError as exc:
        print("  Not valid JSON: %s" % exc)
        return None


def main() -> int:
    print("Music Butler agent — model: %s @ %s" % (MODEL, BASE_URL))
    print("Type 'exit' to quit, 'write-request' to paste a request JSON yourself.\n")
    messages: list[dict] = [{"role": "system", "content": system_prompt()}]
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user:
            continue
        if user.lower() in {"exit", "quit", "bye"}:
            return 0
        try:
            if user.lower() == "write-request":
                # Escape hatch: the owner writes the request; the model validates/imports it.
                request = read_pasted_json()
                if request is None:
                    continue
                try:
                    rid = write_request(request)
                except ToolError as exc:
                    print("  %s" % exc)
                    continue
                print("  Wrote requests/approved-%s.json" % rid)
                user = ("I have written request id %s. Validate it, and import it "
                        "if it is valid." % rid)
            messages.append({"role": "user", "content": user})
            respond(messages)
        except KeyboardInterrupt:
            # run_butler already stopped any running command.
            print("\n(interrupted; run 'status' before retrying an import)\n")


if __name__ == "__main__":
    sys.exit(main())
