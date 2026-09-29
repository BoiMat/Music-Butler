#!/usr/bin/env python3
"""Local Ollama agent for Music Butler.

A small, stdlib-only chat loop that loads the Music Butler skill
(skills/music-butler/SKILL.md) as its system prompt. It is built for small
(~8 B) local models: butler_lookup searches YouTube for "artist title" texts
(via yt-dlp in the downloader container), and butler_add takes a flat list of
songs (one or many), writes the request file and runs `scripts/butler.sh add`,
which validates and imports. The model sees only compact results.

Requires a running Ollama server (default http://localhost:11434) and a
pulled model. Configuration is via environment variables:

    OLLAMA_MODEL      model to use                  (default: qwen2.5:7b)
    OLLAMA_BASE_URL   Ollama server URL             (default: http://localhost:11434)
    OLLAMA_NUM_CTX    context window in tokens      (default: 16384; Ollama's own
                      default is too small for the skill plus tool schemas)
    BUTLER_TIMEOUT    timeout in seconds for Ollama replies and short butler
                      commands (status/drives)      (default: 3600)

Imports and backups have no agent-side timeout: the importer bounds every
download/encode step itself, and killing a half-finished run from here would
only orphan its container.

Run:  python3 scripts/agent.py
"""

from __future__ import annotations

import json
import os
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
NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))

SKILL_PATH = os.path.join(REPO, "skills", "music-butler", "SKILL.md")
REQUESTS_DIR = os.path.join(REPO, "requests")
MAX_TOOL_ROUNDS = 8
MAX_RESULT_CHARS = 3000
LOOKUP_TIMEOUT = min(TIMEOUT, 900)  # searches only read metadata; bounded

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "butler_lookup",
            "description": (
                "Find sources before adding. Searches YouTube for each text in 'search' "
                "(e.g. 'Daft Punk Get Lucky') and returns title, channel, duration and url "
                "per result; for 'Artist - Topic' channels also clean artist/track names. "
                "'urls' returns the same details for links the owner gave. Downloads nothing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search": {"type": "array", "items": {"type": "string"},
                               "description": "One search text per song: artist and title."},
                    "urls": {"type": "array", "items": {"type": "string"},
                             "description": "Links the owner gave, to read their title and length."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "butler_add",
            "description": (
                "Add songs to the library: validates, downloads, tags and imports them, "
                "one after another. Put EVERY song the owner asked for in ONE call. "
                "Can take many minutes. Never call it again for songs it already added."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "songs": {
                        "type": "array",
                        "description": "One entry per song. Album tracks: same artist and album, in track order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "artist": {"type": "string"},
                                "title": {"type": "string"},
                                "url": {"type": "string", "description": "Exact HTTPS URL the owner gave."},
                                "album": {"type": "string", "description": "Optional. Leave out for a single."},
                                "artwork_url": {"type": "string", "description": "Optional cover image URL."},
                            },
                            "required": ["artist", "title", "url"],
                        },
                    },
                    "format": {"type": "string", "enum": ["mp3", "flac"], "description": "Optional, default mp3."},
                    "quality": {
                        "type": "string",
                        "description": "Optional. mp3: 128/192/256/320 (default 192); flac: 0-8 (default 5).",
                    },
                },
                "required": ["songs"],
            },
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

SONG_KEYS = {"artist", "title", "url", "album", "artwork_url"}


class ToolError(Exception):
    """A bad tool call; the message goes back to the model, not a traceback."""


def system_prompt() -> str:
    with open(SKILL_PATH, encoding="utf-8") as fh:
        skill = fh.read()
    return (
        "You are the Music Butler agent on the owner's own machine.\n"
        "Differences from the skill below:\n"
        "- You have no other web access. Where the skill says to write a lookup "
        "file and run `butler.sh lookup`, call butler_lookup with the same search/urls "
        "lists. Use only URLs that butler_lookup returned or the owner gave. Never "
        "guess or build a URL.\n"
        "- Where the skill says to write a request file and run `butler.sh add`, call "
        "butler_add with the same songs list. It does both.\n"
        "- `butler.sh status`, `drives`, `backup` are the tools butler_status, "
        "butler_drives, butler_backup.\n\n"
        "=== SKILL ===\n" + skill
    )


def ollama_chat(messages: list[dict]) -> dict:
    # Low temperature: small models call tools more reliably. num_ctx: see module docstring.
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS, "stream": False,
                       "options": {"num_ctx": NUM_CTX, "temperature": 0.2}}).encode("utf-8")
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
            # communicate(), not wait(): keep draining stdout/stderr so a child that
            # logs while shutting down cannot block on a full pipe.
            proc.communicate(timeout=grace)
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


def songs_request(arguments: dict) -> dict:
    """Build a {"songs": [...]} request from model arguments.

    Small models add empty or unknown fields; those are dropped here. Everything
    else is left to music_butler.py, which is the real validator.
    """
    songs = arguments.get("songs")
    if isinstance(songs, dict):  # one song sent without the list around it
        songs = [songs]
    if not isinstance(songs, list) or not songs:
        raise ToolError("'songs' must be a list with at least one song")
    request: dict = {"songs": [
        {k: v for k, v in s.items() if k in SONG_KEYS and v not in (None, "")} if isinstance(s, dict) else s
        for s in songs
    ]}
    for key in ("format", "quality"):
        if arguments.get(key) not in (None, ""):
            request[key] = str(arguments[key])
    return request


def lookup_request(arguments: dict) -> dict:
    request = {}
    for key in ("search", "urls"):
        value = arguments.get(key)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            value = [v for v in value if v not in (None, "")]
            if value:
                request[key] = value
    if not request:
        raise ToolError("give 'search' (list of 'artist title' texts) and/or 'urls'")
    return request


def write_request(request: object, kind: str = "approved") -> str:
    if not isinstance(request, dict):
        raise ToolError("request must be a JSON object")
    os.makedirs(REQUESTS_DIR, exist_ok=True)
    rid = uuid.uuid4().hex
    path = os.path.join(REQUESTS_DIR, "%s-%s.json" % (kind, rid))
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(request, fh, indent=2, ensure_ascii=False)
    return rid


def compact(code: int, out: str) -> str:
    """What the model needs from `butler.sh add`: the summary line, or the errors.

    The full output (hashes, paths, one line per track) would crowd a small
    model's context and bury the result.
    """
    events = []
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    final = [e for e in events if e.get("event") in ("summary", "lookup")]
    if final:
        return "exit %d\n%s" % (code, json.dumps(final[-1], ensure_ascii=False))
    errors = [e for e in events if e.get("event") == "error"]
    if errors:
        # Validation failed: nothing was downloaded. `song` is the 1-based song number.
        return "exit %d (nothing was done)\n%s" % (
            code, "\n".join(json.dumps(e, ensure_ascii=False) for e in errors))
    return "exit %d\n%s" % (code, out[-MAX_RESULT_CHARS:] or "(no output)")


def add_songs(request: dict) -> str:
    rid = write_request(request)
    code, out = run_butler(["add", rid], None)
    return compact(code, out)


def call_tool(name: str, arguments: object) -> str:
    if isinstance(arguments, str):  # some models send JSON text instead of an object
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return "error: tool arguments are not valid JSON"
    if not isinstance(arguments, dict):
        return "error: tool arguments must be an object"
    try:
        if name == "butler_lookup":
            rid = write_request(lookup_request(arguments), "lookup")
            return compact(*run_butler(["lookup", rid], LOOKUP_TIMEOUT))
        if name == "butler_add":
            return add_songs(songs_request(arguments))
        if name == "butler_status":
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
    # Keep the end: backup success is its last line ("Restore test passed").
    return "exit %d\n%s" % (code, out[-MAX_RESULT_CHARS:] or "(no output)")


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
            print(result)
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
                # Escape hatch: the owner writes the request JSON; it is added directly
                # and the model only explains the result.
                request = read_pasted_json()
                if request is None:
                    continue
                try:
                    result = add_songs(request)
                except (ToolError, OSError) as exc:
                    print("  %s" % exc)
                    continue
                print(result)
                user = "I added a request myself. Report this result to me:\n" + result
            messages.append({"role": "user", "content": user})
            respond(messages)
        except KeyboardInterrupt:
            # run_butler already stopped any running command.
            print("\n(interrupted; run 'status' before retrying an import)\n")


if __name__ == "__main__":
    sys.exit(main())
