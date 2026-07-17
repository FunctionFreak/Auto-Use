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

# Picks the right AgentService for the requested mode:
#   mode = "computer use"     -> desktop agent for the host OS (macOS_use / windows_use)
#   mode = "mobile use, ios"  -> iPhone agent; the phone connection (WDA session)
#                                is opened before the run and closed after it,
#                                same flow the app's mode dial uses (ios_connector).
import inspect
import platform
import time


def _parse_mode(mode, os=None):
    """Split a mode string into (kind, device), e.g. "mobile use, ios" -> ("mobile", "ios")."""
    words = str(mode).strip().lower().replace(",", " ").replace("_", " ").split()
    words = [w for w in words if w != "use"]
    kind = words[0] if words else ""
    device = words[1] if len(words) > 1 else str(os or "").strip().lower()
    return kind, device


def resolve_agent_service(mode, os=None):
    """Return the AgentService class for the given mode.

    mode: "computer use" (host OS auto-detected, nothing else needed) or
          "mobile use, ios" / "mobile use, android" — the device os rides
          along in the same string. Underscores/commas/case don't matter.
    os:   optional separate device os ("ios"/"android") for callers that
          don't embed it in mode; the embedded one wins if both are given.
    """
    kind, os = _parse_mode(mode, os)

    if kind == "computer":
        host = platform.system()
        if host == "Darwin":
            from Auto_Use.macOS_use.agent.main_driver.service import AgentService
        elif host == "Windows":
            from Auto_Use.windows_use.agent.main_driver.service import AgentService
        else:
            raise RuntimeError(f"Unsupported OS for computer use: {host}")
        return AgentService

    if kind == "mobile":
        device = str(os or "").strip().lower()
        if device == "ios":
            from Auto_Use.ios_use.agent.main_driver.service import AgentService
            return AgentService
        if device == "android":
            raise NotImplementedError("Android support is not available yet")
        raise ValueError('mobile use needs a device — "mobile use, ios" or "mobile use, android"')

    raise ValueError(f'Unknown mode "{mode}" — use "computer use" or "mobile use, ios"')


def _connect_iphone(poll_every=2.0, deadline=90.0):
    """Open the iPhone WDA session and wait until it answers (the app's mode-dial flow).

    Returns the live wda_session; raises RuntimeError if the phone can't be reached.
    """
    from Auto_Use.ios_connector.session import wda_session

    res = wda_session.activate()
    if not res.get("ok"):
        raise RuntimeError(f"📱 iPhone connection failed: {res.get('error') or res.get('code') or res}")
    if res.get("state") == "connected":
        print(f"📱 iPhone already connected ({res.get('udid', '')})")
        return wda_session

    print("📱 Connecting iPhone...")
    end = time.time() + deadline
    while time.time() < end:
        time.sleep(poll_every)
        try:
            st = wda_session.status()
        except Exception:
            continue  # transient probe hiccup, keep polling (matches the UI)
        state = st.get("state")
        if state == "connected":
            print(f"📱 iPhone connected ({st.get('udid', '')})")
            return wda_session
        if state in ("error", "disconnected"):
            wda_session.deactivate()  # reap half-started processes, like the UI's fail path
            detail = st.get("hint") or st.get("error") or st.get("code") or state
            raise RuntimeError(f"📱 iPhone connection failed: {detail}")

    wda_session.deactivate()
    raise RuntimeError(f"📱 iPhone connection timed out after {int(deadline)}s")


def run_agent(mode, provider, model, task, os=None,
              save_conversation=False, external_terminal=False, **agent_kwargs):
    """Create the AgentService for mode/os, run the task, return its response dict.

    For "mobile use, ios" the phone connection is opened first and always
    closed when the agent terminates (success, error, or Ctrl+C).
    """
    kind, device = _parse_mode(mode, os)
    AgentService = resolve_agent_service(mode, os)

    kwargs = dict(provider=provider, model=model,
                  save_conversation=save_conversation, **agent_kwargs)
    # The mobile AgentService has no external_terminal param (desktop-only feature)
    if "external_terminal" in inspect.signature(AgentService.__init__).parameters:
        kwargs["external_terminal"] = external_terminal

    phone = _connect_iphone() if (kind, device) == ("mobile", "ios") else None
    try:
        agent = AgentService(**kwargs)
        return agent.process_request(task)
    finally:
        if phone is not None:
            phone.deactivate()
            print("📱 iPhone connection closed")
