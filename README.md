# Tutor: subtitle MCP for English content

This is a **public code slice** of my personal AI tutor. The full private
application runs two Telegram study flows, uses Codex SDK/App Server for agent
sessions, stores learning state in Markdown, schedules practice loops, and
tracks spaced repetition with FSRS/SQLite. This repository publishes the
transcript MCP server and a generic English-content [episode workflow](prompts/episode.md).
It contains no learner history, credentials, bot tokens, or private deployment
configuration.

## Why this piece matters

An English learner can send a video before watching it. The tutor obtains the
actual subtitle track, selects a short set of useful expressions, and prepares
them for recognition during the video. The workflow has a hard gate: **no
verified transcript, no vocabulary list**. That prevents a plausible-looking
list based on the model's memory of a show instead of the episode being watched.

## MCP tools

| Tool | Implementation | Result |
| --- | --- | --- |
| `fetch_subtitles(url, lang)` | `yt-dlp --skip-download` gets author or auto captions; VTT/SRT parsing strips timestamps and adjacent repeated lines | Plain text from the selected subtitle track. Defaults to English. |
| `fetch_page(url)` | `httpx` fetches a transcript page; an HTML parser keeps block boundaries while removing markup | Plain text for the tutor to verify against the requested episode. |

The tools use stdio MCP through a small provider-neutral schema bridge. The
server does not download video or transcribe audio. It needs an available
subtitle track or transcript page. It clips oversized responses and returns
tool errors as text that an agent can handle.

```text
Video URL / transcript page
    -> transcript MCP (fetch_subtitles / fetch_page)
    -> verify the requested content
    -> select 12-20 real expressions
    -> pre-viewing Telegram note in the private application
    -> learner feedback -> spaced repetition
```

## Run locally

Python 3.11+ and `uv` are required. Install dependencies and start the MCP
server from this repository:

```bash
uv sync
uv run tutor-subtitles-mcp
```

The command speaks MCP over stdin/stdout and waits for an MCP client. For a
client that accepts a command and arguments, use the repository's virtualenv:

```json
{
  "command": "/absolute/path/to/tutor-public/.venv/bin/python",
  "args": ["-m", "tutor_public.server"]
}
```

Keep the server local and pass only video or page URLs you trust. It accepts
HTTP(S) URLs and is not hardened as a public web-fetch service. No model API key
is needed for the MCP server itself; an agent client is needed to use the tools.

## Scope of this public version

The published code includes the transcript MCP, a generic episode prompt, and
packaging to run the server. The full Tutor runtime, personal Markdown memory,
Google/Telegram credentials, SRS store, and production deployment remain
private. This is a focused implementation sample, not a hosted tutoring app.
