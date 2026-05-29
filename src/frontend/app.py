"""Streamlit chat UI for the Stateful Personal Assistant.

Install frontend deps (separate from the backend Docker image):
    pip install -r requirements-frontend.txt

Run alongside the FastAPI backend:
    uvicorn src.api.main:app --reload --port 8000   # terminal 1
    streamlit run src/frontend/app.py               # terminal 2
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Generator
from typing import Any

import requests
import streamlit as st

# ── Configuration ─────────────────────────────────────────────────────────────

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
REQUEST_TIMEOUT = 10  # seconds for non-streaming calls
STREAM_TIMEOUT = 120  # seconds for SSE connections
HITL_RECONNECT_RETRIES = 6
HITL_RECONNECT_DELAY = 3  # seconds between reconnect attempts after approval

# ── Page setup ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Stateful Personal Assistant",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Inject a small style tweak so the chat container fills the viewport height
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; }
    .stChatMessage { border-radius: 0.75rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Session-state helpers ─────────────────────────────────────────────────────


def _init_state() -> None:
    """Initialise session-state keys on first page load."""
    defaults: dict[str, Any] = {
        "access_token": None,
        "session_id": None,
        "chat_history": [],  # list of {role, content, meta}
        "is_guest": True,
        "user_email": None,
        "pending_hitl": None,  # dict with HITL context when approval is required
        "stream_id": None,  # kept for HITL reconnection
        "last_event_id": 0,  # last SSE event id seen in this turn
        "backend_ok": True,
        "initialized": True,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _auth_headers() -> dict[str, str]:
    token = st.session_state.get("access_token") or ""
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Backend helpers ───────────────────────────────────────────────────────────


def _backend_get(path: str, **kwargs: Any) -> requests.Response | None:
    """GET request; returns None and flags backend_ok=False on connection error."""
    try:
        resp = requests.get(
            f"{BACKEND_URL}{path}",
            headers=_auth_headers(),
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
        st.session_state.backend_ok = True
        return resp
    except requests.ConnectionError:
        st.session_state.backend_ok = False
        return None
    except requests.Timeout:
        st.session_state.backend_ok = False
        return None


def _backend_post(path: str, payload: dict[str, Any]) -> requests.Response | None:
    """POST request; returns None on connection error."""
    try:
        resp = requests.post(
            f"{BACKEND_URL}{path}",
            json=payload,
            headers=_auth_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        st.session_state.backend_ok = True
        return resp
    except requests.ConnectionError:
        st.session_state.backend_ok = False
        return None
    except requests.Timeout:
        st.session_state.backend_ok = False
        return None


def _obtain_guest_token() -> bool:
    """Call POST /auth/guest and store the token. Returns True on success."""
    resp = _backend_post("/auth/guest", {})
    if resp is None:
        return False
    if resp.status_code == 200:
        data = resp.json()
        st.session_state.access_token = data["access_token"]
        st.session_state.session_id = data.get("session_id")
        st.session_state.is_guest = True
        st.session_state.user_email = None
        return True
    return False


def _register(email: str, password: str, confirm: str) -> str | None:
    """Call POST /auth/register. Returns an error message or None on success."""
    if not email or not password:
        return "Email and password are required."
    if password != confirm:
        return "Passwords do not match."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    resp = _backend_post("/auth/register", {"email": email, "password": password})
    if resp is None:
        return "Cannot reach the backend server."
    if resp.status_code == 201:
        data = resp.json()
        st.session_state.access_token = data["access_token"]
        st.session_state.is_guest = False
        st.session_state.user_email = email.lower().strip()
        st.session_state.session_id = None
        st.session_state.chat_history = []
        st.session_state.pending_hitl = None
        return None
    if resp.status_code == 409:
        return "An account with this email already exists. Please log in."
    if resp.status_code == 422:
        detail = resp.json().get("detail", {})
        return str(detail.get("message", "Invalid input."))
    return f"Registration failed (HTTP {resp.status_code}). Please try again."


def _login(email: str, password: str) -> str | None:
    """Call POST /auth/login. Returns an error message or None on success."""
    resp = _backend_post("/auth/login", {"email": email, "password": password})
    if resp is None:
        return "Cannot reach the backend server."
    if resp.status_code == 200:
        data = resp.json()
        st.session_state.access_token = data["access_token"]
        st.session_state.is_guest = False
        st.session_state.user_email = email
        # Clear any stale guest session_id so the next chat starts fresh
        st.session_state.session_id = None
        st.session_state.chat_history = []
        st.session_state.pending_hitl = None
        return None
    if resp.status_code == 429:
        return "Too many login attempts. Please wait and try again."
    return "Invalid email or password."


def _logout() -> None:
    """Call POST /auth/logout then fall back to guest mode."""
    if not st.session_state.is_guest:
        _backend_post("/auth/logout", {})
    # Re-issue a guest token for anonymous use
    st.session_state.chat_history = []
    st.session_state.session_id = None
    st.session_state.pending_hitl = None
    _obtain_guest_token()


# ── SSE streaming ─────────────────────────────────────────────────────────────


def _sse_events(
    url: str,
    last_event_id: int = 0,
) -> Generator[dict[str, str], None, None]:
    """Stream SSE events from *url*, yielding dicts with 'event', 'data', 'id'.

    Handles keep-alive comment lines (starting with ':') silently.
    Yields each complete event (delimited by a blank line).
    """
    req_headers = dict(_auth_headers())
    req_headers["Accept"] = "text/event-stream"
    req_headers["Cache-Control"] = "no-cache"
    if last_event_id:
        req_headers["Last-Event-ID"] = str(last_event_id)

    try:
        with requests.get(url, headers=req_headers, stream=True, timeout=STREAM_TIMEOUT) as resp:
            if resp.status_code == 410:
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {
                            "code": "STREAM_EXPIRED",
                            "message": "This conversation stream has expired.",
                            "retryable": False,
                        }
                    ),
                    "id": "0",
                }
                return
            if resp.status_code == 403:
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {
                            "code": "FORBIDDEN",
                            "message": "Access denied to this stream.",
                            "retryable": False,
                        }
                    ),
                    "id": "0",
                }
                return
            if not resp.ok:
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {
                            "code": "HTTP_ERROR",
                            "message": f"Backend error: HTTP {resp.status_code}",
                            "retryable": True,
                        }
                    ),
                    "id": "0",
                }
                return

            current: dict[str, str] = {}
            for raw_line in resp.iter_lines():
                line: str = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line

                if line.startswith(":"):
                    continue  # keep-alive

                if not line:
                    # Blank line → dispatch completed event
                    if current:
                        yield current
                        current = {}
                    continue

                if ":" in line:
                    field, _, value = line.partition(":")
                    value = value.lstrip(" ")
                    current[field.strip()] = value

    except requests.ConnectionError:
        st.session_state.backend_ok = False
        yield {
            "event": "error",
            "data": json.dumps(
                {
                    "code": "CONNECTION_ERROR",
                    "message": "Lost connection to the backend server.",
                    "retryable": True,
                }
            ),
            "id": "0",
        }
    except requests.Timeout:
        yield {
            "event": "error",
            "data": json.dumps(
                {
                    "code": "TIMEOUT",
                    "message": "The response timed out. Please try again.",
                    "retryable": True,
                }
            ),
            "id": "0",
        }


# ── HITL handling ─────────────────────────────────────────────────────────────


def _submit_hitl_decision(decision: str) -> str | None:
    """POST /sessions/{id}/approve with decision. Returns error message or None."""
    hitl = st.session_state.pending_hitl
    if not hitl:
        return "No pending approval."

    session_id = st.session_state.session_id
    if not session_id:
        return "No active session."

    resp = _backend_post(
        f"/sessions/{session_id}/approve",
        {"approval_id": hitl["approval_id"], "decision": decision},
    )
    if resp is None:
        return "Cannot reach the backend server."
    if resp.status_code == 200:
        return None
    if resp.status_code == 409:
        return "Approval was already processed concurrently."
    if resp.status_code == 410:
        return "Approval has expired. Please retry your request."
    return f"Approval error: HTTP {resp.status_code}"


def _reconnect_after_hitl() -> None:
    """Reconnect to the SSE stream after HITL approval and display resumed events."""
    hitl = st.session_state.pending_hitl
    if not hitl:
        return

    stream_id = hitl.get("stream_id")
    last_event_id = hitl.get("last_event_id", 0)
    if not stream_id:
        st.warning("Stream ID not available for reconnection.")
        return

    url = f"{BACKEND_URL}/chat/stream?stream_id={stream_id}"

    with st.chat_message("assistant"):
        status = st.empty()
        response_placeholder = st.empty()
        full_response = ""

        # Poll for resumed events — backend's _resume_graph is async
        for attempt in range(HITL_RECONNECT_RETRIES):
            status.info(
                f"Waiting for backend response... (attempt {attempt + 1}/{HITL_RECONNECT_RETRIES})"
            )
            time.sleep(HITL_RECONNECT_DELAY)

            got_events = False
            for ev in _sse_events(url, last_event_id=last_event_id):
                event_type = ev.get("event", "")
                event_id = int(ev.get("id", last_event_id))
                try:
                    data: dict[str, Any] = json.loads(ev.get("data", "{}"))
                except json.JSONDecodeError:
                    continue

                if event_type == "thinking":
                    node = data.get("node", "")
                    status.info(f"Thinking... `{node}`")
                    got_events = True

                elif event_type == "token":
                    full_response += data.get("content", "")
                    response_placeholder.markdown(full_response + "▌")
                    got_events = True

                elif event_type == "tool_result":
                    tool_name = data.get("tool", "unknown")
                    status.success(f"Tool `{tool_name}` completed.")
                    got_events = True

                elif event_type == "error":
                    status.error(data.get("message", "An error occurred."))
                    response_placeholder.empty()
                    st.session_state.pending_hitl = None
                    return

                elif event_type == "done":
                    status.empty()
                    response_placeholder.markdown(full_response or "_No text response._")
                    if full_response:
                        st.session_state.chat_history.append(
                            {"role": "assistant", "content": full_response, "meta": {}}
                        )
                    st.session_state.pending_hitl = None
                    return

                last_event_id = max(last_event_id, event_id)

            if got_events:
                # Got partial events but no done yet; keep retrying
                continue

        # Exhausted retries
        status.warning(
            "The backend is still processing your request. "
            "You may send a new message and the assistant will continue from where it left off."
        )
        st.session_state.pending_hitl = None


# ── Core chat flow ────────────────────────────────────────────────────────────


def _send_and_stream(user_message: str) -> None:
    """Submit *user_message* to the backend and stream the response into the UI."""
    # Step 1: POST /chat → get stream_url
    payload: dict[str, Any] = {"message": user_message}
    if st.session_state.session_id:
        payload["session_id"] = st.session_state.session_id

    resp = _backend_post("/chat", payload)
    if resp is None:
        with st.chat_message("assistant"):
            st.error(
                f"Cannot reach the backend server at `{BACKEND_URL}`. "
                "Make sure the FastAPI server is running."
            )
        return

    if resp.status_code == 429:
        detail = resp.json().get("detail", {})
        with st.chat_message("assistant"):
            st.warning(
                f"Quota exceeded ({detail.get('quota_type', 'unknown')}). "
                "Please wait before sending another message."
            )
        return

    if not resp.ok:
        with st.chat_message("assistant"):
            st.error(f"Backend returned HTTP {resp.status_code}. Please try again.")
        return

    chat_data = resp.json()
    st.session_state.session_id = chat_data.get("session_id", st.session_state.session_id)
    stream_path: str = chat_data["stream_url"]  # e.g. "/chat/stream?stream_id=..."
    stream_id = stream_path.split("stream_id=")[-1] if "stream_id=" in stream_path else ""
    st.session_state.stream_id = stream_id

    stream_url = f"{BACKEND_URL}{stream_path}"

    # Step 2: stream SSE events
    with st.chat_message("assistant"):
        status = st.empty()
        response_placeholder = st.empty()
        tool_expander_placeholder = st.empty()

        full_response = ""
        tool_results: list[dict[str, Any]] = []
        last_event_id = 0
        pending_hitl: dict[str, Any] | None = None

        for ev in _sse_events(stream_url):
            event_type = ev.get("event", "")
            try:
                data: dict[str, Any] = json.loads(ev.get("data", "{}"))
            except json.JSONDecodeError:
                continue

            event_id_raw = ev.get("id", "0")
            try:
                last_event_id = int(event_id_raw)
            except ValueError:
                pass

            if event_type == "thinking":
                node = data.get("node", "")
                elapsed = data.get("elapsed_ms", 0)
                status.info(f"Thinking... `{node}` ({elapsed} ms)")

            elif event_type == "token":
                full_response += data.get("content", "")
                response_placeholder.markdown(full_response + "▌")

            elif event_type == "tool_result":
                tool_name = data.get("tool", "unknown")
                tool_results.append(data)
                status.success(f"Tool `{tool_name}` completed.")

            elif event_type == "approval_required":
                pending_hitl = {
                    "approval_id": data.get("approval_id", ""),
                    "tool": data.get("tool", ""),
                    "description": data.get("description", ""),
                    "stream_id": stream_id,
                    "last_event_id": last_event_id,
                }
                status.warning(
                    f"Approval required to run `{data.get('tool', 'a tool')}`. "
                    "See the approval banner below."
                )

            elif event_type == "error":
                code = data.get("code", "ERROR")
                message = data.get("message", "An unexpected error occurred.")
                retryable = data.get("retryable", True)
                status.empty()
                st.error(f"**{code}**: {message}" + (" (you may retry)" if retryable else ""))

            elif event_type == "done":
                status.empty()
                if full_response:
                    response_placeholder.markdown(full_response)
                elif not pending_hitl:
                    response_placeholder.markdown("_No response received._")

        # Show collected tool results in an expander
        if tool_results:
            with tool_expander_placeholder.expander(
                f"Tool calls ({len(tool_results)})", expanded=False
            ):
                for tr in tool_results:
                    st.markdown(f"**`{tr.get('tool', 'tool')}`**")
                    result_data = tr.get("result", {})
                    st.json(result_data)

        # Persist in history
        if full_response:
            st.session_state.chat_history.append(
                {
                    "role": "assistant",
                    "content": full_response,
                    "meta": {"tool_results": tool_results},
                }
            )

        # Store pending HITL for the approval banner
        if pending_hitl:
            st.session_state.pending_hitl = pending_hitl


# ── Sidebar ───────────────────────────────────────────────────────────────────


def _render_sidebar() -> None:
    with st.sidebar:
        st.title("🤖 Assistant")
        st.caption("Stateful Personal Assistant")
        st.divider()

        # ── Auth section ──────────────────────────────────────────────────────
        if st.session_state.is_guest:
            st.markdown("**Mode:** Guest (ephemeral)")
            st.caption("Messages are not saved between sessions.")
            st.divider()

            login_tab, register_tab = st.tabs(["Log in", "Register"])

            with login_tab:
                email = st.text_input("Email", key="login_email")
                password = st.text_input("Password", type="password", key="login_password")
                if st.button("Log in", use_container_width=True, key="btn_login"):
                    if email and password:
                        error = _login(email, password)
                        if error:
                            st.error(error)
                        else:
                            st.success("Logged in!")
                            st.rerun()
                    else:
                        st.warning("Please enter email and password.")

            with register_tab:
                reg_email = st.text_input("Email", key="reg_email")
                reg_password = st.text_input("Password", type="password", key="reg_password")
                reg_confirm = st.text_input("Confirm password", type="password", key="reg_confirm")
                if st.button("Create account", use_container_width=True, key="btn_register"):
                    error = _register(reg_email, reg_password, reg_confirm)
                    if error:
                        st.error(error)
                    else:
                        st.success("Account created! You are now logged in.")
                        st.rerun()
        else:
            st.markdown(f"**Logged in as:** {st.session_state.user_email}")
            if st.button("Log out", use_container_width=True):
                _logout()
                st.rerun()

        st.divider()

        # ── Session controls ──────────────────────────────────────────────────
        if st.button("New conversation", use_container_width=True):
            st.session_state.session_id = None
            st.session_state.chat_history = []
            st.session_state.pending_hitl = None
            st.session_state.last_event_id = 0
            if st.session_state.is_guest:
                # Re-obtain a fresh guest token for a clean slate
                _obtain_guest_token()
            st.rerun()

        if st.session_state.session_id:
            st.caption(f"Session: `{st.session_state.session_id[:8]}…`")

        st.divider()

        # ── Backend status ────────────────────────────────────────────────────
        st.markdown("**Backend**")
        health = _backend_get("/health")
        if health and health.status_code == 200:
            st.success("Online", icon="✅")
        else:
            st.error("Offline — start the FastAPI server", icon="❌")

        st.caption(f"URL: `{BACKEND_URL}`")


# ── HITL approval banner ──────────────────────────────────────────────────────


def _render_hitl_banner() -> None:
    """Show a sticky approval banner when a HITL decision is pending."""
    hitl = st.session_state.get("pending_hitl")
    if not hitl:
        return

    tool_name = hitl.get("tool", "a tool")
    description = hitl.get("description", "")

    with st.container(border=True):
        st.markdown(
            f"### Approval Required\n"
            f"The assistant wants to run **`{tool_name}`**."
            + (f"\n\n_{description}_" if description else "")
        )

        if st.session_state.is_guest:
            st.warning(
                "HITL approval requires an authenticated account. "
                "Please log in to approve or deny this action.",
                icon="🔒",
            )
            if st.button("Dismiss", key="hitl_dismiss"):
                st.session_state.pending_hitl = None
                st.rerun()
            return

        col1, col2, col3 = st.columns([2, 2, 6])
        with col1:
            approve_clicked = st.button("Approve", type="primary", key="hitl_approve")
        with col2:
            deny_clicked = st.button("Deny", type="secondary", key="hitl_deny")

        if approve_clicked or deny_clicked:
            decision = "approve" if approve_clicked else "deny"
            err = _submit_hitl_decision(decision)
            if err:
                st.error(err)
            else:
                st.session_state.chat_history.append(
                    {
                        "role": "assistant",
                        "content": f"_You chose to **{decision}** the `{tool_name}` action._",
                        "meta": {},
                    }
                )
                # Reconnect to get the resumed turn's events
                _reconnect_after_hitl()
            st.rerun()


# ── Chat history display ──────────────────────────────────────────────────────


def _render_history() -> None:
    for msg in st.session_state.chat_history:
        role = msg["role"]
        content = msg["content"]
        meta = msg.get("meta", {})

        with st.chat_message(role):
            st.markdown(content)

            tool_results = meta.get("tool_results", [])
            if tool_results:
                with st.expander(f"Tool calls ({len(tool_results)})", expanded=False):
                    for tr in tool_results:
                        st.markdown(f"**`{tr.get('tool', 'tool')}`**")
                        st.json(tr.get("result", {}))


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    _init_state()

    # Auto-obtain guest token on first load (or if token was lost)
    if not st.session_state.access_token:
        with st.spinner("Connecting to assistant…"):
            ok = _obtain_guest_token()
        if not ok:
            st.error(
                "Could not connect to the backend server at "
                f"`{BACKEND_URL}`. "
                "Please start the FastAPI server and refresh this page.",
                icon="❌",
            )
            st.code(
                "uvicorn src.api.main:app --reload --port 8000",
                language="bash",
            )
            return

    _render_sidebar()

    st.title("💬 Stateful Personal Assistant")
    st.caption(
        "Powered by LangGraph · Tools: weather, calculator, web search · "
        "Streaming · Human-in-the-Loop"
    )

    # HITL approval banner at the top so it's always visible
    _render_hitl_banner()

    st.divider()

    # Render chat history
    _render_history()

    # Chat input (pinned to bottom by Streamlit)
    user_input = st.chat_input("Ask anything…")

    if user_input:
        # Append user message immediately so the UI feels responsive
        st.session_state.chat_history.append({"role": "user", "content": user_input, "meta": {}})
        with st.chat_message("user"):
            st.markdown(user_input)

        # Stream the assistant's response
        _send_and_stream(user_input)

        # Rerun to sync history state (clears the streaming placeholders)
        st.rerun()


if __name__ == "__main__":
    main()
