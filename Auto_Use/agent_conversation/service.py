# Copyright 2026 Autouse AI — https://github.com/auto-use/Auto-Use
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# If you build on this project, please keep this header and credit
# Autouse AI (https://github.com/auto-use/Auto-Use) in forks and derivative works.
# A small attribution goes a long way toward a healthy open-source
# community — thank you for contributing.

"""ConversationService — permanent, resumable chat memory for the UI path.

This is the ONE place that knows how a chat is persisted, optimized and
replayed. app.py is thin glue:

    sid, prior = conversation.start_or_resume(session_id, task)   # load
    agent = AgentService(..., prior_history=prior)                # trigger
    agent.process_request(task)
    conversation.save_run(sid, agent.assistant_messages,          # save
                          agent.tool_responses, status, message, task)

On-disk layout (data lives INSIDE this package's folder, in sub-folders):

    Auto_Use/agent_conversation/
        index.json                      <- { id: {title, created_at,
                                              updated_at, last_done_message} }
        <session_id>/conversation.json  <- one folder per session: the
                                           optimized, resumable history snapshot

The saved history is the agent's FULL per-step memory: each assistant step with
its thinking / eval / decision / memory / next_goal / action intact, paired 1:1
with the agent's already-compacted tool results, and capped with a single
"terminal note" step carrying a clean done-message so a resumed run always knows
how the previous run ended — on EVERY ending (done, user stop, error, provider
kill, step limit, parse fails). Storing the FULL steps (not a trimmed copy) is
deliberate: the debug download (render_readable) is then the TRUE agent memory,
readable like main.py's conversation_N.txt. Resume token-efficiency is
unaffected — the agent's OWN message builder trims older steps to
decision/memory/next_goal when it sends them to the LLM.

It contains NO system prompt and NO screenshot bytes, so a resumed run never
double-loads the system prompt: the agent's message builder always prepends
exactly one system message and seeds these two lists underneath it. (The debug
log adds the system prompt for readability only, supplied by app.py at export.)
"""

import os
import re
import sys
import json
import time
import shutil
import logging
from pathlib import Path

logger = logging.getLogger("agent_conversation")

# Compiled (Nuitka) binary vs dev run — mirrors the detection in app.py so the
# data root lands next to the executable in a packaged build and inside this
# package in development.
_IS_COMPILED = bool(getattr(sys, "frozen", False)) or ("__compiled__" in globals())


class ConversationService:
    """Owns all conversation-memory persistence + optimization for the UI."""

    # ── storage paths ──────────────────────────────────────────────────────
    def root(self) -> Path:
        """Root folder holding index.json + one sub-folder per session."""
        if _IS_COMPILED:
            base = Path(sys.executable).parent / "Auto_Use" / "agent_conversation"
        else:
            base = Path(__file__).resolve().parent
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _index_file(self) -> Path:
        return self.root() / "index.json"

    def _session_dir(self, session_id) -> Path:
        return self.root() / str(session_id)

    def _session_file(self, session_id) -> Path:
        return self._session_dir(session_id) / "conversation.json"

    def _memory_log_file(self, session_id) -> Path:
        # The TRUE, human-readable memory log (exact payload sent to the model).
        return self._session_dir(session_id) / "memory_log.txt"

    def _exchanges_file(self, session_id) -> Path:
        # The reopen-view transcript: one {task, done_message, status} per run
        # ending, appended by save_run — separate from conversation.json (which
        # is REWRITTEN each run) so the full request/outcome history survives.
        return self._session_dir(session_id) / "exchanges.json"

    # ── index.json (session -> display meta) ───────────────────────────────
    def _read_index(self) -> dict:
        f = self._index_file()
        if f.exists():
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    return data
            except Exception:
                logger.exception("read index.json")
        return {}

    def _write_index(self, index: dict) -> None:
        f = self._index_file()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(index, fh, indent=2)
        except Exception:
            logger.exception("write index.json")

    def _touch_index(self, session_id, *, title=None, last_done_message=None,
                     context_tokens=None, context_cap=None, run_pkg=None) -> dict:
        """Create-or-update one index entry. created_at + title are set ONCE
        (title never overwritten, so the original objective stays the label);
        updated_at + last_done_message + context_tokens/context_cap refresh
        whenever provided.

        context_tokens is the memory bar's LATEST context size (not a sum) and
        context_cap is the fixed memory budget the bar fills toward (MemoryTracker
        MEMORY_CAP = 300k — NOT the model context window) — together they let a
        reopened chat restore the bar to where memory was."""
        index = self._read_index()
        now = int(time.time())
        entry = index.get(str(session_id)) or {
            "title": None, "created_at": now,
            "updated_at": now, "last_done_message": "",
            "context_tokens": 0, "context_cap": 0,
        }
        if title and not entry.get("title"):
            entry["title"] = title
        if last_done_message is not None:
            entry["last_done_message"] = last_done_message
        if context_tokens is not None:
            entry["context_tokens"] = int(context_tokens or 0)
        if context_cap is not None:
            entry["context_cap"] = int(context_cap or 0)
        if run_pkg:
            # Which agent package produced this chat's memory (macOS_use /
            # windows_use / ios_use) — lets a resume detect a mode switch.
            entry["run_pkg"] = run_pkg
        entry["updated_at"] = now
        index[str(session_id)] = entry
        self._write_index(index)
        return entry

    # ── per-session conversation.json ──────────────────────────────────────
    def _read_session(self, session_id):
        """Return the stored optimized history dict, or None on missing/corrupt
        (so the caller can resume the SAME id with a fresh agent context)."""
        f = self._session_file(session_id)
        if f.exists():
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and isinstance(data.get("history"), dict):
                    return data["history"]
            except Exception:
                logger.exception("read session %s", session_id)
        return None

    def _write_session(self, session_id, history: dict) -> None:
        f = self._session_file(session_id)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "w", encoding="utf-8") as fh:
                json.dump({"session_id": str(session_id), "history": history}, fh, indent=2)
        except Exception:
            logger.exception("write session %s", session_id)

    # ── per-session exchanges.json (reopen-view transcript) ────────────────
    def _read_exchanges(self, session_id) -> list:
        f = self._exchanges_file(session_id)
        if f.exists():
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    return data
            except Exception:
                logger.exception("read exchanges %s", session_id)
        return []

    def _append_exchange(self, session_id, task, done_message, status) -> None:
        """Append this run's (user request, terminal message) pair. Every run
        ending records one — completion, user stop, error alike — so a reopened
        chat can replay '1. request / outcome' for its whole life."""
        items = self._read_exchanges(session_id)
        items.append({
            "task": str(task or ""),
            "done_message": str(done_message or ""),
            "status": str(status or ""),
            "at": int(time.time()),
        })
        f = self._exchanges_file(session_id)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(items, fh, indent=2, ensure_ascii=False)
        except Exception:
            logger.exception("write exchanges %s", session_id)

    # ── helpers ────────────────────────────────────────────────────────────
    def _new_id(self) -> str:
        """Filesystem-safe persistent chat id (distinct from app.py's per-run
        guard token)."""
        return f"chat_{int(time.time() * 1000):x}"

    def _title_from_task(self, task) -> str:
        """First non-empty line of the task, trimmed to a short sidebar label."""
        text = (task or "").strip()
        if not text:
            return "New chat"
        first = text.splitlines()[0].strip()
        return first if len(first) <= 60 else first[:57].rstrip() + "..."

    def _terminal_message(self, status: str, message: str) -> str:
        """Clean 'done message' for ANY ending — so an interrupted / failed /
        provider-killed run still records a sensible conclusion in memory."""
        message = message or ""
        if status == "success":
            return f"Task completed: {message}".strip().rstrip(":")
        if status == "error":
            return f"Agent terminated (error / provider): {message}".strip().rstrip(":")
        return f"Agent stopped before completing: {message}".strip().rstrip(":")

    def _build_history(self, task, assistant_messages, tool_responses,
                       status, message, prior_task=None) -> dict:
        """Build the saved conversation = the agent's FULL per-step memory
        (thinking / eval / decision / memory / next_goal / action all preserved,
        tool results as-is), capped with a terminal note recording how the run
        ended.

        We store the FULL steps — NOT a trimmed copy — so the debug download is
        the true, human-readable agent memory. Resume token-efficiency is
        unaffected: the agent's OWN message builder trims older steps to
        decision/memory/next_goal when it sends them to the LLM, so storing full
        here costs only a little disk and loses nothing for debugging.
        prior_task preserves the ORIGINAL objective across resumes."""
        full_assistant = list(assistant_messages or [])
        tools = list(tool_responses or [])
        # Force exact 1:1 alignment defensively.
        if len(tools) < len(full_assistant):
            tools += [None] * (len(full_assistant) - len(tools))
        else:
            tools = tools[:len(full_assistant)]
        done_message = self._terminal_message(status, message)
        # Cap with a terminal note so a resumed run (and the reader) always sees
        # how the previous run ended — on EVERY ending (done / stop / error). On
        # resume it becomes the most-recent step, so the agent's builder replays
        # every real step's tool result beneath it.
        full_assistant.append(json.dumps({
            "memory": done_message,
            "next_goal": "Previous run concluded. Awaiting the user's next request; resume from this point.",
        }, ensure_ascii=False, indent=2))
        tools.append(None)
        return {
            "version": 1,
            "task": prior_task or task,
            "assistant_messages": full_assistant,
            "tool_responses": tools,
            "final_status": status,
            "final_message": message,
            "done_message": done_message,
        }

    # ── public API used by app.py ──────────────────────────────────────────
    def start_or_resume(self, req_session_id, task):
        """Resolve the chat session for an incoming run.

        Returns (session_id, prior_history):
          - existing session (folder present)  -> CONTINUATION: load its saved
            optimized history (None if missing/corrupt -> fresh agent context on
            the SAME id).
          - otherwise -> fresh start: mint a new id and seed its index entry so
            the sidebar shows it immediately. prior_history is None.
        """
        if req_session_id and req_session_id != "new" and self._session_dir(req_session_id).exists():
            sid = str(req_session_id)
            return sid, self._read_session(sid)
        sid = self._new_id()
        self._touch_index(sid, title=self._title_from_task(task))
        return sid, None

    def save_run(self, session_id, assistant_messages, tool_responses, status,
                 message, task, last_messages=None, context_tokens=None,
                 context_cap=None, run_pkg=None):
        """Persist a finished run + refresh index meta. Writes TWO things:
          - conversation.json  — the lean resume seed (assistant/tool turns).
          - memory_log.txt      — the TRUE debug memory: the exact final payload
            sent to the model (system + interleaved history + the live user
            message that re-injects user_request/todo/scratchpad/element_tree).
        Called for EVERY ending (completion, user stop, error, provider kill)."""
        try:
            existing = self._read_session(session_id)
            prior_task = existing.get("task") if isinstance(existing, dict) else None
            payload = self._build_history(
                task, assistant_messages, tool_responses, status, message, prior_task
            )
            self._write_session(session_id, payload)
            done_message = payload["done_message"]
            # Reopen-view transcript: this run's request + how it ended.
            self._append_exchange(session_id, task, done_message, status)

            # TRUE memory log (debug): render the exact payload the model received.
            if last_messages:
                header = [
                    "=== AGENT MEMORY — exact payload sent to the model (true snapshot) ===",
                    f"Session: {session_id}",
                    f"Task: {payload.get('task', '')}",
                    f"Final status: {status}",
                    f"Done: {done_message}",
                ]
                # The latest step's response is generated AFTER last_messages was
                # built, so append it as the final assistant turn (like main.py).
                final_response = (assistant_messages or [None])[-1]
                text = self._render_messages(last_messages, header, final_response)
                self._write_text(self._memory_log_file(session_id), text)

            self._touch_index(
                session_id,
                title=self._title_from_task(prior_task or task),  # no-op if already set
                last_done_message=done_message,
                context_tokens=context_tokens,  # latest context size for the memory bar
                context_cap=context_cap,        # fixed 300k memory budget (MEMORY_CAP)
                run_pkg=run_pkg,                # which agent produced this memory
            )
            return done_message
        except Exception:
            logger.exception("save_run %s", session_id)
            return None

    def _write_text(self, path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            logger.exception("write text %s", path)

    @staticmethod
    def _content_text(content):
        """Flatten a message's content to text. Prompt-cached turns carry content
        as a list of {type, text, ...} blocks rather than a plain string."""
        if isinstance(content, list):
            return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        return content if isinstance(content, str) else str(content)

    def _render_messages(self, messages, header_lines=None, final_response=None):
        """Render an exact messages payload to the human-readable conversation log
        (same shape as main.py's conversation_N.txt): SYSTEM PROMPT block, then
        each ASSISTANT / USER turn in the order sent — the USER turns include the
        live <user_request>/<todo_list>/<scratchpad>/<element_tree>."""
        bar = "=" * 60
        out = list(header_lines or [])
        if header_lines:
            out += [bar, ""]
        for m in messages or []:
            role = str(m.get("role", "?")).upper()
            content = self._content_text(m.get("content", ""))
            if role == "SYSTEM":
                out += ["=== SYSTEM PROMPT ===", content, "", bar, ""]
            else:
                out += [f"--- {role} ---", content, ""]
        if final_response:
            out += ["--- ASSISTANT (final response) ---", str(final_response), ""]
        return "\n".join(out) + "\n"

    def list_sessions(self):
        """All sessions, newest-first, as {id, name, updated_at, last_done_message}."""
        index = self._read_index()
        items = [{
            "id": sid,
            "name": meta.get("title") or "New chat",
            "updated_at": meta.get("updated_at", 0),
            "last_done_message": meta.get("last_done_message", ""),
        } for sid, meta in index.items()]
        items.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return items

    def get_session(self, session_id):
        """{id, name, last_done_message, exchanges, context_tokens, context_cap}
        for the reopen view, or None if unknown. `exchanges` is the full
        request/outcome transcript ({task, done_message, status} per run) shown
        as the numbered Agent Notes on reopen; last_done_message stays for
        legacy sessions saved before exchanges.json existed. Legacy rows that
        only carried the old cumulative `tokens_used` are ignored
        (context_tokens defaults to 0, so the bar starts empty and the next run
        corrects it)."""
        meta = self._read_index().get(str(session_id))
        if not meta:
            return None
        return {
            "id": str(session_id),
            "name": meta.get("title") or "New chat",
            "context_tokens": int(meta.get("context_tokens", 0) or 0),
            "context_cap": int(meta.get("context_cap", 0) or 0),
            "last_done_message": meta.get("last_done_message", ""),
            "run_pkg": meta.get("run_pkg", ""),
            "exchanges": self._read_exchanges(session_id),
        }

    def delete_session(self, session_id) -> bool:
        """Remove a session's folder and its index entry (idempotent)."""
        try:
            d = self._session_dir(session_id)
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            index = self._read_index()
            if str(session_id) in index:
                del index[str(session_id)]
                self._write_index(index)
            return True
        except Exception:
            logger.exception("delete_session %s", session_id)
            return False

    def read_raw(self, session_id):
        """Full saved conversation.json payload ({session_id, history}) exactly as
        on disk, or None if missing/corrupt. This is the agent's real optimized
        memory — used by the debug export."""
        f = self._session_file(session_id)
        if f.exists():
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    return data
            except Exception:
                logger.exception("read_raw %s", session_id)
        return None

    def render_readable(self, session_id, system_prompt=None):
        """Render a session's saved memory as a HUMAN-READABLE conversation log —
        the same shape main.py's save_conversation writes (conversation_N.txt):
        a SYSTEM PROMPT block, then the real interleaved ASSISTANT (per-step JSON,
        full thinking/eval/decision/memory/next_goal/action) / USER
        (<tool_response>) turns. This is the true agent memory, formatted to read
        and critique — not raw JSON. Returns the text, or None if nothing saved."""
        data = self.read_raw(session_id)
        if data is None:
            return None
        hist = data.get("history") or {}
        meta = self.get_session(session_id) or {}
        asst = hist.get("assistant_messages") or []
        tools = hist.get("tool_responses") or []
        task = hist.get("task", "") or ""
        bar = "=" * 60
        out = [
            "=== AGENT MEMORY (conversation log) ===",
            f"Chat: {meta.get('name', '')}",
            f"Session: {session_id}",
            f"Steps: {len(asst)}",
            f"Final status: {hist.get('final_status', '')}",
            f"Done: {hist.get('done_message', '')}",
            f"Task: {task}",
            bar,
        ]
        if system_prompt:
            out += ["", "=== SYSTEM PROMPT ===", system_prompt.strip(), bar]
        for i, step in enumerate(asst):
            out += ["", "--- ASSISTANT ---"]
            if i == 0 and task:
                out += ["<User_Task>", task, "</User_Task>", ""]
            out.append(str(step))
            tr = tools[i] if i < len(tools) else None
            if tr:
                out += ["", "--- USER ---", "<tool_response>", str(tr), "</tool_response>"]
        return "\n".join(out) + "\n"

    def export_to_downloads(self, session_id, system_prompt=None):
        """Debug aid: write a session's TRUE memory log (.txt) to the user's
        Downloads folder. Prefers memory_log.txt (the exact payload sent to the
        model); falls back to a reconstruction for chats saved before memory_log
        existed. Returns the destination path, or None if nothing's saved."""
        text = None
        log_file = self._memory_log_file(session_id)
        if log_file.exists():
            try:
                text = log_file.read_text(encoding="utf-8")
            except Exception:
                logger.exception("read memory_log %s", session_id)
                text = None
        if text is None:
            text = self.render_readable(session_id, system_prompt)  # back-compat fallback
        if text is None:
            return None
        meta = self.get_session(session_id) or {}
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", meta.get("name") or str(session_id)).strip("-")[:40]
        # System date-time in a clear, readable form so each download is easy to tell apart.
        fname = f"autouse-{slug or session_id}-{time.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        home = Path.home()
        for folder in (home / "Downloads", home / "Desktop", home):
            try:
                if folder.exists():
                    dest = folder / fname
                    with open(dest, "w", encoding="utf-8") as fh:
                        fh.write(text)
                    return str(dest)
            except Exception:
                logger.exception("export_to_downloads -> %s", folder)
                continue
        return None


# Module-level singleton — app.py imports this directly.
conversation = ConversationService()
