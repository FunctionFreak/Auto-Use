// Copyright 2026 Ashish Yadav — Auto-Use

//! Browser session — opening Chrome, owning the CDP link, and lending it out.
//!
//! THIS side owns the browser. It launches Chrome with the remote-debugging
//! port open, opens and closes tabs over Chrome's HTTP endpoint, and holds the
//! ONE CDP session per tab that everything else borrows: the glow rides on it,
//! and every controller tool takes it for the length of a single operation to
//! click, type, scroll or navigate.
//!
//! Auto_Use/web/tree/element.rs is a SCANNER, not a driver, and not a process:
//! `scan_core` below calls it with the session and the tab to read, and gets
//! back a tree, its geometry and a screenshot. It cannot navigate, click,
//! type, open a tab or launch Chrome, and it holds no connection of its own.
//!
//! It used to be a second binary driven as a subprocess REPL, answering over
//! stdin/stdout with its results left in three files on disk. That meant two
//! sockets onto one browser, two ideas of which tab was current, and a scan
//! whose tree, geometry and screenshot were read back separately.

use std::collections::{HashMap, HashSet, VecDeque};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use base64::Engine;
use percent_encoding::{AsciiSet, CONTROLS};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use regex::Regex;
use serde_json::{json, Value};

use crate::tree::element;
use crate::ScannerError;

pub const CHROME_PORT: u16 = 9222;
// A blank tab is always about:blank — the URL the ADDRESS BAR shows. The logo
// is painted into it afterwards with Page.setDocumentContent.
const BLANK_URL: &str = "about:blank";

// Ceiling on buffered CDP events, as a last-resort guard against a run that
// buffers forever. It should not normally be reached: WAITED_METHODS below is
// what actually keeps the buffer small.
//
// Reaching it must drop the OLDEST event, never the newest. A cap that drops
// the newest is a trap: once full, the very event a caller is waiting for is
// the one thrown away, and because take_events only removes the method it was
// asked for, a buffer full of anything else never drains — so the wait stays
// deaf until the next clear_events. That turned every navigation on a
// subresource-heavy page into a silent 30-second timeout.
const EVENT_CAP: usize = 2000;

// The only CDP events anything in this crate ever waits on:
//   Page.loadEventFired        - navigation, reload and history waits
//   Network.requestWillBeSent  - settle()'s in-flight set
//   Network.loadingFinished    -   "
//   Network.loadingFailed      -   "
//   Target.attachedToTarget    - discovering cross-origin iframe sessions
//
// Everything else Chrome sends is never read by anyone. Buffering it only
// crowded out the events that matter: with Network.enable on, Chrome emits
// roughly eight events per request, so a page with a few hundred subresources
// used to bury the load event under its own noise.
const WAITED_METHODS: [&str; 6] = [
    "Page.loadEventFired",
    // Not waited on directly — it is what tells us a tab STARTED loading, so
    // switching to that tab knows to wait for it. See `loading`.
    "Page.frameNavigated",
    "Network.requestWillBeSent",
    "Network.loadingFinished",
    "Network.loadingFailed",
    "Target.attachedToTarget",
];

// Relative install paths under Program Files / Program Files (x86) /
// LOCALAPPDATA, in the same browser preference order as the macOS list below.
// Windows takes one of these three roots depending on installer and per-user
// vs per-machine install, so each browser is probed in all three.
const CHROME_RELATIVE_WIN: &[&str] = &[
    r"Google\Chrome\Application\chrome.exe",
    r"Chromium\Application\chrome.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
    r"Microsoft\Edge\Application\msedge.exe",
];

const CHROME_PATHS_UNIX: &[&str] = &[
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
];

// urllib.parse.quote(safe='') — everything except [A-Za-z0-9_.~-] is encoded.
const PY_QUOTE: &AsciiSet = &CONTROLS
    .add(b' ').add(b'!').add(b'"').add(b'#').add(b'$').add(b'%').add(b'&')
    .add(b'\'').add(b'(').add(b')').add(b'*').add(b'+').add(b',').add(b'/')
    .add(b':').add(b';').add(b'<').add(b'=').add(b'>').add(b'?').add(b'@')
    .add(b'[').add(b'\\').add(b']').add(b'^').add(b'`').add(b'{').add(b'|')
    .add(b'}').add(b'~').add(b'~');

// ---------------------------------------------------------------------------
// Errors. ScanErr::Scanner surfaces to Python as the module's ScannerError;
// ScanErr::Py passes an already-raised Python error (KeyboardInterrupt from a
// signal check) through untouched.
// ---------------------------------------------------------------------------

pub enum ScanErr {
    Scanner(String),
    Py(PyErr),
}

impl ScanErr {
    pub fn s(msg: impl Into<String>) -> Self {
        ScanErr::Scanner(msg.into())
    }
}

impl From<ScanErr> for PyErr {
    fn from(e: ScanErr) -> PyErr {
        match e {
            ScanErr::Scanner(msg) => ScannerError::new_err(msg),
            ScanErr::Py(err) => err,
        }
    }
}

pub type SResult<T> = Result<T, ScanErr>;

/// A CDP call's two failure shapes: a clean error reply (connection still in
/// step — Python's _CdpError) vs. a torn/late transport (connection dropped).
/// Cosmetics callers swallow both, so the messages are never surfaced —
/// they exist to keep the two paths legible.
#[allow(dead_code)]
pub enum CdpFail {
    Clean(String),
    Lost(String),
}

impl From<CdpFail> for ScanErr {
    fn from(e: CdpFail) -> ScanErr {
        match e {
            CdpFail::Clean(m) | CdpFail::Lost(m) => ScanErr::Scanner(m),
        }
    }
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/// `name` plus the platform's executable suffix — "cargo" stays "cargo" on
/// Unix and becomes "cargo.exe" on Windows, where the file on disk carries a
/// suffix callers never write.
fn exe_name(name: &str) -> String {
    if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_string()
    }
}

fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let exe = exe_name(name);
    std::env::split_paths(&path)
        .map(|d| d.join(&exe))
        .find(|c| c.is_file())
}

/// Windows sets USERPROFILE rather than HOME. Falling through to an empty path
/// would silently drop the Chrome profile into the current directory instead of
/// the user's home, so check both.
fn home_dir() -> PathBuf {
    match std::env::var_os("HOME").filter(|h| !h.is_empty()) {
        Some(h) => PathBuf::from(h),
        None => PathBuf::from(std::env::var_os("USERPROFILE").unwrap_or_default()),
    }
}

fn sleep_s(secs: f64) {
    std::thread::sleep(Duration::from_secs_f64(secs));
}

/// Check for a pending Python signal (Ctrl+C) from GIL-released Rust code, so
/// a long poll loop stays as interruptible as Python's `select` loop was.
fn check_py_signals() -> SResult<()> {
    Python::attach(|py| py.check_signals()).map_err(ScanErr::Py)
}

// ---------------------------------------------------------------------------
// Chrome's HTTP control endpoint — /json/list, /json/new. Not CDP.
// ---------------------------------------------------------------------------

fn chrome_http(port: u16, path: &str, method: &str) -> SResult<Value> {
    let addr = format!("127.0.0.1:{port}");
    let sock_addr = addr
        .parse()
        .map_err(|e| ScanErr::s(format!("bad address {addr}: {e}")))?;
    let mut s = TcpStream::connect_timeout(&sock_addr, Duration::from_secs(5))
        .map_err(|e| ScanErr::s(format!("{method} {path}: {e}")))?;
    s.set_read_timeout(Some(Duration::from_secs(5))).ok();
    s.set_write_timeout(Some(Duration::from_secs(5))).ok();
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    s.write_all(req.as_bytes())
        .map_err(|e| ScanErr::s(format!("{method} {path}: {e}")))?;

    let mut buf = Vec::new();
    let mut tmp = [0u8; 4096];
    let header_end = loop {
        let n = s
            .read(&mut tmp)
            .map_err(|e| ScanErr::s(format!("{method} {path}: {e}")))?;
        if n == 0 {
            return Err(ScanErr::s(format!(
                "{method} {path}: connection closed before http headers"
            )));
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            break i + 4;
        }
        if buf.len() > 64 * 1024 {
            return Err(ScanErr::s(format!("{method} {path}: http headers too large")));
        }
    };

    let headers = String::from_utf8_lossy(&buf[..header_end]).into_owned();
    let content_len = headers.lines().find_map(|l| {
        let (k, v) = l.split_once(':')?;
        if k.eq_ignore_ascii_case("content-length") {
            v.trim().parse::<usize>().ok()
        } else {
            None
        }
    });

    match content_len {
        Some(len) => {
            while buf.len() < header_end + len {
                let n = s
                    .read(&mut tmp)
                    .map_err(|e| ScanErr::s(format!("{method} {path}: {e}")))?;
                if n == 0 {
                    return Err(ScanErr::s(format!(
                        "{method} {path}: connection closed before full body"
                    )));
                }
                buf.extend_from_slice(&tmp[..n]);
            }
            buf.truncate(header_end + len);
        }
        None => {
            // Connection: close was requested — read to EOF.
            loop {
                match s.read(&mut tmp) {
                    Ok(0) => break,
                    Ok(n) => buf.extend_from_slice(&tmp[..n]),
                    Err(_) => break,
                }
            }
        }
    }

    let body = String::from_utf8_lossy(&buf[header_end..]).into_owned();
    // Python returns the raw body when it isn't JSON.
    Ok(serde_json::from_str(body.trim()).unwrap_or(Value::String(body)))
}

fn page_targets(port: u16) -> Vec<Value> {
    match chrome_http(port, "/json/list", "GET") {
        Ok(Value::Array(items)) => items
            .into_iter()
            .filter(|t| t.get("type").and_then(Value::as_str) == Some("page"))
            .collect(),
        _ => Vec::new(),
    }
}

/// A tab's CDP target id, or "" — every caller wants the same field out of
/// the Value /json/list hands back.
pub fn target_id_of(target: &Value) -> &str {
    target.get("id").and_then(Value::as_str).unwrap_or("")
}

/// Whether that tab is still open.
pub fn tab_exists(port: u16, target_id: &str) -> bool {
    page_targets(port)
        .iter()
        .any(|t| target_id_of(t) == target_id)
}

/// Close one tab by target id, and report whether it actually left Chrome's
/// list. /json/close answers with plain text ("Target is closing"), so the
/// only thing worth judging is the target going away.
pub fn close_target(port: u16, target_id: &str) -> SResult<bool> {
    chrome_http(port, &format!("/json/close/{target_id}"), "GET")?;
    for _ in 0..20 {
        if !tab_exists(port, target_id) {
            return Ok(true);
        }
        sleep_s(0.1);
    }
    Ok(false)
}

/// True for the surfaces that carry no page of their own — including Chrome's
/// New Tab page, which is browser chrome, not a document.
pub fn blank_url(url: &str) -> bool {
    matches!(
        url.trim(),
        "" | "about:blank"
            | "chrome://newtab/"
            | "chrome://new-tab-page/"
            | "chrome://new-tab-page"
            | "edge://newtab/"
    )
}

/// Guarantee the browser has a tab for the agent to work in. Returns true if
/// a tab had to be created.
fn ensure_tab_impl(port: u16) -> SResult<bool> {
    if !page_targets(port).is_empty() {
        return Ok(false);
    }
    let quoted = percent_encoding::utf8_percent_encode(BLANK_URL, PY_QUOTE).to_string();
    if let Err(e) = chrome_http(port, &format!("/json/new?{quoted}"), "PUT") {
        let msg = match e {
            ScanErr::Scanner(m) => m,
            ScanErr::Py(err) => return Err(ScanErr::Py(err)),
        };
        return Err(ScanErr::s(format!("could not open a tab on port {port}: {msg}")));
    }
    for _ in 0..20 {
        if !page_targets(port).is_empty() {
            return Ok(true);
        }
        sleep_s(0.25);
    }
    Err(ScanErr::s(format!("opened a tab on port {port} but it never appeared")))
}

/// Create a fresh blank tab and return its target id — used by single-tab
/// agents so each one drives a tab of its own instead of whatever tab happens
/// to be frontmost in the shared browser. The /json/new response body is the
/// created target's JSON (the same contract element.rs's `n` relies on).
fn create_tab_impl(port: u16) -> SResult<String> {
    let quoted = percent_encoding::utf8_percent_encode(BLANK_URL, PY_QUOTE).to_string();
    let created = match chrome_http(port, &format!("/json/new?{quoted}"), "PUT") {
        Ok(v) => v,
        Err(ScanErr::Scanner(m)) => {
            return Err(ScanErr::s(format!("could not open a tab on port {port}: {m}")));
        }
        Err(e) => return Err(e),
    };
    let id = created
        .get("id")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ScanErr::s("Chrome did not return the new tab's target id"))?;
    for _ in 0..20 {
        if page_targets(port)
            .iter()
            .any(|t| t.get("id").and_then(Value::as_str) == Some(id.as_str()))
        {
            return Ok(id);
        }
        sleep_s(0.25);
    }
    Err(ScanErr::s(format!("opened tab {id} on port {port} but it never appeared")))
}

/// The logo page as one line of HTML, ready to render into a blank tab.
/// Empty string if the logo is missing — the tab then stays honestly blank.
fn blank_html_impl(logo_page: &PathBuf) -> String {
    match std::fs::read_to_string(logo_page) {
        Ok(raw) => raw.split_whitespace().collect::<Vec<_>>().join(" "),
        Err(_) => String::new(),
    }
}

/// Drop container lines that contain nothing the model can see. A container
/// earns its line only if a NUMBERED line sits beneath it (deeper indent,
/// before the tree returns to its level).
fn prune_empty_containers(tree: &str) -> String {
    let lines: Vec<&str> = tree.lines().collect();

    fn depth(ln: &str) -> usize {
        (ln.len() - ln.trim_start_matches(' ').len()) / 2
    }

    fn bare_container(ln: &str) -> bool {
        let s = ln.trim();
        s.starts_with('<')
            && !s.contains("</")
            && !s.ends_with("/>")
            && !matches!(s, "<element>" | "</element>" | "<frame>")
    }

    let mut keep: Vec<&str> = Vec::new();
    for (i, ln) in lines.iter().enumerate() {
        if !bare_container(ln) {
            keep.push(ln);
            continue;
        }
        let d = depth(ln);
        let mut earned = false;
        for nxt in &lines[i + 1..] {
            let s = nxt.trim();
            if s == "<element>" || s == "</element>" {
                break;
            }
            if s != "<frame>" && depth(nxt) <= d {
                break;
            }
            if s.starts_with('[') {
                earned = true;
                break;
            }
        }
        if earned {
            keep.push(ln);
        }
    }
    keep.join("\n")
}

/// glow.css + glow.js as one page script, read once per process. Empty string
/// when either asset is missing or unreadable — the overlay is then simply
/// absent, and nothing else about the run changes.
fn glow_source(glow_css: &PathBuf, glow_js: &PathBuf) -> &'static str {
    static CACHE: OnceLock<String> = OnceLock::new();
    CACHE.get_or_init(|| {
        match (
            std::fs::read_to_string(glow_css),
            std::fs::read_to_string(glow_js),
        ) {
            (Ok(css), Ok(js)) => {
                // glow.js reads AUTOUSE_CSS and adopts it; glow.html instead
                // links the stylesheet and leaves the name undefined.
                let quoted = serde_json::to_string(&css).unwrap_or_default();
                format!("var AUTOUSE_CSS = {quoted};\n{js}")
            }
            _ => String::new(),
        }
    })
}

// ---------------------------------------------------------------------------
// This side's own thin CDP line to Chrome, for dressing tabs (favicon, glow).
// Chrome happily serves several CDP clients at once; the scanner's socket is
// never touched. Deliberately NO Origin header — tungstenite's plain client
// handshake sends none, and a socket with no Origin needs no
// --remote-allow-origins flag. Cosmetics-only by contract: callers swallow
// every failure, and a dead socket just gets dropped and lazily redialed.
// ---------------------------------------------------------------------------

pub struct Cdp {
    port: u16,
    ws: Option<tungstenite::WebSocket<TcpStream>>,
    id: u64,
    /// Events seen while waiting for a reply. CDP interleaves them with
    /// answers, and an action that has to wait for one (a navigation's
    /// loadEventFired) would never see it if they were dropped on the floor.
    events: VecDeque<Value>,
    /// Answers that arrived for a request other than the one being waited on.
    /// One call is in flight at a time, so this only ever holds a reply the
    /// event drain happened to read first — but dropping it would strand the
    /// caller waiting for an answer already off the wire.
    replies: HashMap<u64, Value>,
    /// targetId -> sessionId (flatten mode)
    sessions: HashMap<String, String>,
    /// Sessions whose MAIN frame has started navigating and has not reported
    /// its load event yet.
    ///
    /// This is the only way to answer "is that tab still loading?" without
    /// running JavaScript in it. It matters on switch_tab: binding to a tab
    /// that is mid-load and scanning it immediately hands the model half a
    /// page with no hint that the rest is still coming. `settle()` cannot
    /// cover this — it only knows about requests whose start it witnessed
    /// itself, and a navigation begun before the scan is invisible to it.
    loading: HashSet<String>,
    /// Bumped on every hang-up. Sessions and anything registered inside them
    /// die with the connection, so a caller holding session-keyed state
    /// watches this to know its state is stale.
    generation: u64,
}

impl Cdp {
    fn new(port: u16) -> Self {
        Cdp {
            port,
            ws: None,
            id: 0,
            events: VecDeque::new(),
            replies: HashMap::new(),
            sessions: HashMap::new(),
            loading: HashSet::new(),
            generation: 0,
        }
    }

    fn connect(&mut self) -> Result<(), CdpFail> {
        if self.ws.is_some() {
            return Ok(());
        }
        let ver = chrome_http(self.port, "/json/version", "GET")
            .map_err(|e| match e {
                ScanErr::Scanner(m) => CdpFail::Lost(m),
                ScanErr::Py(_) => CdpFail::Lost("interrupted".into()),
            })?;
        let url = ver
            .get("webSocketDebuggerUrl")
            .and_then(Value::as_str)
            .ok_or_else(|| CdpFail::Lost("no webSocketDebuggerUrl".into()))?
            .to_string();
        let rest = url
            .strip_prefix("ws://")
            .ok_or_else(|| CdpFail::Lost(format!("unexpected ws url: {url}")))?;
        let (hostport, _path) = match rest.find('/') {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, "/"),
        };
        let (host, port) = match hostport.rsplit_once(':') {
            Some((h, p)) => (
                h.to_string(),
                p.parse::<u16>()
                    .map_err(|_| CdpFail::Lost(format!("bad ws port in {url}")))?,
            ),
            None => (hostport.to_string(), 80),
        };
        let sock_addr = format!("{host}:{port}")
            .parse()
            .map_err(|_| CdpFail::Lost(format!("bad ws host in {url}")))?;
        let stream = TcpStream::connect_timeout(&sock_addr, Duration::from_secs(5))
            .map_err(|e| CdpFail::Lost(format!("CDP dial failed: {e}")))?;
        stream.set_read_timeout(Some(Duration::from_secs(5))).ok();
        let (ws, _resp) = tungstenite::client(url.as_str(), stream)
            .map_err(|e| CdpFail::Lost(format!("CDP handshake refused: {e}")))?;
        self.ws = Some(ws);
        Ok(())
    }

    fn drop_conn(&mut self) {
        self.ws = None; // dropping the WebSocket closes the TcpStream
        self.sessions.clear();
        self.loading.clear();
        self.events.clear();
        self.replies.clear();
        self.generation += 1;
    }

    /// Keep an event for whoever is waiting on one.
    ///
    /// Anything nobody waits on is dropped here rather than buffered — see
    /// WAITED_METHODS. If the cap is somehow still reached, the OLDEST event
    /// goes, so the event that just arrived is always kept.
    fn stash(&mut self, m: Value) {
        let waited = m
            .get("method")
            .and_then(Value::as_str)
            .is_some_and(|name| WAITED_METHODS.contains(&name));
        if !waited {
            return;
        }
        self.note_load_state(&m);
        while self.events.len() >= EVENT_CAP {
            self.events.pop_front();
        }
        self.events.push_back(m);
    }

    /// Follow a session's main-frame navigation so `is_loading` can answer.
    ///
    /// Only the MAIN frame counts: a sub-frame navigating (an ad, an embed)
    /// does not mean the page the model is about to read is unfinished.
    fn note_load_state(&mut self, m: &Value) {
        let sess = match m.get("sessionId").and_then(Value::as_str) {
            Some(s) => s.to_string(),
            None => return,
        };
        match m.get("method").and_then(Value::as_str) {
            Some("Page.frameNavigated") => {
                let is_main = m
                    .get("params")
                    .and_then(|p| p.get("frame"))
                    .map(|f| f.get("parentId").is_none())
                    .unwrap_or(false);
                if is_main {
                    self.loading.insert(sess);
                }
            }
            Some("Page.loadEventFired") => {
                self.loading.remove(&sess);
            }
            _ => {}
        }
    }

    /// True while this tab's main frame has a navigation in flight.
    pub fn is_loading(&mut self, session: &str) -> bool {
        // Read whatever is already on the wire first, so the answer reflects
        // the browser now rather than the last time somebody happened to drain.
        self.drain(0);
        self.loading.contains(session)
    }

    /// Take every buffered event with this method name, oldest first.
    ///
    /// `session` scopes the take to ONE page. This socket is shared by every
    /// tab the agent has ever touched, so without it a sibling tab's event
    /// answers a wait meant for this one: another tab's `Page.loadEventFired`
    /// ends this tab's navigation wait early, and another tab's network
    /// chatter keeps `settle` from ever going quiet.
    ///
    /// `None` means "from any session", which is what cross-session discovery
    /// (`Target.attachedToTarget`, whose envelope names the PARENT session)
    /// actually wants.
    pub fn take_events(&mut self, method: &str, session: Option<&str>) -> Vec<Value> {
        let mut hit = Vec::new();
        self.events.retain(|e| {
            let matches = e.get("method").and_then(Value::as_str) == Some(method)
                && match session {
                    None => true,
                    Some(want) => e.get("sessionId").and_then(Value::as_str) == Some(want),
                };
            if matches {
                hit.push(e.clone());
                false
            } else {
                true
            }
        });
        hit
    }

    /// Drop every buffered event. Whatever is left after a wait is stale by
    /// definition, and left in place it grows for the life of the run.
    pub fn clear_events(&mut self) {
        self.events.clear();
    }

    /// Read what is waiting, stopping once the socket has been quiet for IDLE
    /// or `ms` total has elapsed — `ms` is a cap, not a sleep.
    ///
    /// The count-based `drain` above never blocks, which is right for a
    /// cosmetics pass tidying an idle socket. A loop WAITING for events (the
    /// scanner's settle) needs the opposite: give the wire a moment to speak,
    /// then come back. Spinning on a non-blocking drain would burn a core and
    /// still see nothing.
    pub fn drain_for(&mut self, ms: u64) {
        const IDLE: Duration = Duration::from_millis(60);
        let end = Instant::now() + Duration::from_millis(ms);
        loop {
            let left = end.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            let Some(ws) = self.ws.as_mut() else { return };
            if ws
                .get_ref()
                .set_read_timeout(Some(IDLE.min(left)))
                .is_err()
            {
                self.drop_conn();
                return;
            }
            match ws.read() {
                Ok(tungstenite::Message::Text(t)) => {
                    match serde_json::from_str::<Value>(t.as_ref()) {
                        Ok(m) if m.get("method").is_some() => self.stash(m),
                        Ok(m) => {
                            if let Some(id) = m.get("id").and_then(Value::as_u64) {
                                if self.replies.len() < EVENT_CAP {
                                    self.replies.insert(id, m);
                                }
                            }
                        }
                        Err(_) => continue,
                    }
                }
                Ok(_) => continue,
                Err(_) => break, // read timeout: the socket has gone quiet
            }
        }
    }

    /// Block until one `method` event arrives, or `timeout` runs out. True if
    /// it arrived. Used by navigation, which is the one action whose result
    /// the caller genuinely has to wait for.
    pub fn wait_event(&mut self, method: &str, session: Option<&str>, timeout: f64) -> bool {
        let deadline = Instant::now() + Duration::from_secs_f64(timeout);
        loop {
            if !self.take_events(method, session).is_empty() {
                return true;
            }
            if Instant::now() >= deadline || self.ws.is_none() {
                return false;
            }
            self.drain(250);
            if !self.take_events(method, session).is_empty() {
                return true;
            }
            sleep_s(0.02);
        }
    }

    pub fn rpc(
        &mut self,
        method: &str,
        params: Value,
        session: Option<&str>,
        timeout: f64,
    ) -> Result<Value, CdpFail> {
        self.connect()?;
        self.id += 1;
        let mid = self.id;
        let mut msg = json!({"id": mid, "method": method, "params": params});
        if let Some(s) = session {
            msg["sessionId"] = Value::String(s.to_string());
        }
        let ws = self.ws.as_mut().expect("connected above");
        ws.get_ref()
            .set_read_timeout(Some(Duration::from_secs_f64(timeout.max(0.001))))
            .ok();
        if ws
            .send(tungstenite::Message::Text(msg.to_string().into()))
            .is_err()
        {
            self.drop_conn();
            return Err(CdpFail::Lost(format!("{method}: connection lost")));
        }
        // The answer may already be in hand: a drain reads whatever is on the
        // wire, and that can include the reply to this very call.
        if let Some(m) = self.replies.remove(&mid) {
            if let Some(err) = m.get("error") {
                return Err(CdpFail::Clean(format!("{method} -> {err}")));
            }
            return Ok(m.get("result").cloned().unwrap_or(json!({})));
        }
        let deadline = Instant::now() + Duration::from_secs_f64(timeout);
        while Instant::now() < deadline {
            let ws = self.ws.as_mut().expect("still connected in loop");
            match ws.read() {
                Ok(tungstenite::Message::Text(t)) => {
                    let m: Value = match serde_json::from_str(t.as_ref()) {
                        Ok(v) => v,
                        Err(_) => continue,
                    };
                    // Anything carrying a `method` is an event, not our
                    // answer — keep it for a caller that is waiting on one.
                    if m.get("method").is_some() {
                        self.stash(m);
                        continue;
                    }
                    // Flatten-mode ids are scoped per session; ours are
                    // globally unique AND used strictly one-in-flight, so
                    // id+session matches exactly one reply.
                    if m.get("id").and_then(Value::as_u64) == Some(mid)
                        && m.get("sessionId").and_then(Value::as_str) == session
                    {
                        if let Some(err) = m.get("error") {
                            // A clean refusal — the frame stream is still in
                            // step, so the connection survives.
                            return Err(CdpFail::Clean(format!("{method} -> {err}")));
                        }
                        return Ok(m.get("result").cloned().unwrap_or(json!({})));
                    }
                    // Someone else's answer — hold it rather than drop it.
                    if let Some(other) = m.get("id").and_then(Value::as_u64) {
                        if self.replies.len() < EVENT_CAP {
                            self.replies.insert(other, m);
                        }
                    }
                }
                Ok(tungstenite::Message::Close(_)) => {
                    self.drop_conn();
                    return Err(CdpFail::Lost(format!("{method}: connection lost")));
                }
                Ok(_) => continue, // ping/pong/binary — tungstenite answers pings
                Err(_) => {
                    // Timeout or torn transport mid-frame: the stream can no
                    // longer be trusted to start at a frame boundary.
                    self.drop_conn();
                    return Err(CdpFail::Lost(format!("{method}: connection lost")));
                }
            }
        }
        // Deadline passed with the reply unread — it may yet arrive and
        // desync whoever reads next, so this connection is done too.
        self.drop_conn();
        Err(CdpFail::Lost(format!("{method} timed out")))
    }

    /// Read whatever is already waiting, so an idle socket can never back up
    /// between passes. Events are buffered, not dropped.
    pub fn drain(&mut self, cap: usize) {
        let Some(ws) = self.ws.as_mut() else { return };
        if ws.get_ref().set_nonblocking(true).is_err() {
            self.drop_conn();
            return;
        }
        let mut seen: Vec<Value> = Vec::new();
        for _ in 0..cap {
            match ws.read() {
                Ok(tungstenite::Message::Text(t)) => {
                    if let Ok(v) = serde_json::from_str::<Value>(t.as_ref()) {
                        if v.get("method").is_some() {
                            seen.push(v);
                        }
                    }
                }
                Ok(_) => continue,
                Err(tungstenite::Error::Io(e))
                    if e.kind() == std::io::ErrorKind::WouldBlock =>
                {
                    break;
                }
                Err(_) => {
                    self.drop_conn();
                    return;
                }
            }
        }
        for v in seen {
            self.stash(v);
        }
        if let Some(ws) = self.ws.as_mut() {
            ws.get_ref().set_nonblocking(false).ok();
        }
    }

    /// SessionId for a tab, dialing and attaching only when needed.
    ///
    /// This is THE session for that tab: the glow rides on it and every tool
    /// borrows it to act. Nothing else opens one.
    pub fn attach(&mut self, target_id: &str) -> Result<String, CdpFail> {
        if let Some(s) = self.sessions.get(target_id) {
            return Ok(s.clone());
        }
        let r = self.rpc(
            "Target.attachToTarget",
            json!({"targetId": target_id, "flatten": true}),
            None,
            5.0,
        )?;
        let s = r
            .get("sessionId")
            .and_then(Value::as_str)
            .ok_or_else(|| CdpFail::Lost("attach returned no sessionId".into()))?
            .to_string();
        self.sessions.insert(target_id.to_string(), s.clone());
        // Page.enable is what makes navigation events (loadEventFired) arrive
        // at all, and the glow's document-start script fire. Focus emulation
        // keeps a backgrounded tab believing it is focused, which matters the
        // moment several agents share one browser: only one tab can really be
        // in front, and pages that gate on focus would misbehave for the rest.
        // Both are best-effort — a session that refuses either is still
        // perfectly usable for reading and clicking.
        let _ = self.rpc("Page.enable", json!({}), Some(&s), 5.0);
        let _ = self.rpc(
            "Emulation.setFocusEmulationEnabled",
            json!({"enabled": true}),
            Some(&s),
            5.0,
        );
        Ok(s)
    }

    /// Evaluate an expression in the tab and hand back its value. The only
    /// things evaluated this way are the overlay's own entry points, which
    /// answer `undefined` on a page that has no overlay.
    pub fn eval(&mut self, session: &str, expression: String) -> Result<Value, CdpFail> {
        let r = self.rpc(
            "Runtime.evaluate",
            json!({"expression": expression, "returnByValue": true}),
            Some(session),
            5.0,
        )?;
        Ok(r.get("result")
            .and_then(|v| v.get("value"))
            .cloned()
            .unwrap_or(Value::Null))
    }

    pub fn forget(&mut self, target_id: &str) {
        self.sessions.remove(target_id);
    }
}

// ---------------------------------------------------------------------------
// Chrome launch
// ---------------------------------------------------------------------------

fn port_open(port: u16) -> bool {
    let addr = format!("127.0.0.1:{port}").parse();
    match addr {
        Ok(a) => TcpStream::connect_timeout(&a, Duration::from_millis(250)).is_ok(),
        Err(_) => false,
    }
}

/// How long Chrome gets to become driveable before we give up on it.
const READY_TIMEOUT: f64 = 30.0;

/// Viewport for a headless run, where there is no screen to maximise to.
const HEADLESS_WINDOW: &str = "1920,1080";

/// Whether Chrome can actually be DRIVEN, not merely dialled.
///
/// `port_open` does not answer the question it looks like it answers. Chrome
/// binds the debug port and accepts connections on it well before the DevTools
/// HTTP endpoint will serve anything — measured at ~3.1s on a cold headful
/// start, and longer on a cold disk. Returning on `port_open` alone hands that
/// gap to whoever speaks first, which is the first scan: it sits inside
/// `chrome_http` waiting on a socket that is accepting but silent, and the wait
/// gets reported to the user as scan time.
///
/// The honest gate is the two things every caller after this needs: a socket
/// url to connect to, and a page to attach to. Nothing further is worth
/// waiting for — the tab Chrome starts on is about:blank, which is already
/// loaded by the time it is listed.
fn browser_ready(port: u16) -> bool {
    let Ok(ver) = chrome_http(port, "/json/version", "GET") else {
        return false;
    };
    ver.get("webSocketDebuggerUrl")
        .and_then(Value::as_str)
        .is_some_and(|u| !u.is_empty())
        && !page_targets(port).is_empty()
}

/// Block until `browser_ready`, or `secs` runs out. True if it came up.
///
/// A deadline, not a try count: `chrome_http` blocks for up to its own 5s read
/// timeout against a port that is accepting but silent, so counting attempts
/// would bound this at minutes rather than seconds.
fn await_browser_ready(port: u16, secs: f64) -> SResult<bool> {
    let end = Instant::now() + Duration::from_secs_f64(secs);
    loop {
        if browser_ready(port) {
            return Ok(true);
        }
        if Instant::now() >= end {
            return Ok(false);
        }
        check_py_signals()?;
        sleep_s(0.05);
    }
}

/// Every place a Chrome-family browser may be installed, most preferred first.
fn chrome_candidates() -> Vec<PathBuf> {
    if !cfg!(windows) {
        return CHROME_PATHS_UNIX.iter().map(PathBuf::from).collect();
    }
    let roots: Vec<String> = ["ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"]
        .iter()
        .filter_map(|k| std::env::var(k).ok())
        .collect();
    let mut v = Vec::with_capacity(CHROME_RELATIVE_WIN.len() * roots.len());
    // Browser-major order: Chrome under any root beats Chromium under any root.
    for rel in CHROME_RELATIVE_WIN {
        for root in &roots {
            v.push(PathBuf::from(root).join(rel));
        }
    }
    v
}

fn find_chrome() -> SResult<PathBuf> {
    for path in chrome_candidates() {
        if path.exists() {
            return Ok(path);
        }
    }
    // Linux installs usually expose a launcher on PATH rather than a fixed
    // location. which() appends .exe on Windows, so these cost nothing there.
    which("google-chrome")
        .or_else(|| which("chromium"))
        .or_else(|| which("chrome"))
        .ok_or_else(|| ScanErr::s("Chrome not found — install Google Chrome to use web mode"))
}

pub fn launch_chrome_impl(port: u16, headless: bool) -> SResult<bool> {
    if port_open(port) {
        // Attached, not launched — but a browser someone started a moment ago
        // is in exactly the state `browser_ready` describes, so this path has
        // to wait it out too rather than assume an open port means a usable one.
        if !await_browser_ready(port, READY_TIMEOUT)? {
            return Err(ScanErr::s(format!(
                "Chrome holds port {port} but its DevTools endpoint never answered"
            )));
        }
        println!("Attached to Chrome already on port {port}");
        return Ok(false);
    }

    let profile = home_dir().join(".autouse").join(format!("chrome-{port}"));
    std::fs::create_dir_all(&profile)
        .map_err(|e| ScanErr::s(format!("could not create {}: {e}", profile.display())))?;
    let chrome = find_chrome()?;
    let mut cmd = Command::new(chrome);
    cmd.arg(format!("--remote-debugging-port={port}"))
        .arg(format!("--user-data-dir={}", profile.display()))
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .arg("--disable-backgrounding-occluded-windows")
        .arg("--disable-renderer-backgrounding")
        .arg("--disable-background-timer-throttling");
    // The window is not just where the agent works — it IS what the model
    // sees. The screenshot is the viewport, so Chrome's default restore size
    // costs the model page area and elements on every single scan. Ask for
    // the whole screen rather than accept it.
    if headless {
        // Nothing to maximise against without a display, so name a size.
        cmd.arg(format!("--window-size={HEADLESS_WINDOW}"));
    } else {
        cmd.arg("--start-maximized");
    }
    if headless {
        cmd.arg("--headless=new");
    }
    // Start ON about:blank rather than letting Chrome show its New Tab page,
    // so the very first surface the agent sees is inert.
    cmd.arg(BLANK_URL);
    cmd.stdout(Stdio::null()).stderr(Stdio::null());
    cmd.spawn()
        .map_err(|e| ScanErr::s(format!("could not launch Chrome: {e}")))?;

    if await_browser_ready(port, READY_TIMEOUT)? {
        let mode = if headless { "headless" } else { "headful" };
        println!("Chrome launched on port {port} ({mode})");
        return Ok(true);
    }
    Err(ScanErr::s(format!("Chrome did not open the debug port {port}")))
}

// ---------------------------------------------------------------------------
// The scanner process + its scan state. Pure Rust, no GIL held during I/O —
// the pyclass wrapper below releases the GIL around every call in here.
// ---------------------------------------------------------------------------

pub struct ScannerInner {
    pub port: u16,
    pub out_dir: PathBuf,
    logo_page: PathBuf,
    glow_css: PathBuf,
    glow_js: PathBuf,
    /// Scan tuning: the embedded defaults with tree/element.config.json
    /// merged over them, loaded once and reloadable by hand.
    cfg: Value,
    cfg_path: PathBuf,
    /// Sessions whose scan domains are already on. A session is long-lived and
    /// enabling them is a round trip, so it happens once per tab.
    prepared: HashSet<String>,
    /// Whether scans come back with numbered marks painted on.
    marks: bool,
    /// Scans so far, for DEBUG's per-scan folders.
    scan_count: usize,
    pub tree_text: String,
    pub image_b64: Option<String>,
    pub all_tabs: String,
    pub url: String,
    pub mapping: serde_json::Map<String, Value>,
    /// device px per CSS px, from the most recent scan.
    pub dpr: f64,
    /// Wall time of the most recent scan_elements(), in seconds.
    pub last_scan_seconds: f64,
    cdp: Cdp,
    /// sessionId -> targetId for tabs whose glow script is registered.
    glow_armed: HashMap<String, String>,
    glow_gen: u64,
    /// Parallel-run mode: this agent creates and drives exactly ONE tab of
    /// its own in a browser shared with other agents. Tab listing, cosmetics
    /// and the current-target lookup are all scoped to that tab.
    single_tab: bool,
    /// The tabs of the CURRENT listing, in the order `<all_tabs>` shows them.
    ///
    /// Filled by `read_tabs`, so the ids and the text the model reads come out
    /// of ONE listing. `[n]` resolves against this and never against a fresh
    /// /json/list: Chrome orders that most-recently-USED and `bring_to_front`
    /// reorders it, so re-reading it between the scan and the action can hand
    /// back a different tab than the one the model picked. Bounds-checking the
    /// old list and then indexing the new one is how `close_tab` could close a
    /// tab nobody asked to close.
    tab_ids: Vec<String>,
    /// Every tab this agent has seen, in the order it first saw them — the
    /// order `<all_tabs>` numbers against, and the only stable one available.
    tab_order: Vec<String>,
    /// The tab this agent is driving, by CDP target id.
    ///
    /// Tracked in BOTH modes, and authoritative: the scanner is bound to it by
    /// id, every tool acts on it, and the glow decorates it. It used to be
    /// single-tab-only, with multi-tab runs guessing "whichever target
    /// /json/list puts first" — a guess that was only ever right because the
    /// scanner had just called bringToFront on its own tab.
    tab_id: Option<String>,
}

// No Drop impl: the pump threads own the read ends now, and they exit on EOF
// when the child's pipes close. There is no raw fd left to hand back.

impl ScannerInner {
    pub fn new(browser_dir: &PathBuf, port: u16, out_dir: Option<PathBuf>, single_tab: bool) -> Self {
        // web/browser -> parent=web; web/tree holds the scanner crate, and
        // parent.parent=Auto_Use holds the shared logo.
        let web_dir = browser_dir.parent().map(|p| p.to_path_buf()).unwrap_or_default();
        let cfg_path = web_dir.join("tree").join("element.config.json");
        let logo_page = web_dir
            .parent()
            .map(|p| p.to_path_buf())
            .unwrap_or_default()
            .join("logo")
            .join("logo.html");
        ScannerInner {
            port,
            // Scan output is run data, not source: it lives under the CWD's
            // debug/ — the folder the agent wipes at the start of every run —
            // never inside web/tree.
            out_dir: out_dir.unwrap_or_else(|| {
                std::env::current_dir()
                    .unwrap_or_default()
                    .join("debug")
                    .join("scans")
            }),
            logo_page,
            glow_css: browser_dir.join("glow").join("glow.css"),
            glow_js: browser_dir.join("glow").join("glow.js"),
            cfg: element::load_config(&cfg_path.display().to_string()),
            cfg_path,
            prepared: HashSet::new(),
            marks: true,
            scan_count: 0,
            tree_text: String::new(),
            image_b64: None,
            all_tabs: String::new(),
            url: String::new(),
            mapping: serde_json::Map::new(),
            dpr: 1.0,
            last_scan_seconds: 0.0,
            cdp: Cdp::new(port),
            glow_armed: HashMap::new(),
            glow_gen: 0,
            single_tab,
            tab_ids: Vec::new(),
            tab_order: Vec::new(),
            tab_id: None,
        }
    }

    // -- the scanned tab ---------------------------------------------------

    /// Make sure there IS a tab to read, and that this agent is bound to it.
    ///
    /// There is no process to start any more: scanning is a call on the
    /// session below, so "start" means only "have somewhere to point it".
    pub fn start(&mut self) -> SResult<()> {
        let alive = self.tab_id.as_deref().is_some_and(|id| tab_exists(self.port, id));
        if !alive {
            self.tab_id = if self.single_tab {
                // Parallel mode: this agent drives ONE tab of its own. Create
                // it (or re-create it if it died) and never point at another
                // agent's tab.
                Some(create_tab_impl(self.port)?)
            } else {
                // Shared with the human: adopt the tab the browser is already
                // showing rather than opening another one on top of it.
                ensure_tab_impl(self.port)?;
                page_targets(self.port)
                    .first()
                    .map(|t| target_id_of(t).to_string())
                    .filter(|id| !id.is_empty())
            };
            self.prepared.clear();
        }
        std::fs::create_dir_all(&self.out_dir)
            .map_err(|e| ScanErr::s(format!("could not create {}: {e}", self.out_dir.display())))?;

        // If the surface we landed on is blank, put our own page on it. Only a
        // BLANK surface is replaced; a real page someone left open is never
        // touched.
        let url = self.current_tab_url();
        if blank_url(&url) {
            match self.show_blank_page() {
                Ok(_) => {}
                Err(ScanErr::Scanner(_)) => {} // fails louder later
                Err(e) => return Err(e),
            }
        }

        // Glow from second zero: the browser should read as driven the moment
        // it is driven — blank tab included.
        self.glow_tabs();
        Ok(())
    }

    /// Let the tab go. Chrome stays up — the browser outlives the run — and so
    /// does the tab; only this side's session is dropped.
    pub fn stop(&mut self) {
        self.prepared.clear();
        self.cdp.drop_conn();
    }

    /// Reload tree/element.config.json over the embedded defaults.
    pub fn reload_config(&mut self) {
        self.cfg = element::load_config(&self.cfg_path.display().to_string());
    }

    /// Numbered marks on the screenshot, on or off.
    pub fn set_marks(&mut self, on: bool) {
        self.marks = on;
    }

    // -- scan --------------------------------------------------------------

    /// Borrow the session for a read of the driven tab.
    ///
    /// The scan domains go on the first time a given tab is read: a session
    /// outlives any one scan, so enabling them per scan would be a round trip
    /// paid over and over for nothing.
    fn on_page<T>(
        &mut self,
        f: impl FnOnce(&mut Cdp, &str) -> Result<T, String>,
    ) -> SResult<T> {
        let tid = self.current_target_id()?;
        let sess = self.cdp.attach(&tid)?;
        if !self.prepared.contains(&sess) {
            element::prepare_session(&mut self.cdp, &sess).map_err(ScanErr::s)?;
            self.prepared.insert(sess.clone());
        }
        // Whatever is buffered predates this scan and says nothing about it —
        // and `settle` is about to count events to decide the page is quiet.
        self.cdp.clear_events();
        f(&mut self.cdp, &sess).map_err(ScanErr::s)
    }

    /// Everything scan_elements does except the frontend callback, the glow
    /// pass and the timing stamp — those run in the pyclass wrapper so the
    /// callback fires without this lock held.
    ///
    /// The scan is a CALL now, on the session this side already holds. It used
    /// to be a line written to a subprocess whose answer came back through
    /// three files on disk, which meant the tree, the geometry and the
    /// screenshot could each be from a different moment if anything went wrong
    /// between writing and reading them.
    pub fn scan_core(&mut self) -> SResult<String> {
        self.start()?;
        // The click box is per-action and must not be frozen into the scan's
        // screenshot. The glow stays up: it is a thin, blurred edge the model
        // can ignore.
        self.unflash();

        let (marks, cfg) = (self.marks, self.cfg.clone());
        let out = self.on_page(|cdp, sess| element::scan_page(cdp, sess, &cfg, marks))?;

        self.url = out.url.clone();
        self.tree_text = prune_empty_containers(out.tree.trim());
        self.image_b64 = out
            .screenshot
            .as_deref()
            .map(|bytes| base64::engine::general_purpose::STANDARD.encode(bytes));
        self.mapping = parse_mapping(&self.tree_text);
        self.load_hits(&out);
        self.all_tabs = self.read_tabs();

        self.scan_count += 1;
        let summary = format!(
            "{} interactive, {} ms settle, {} sessions, {} frames skipped, \
             {} occluded, {} noise",
            out.count, out.settled_ms, out.sessions, out.skipped, out.occluded, out.noise
        );
        self.write_scan_files(&out, &summary);
        Ok(summary)
    }

    /// This scan on disk: one always-overwritten set under `out_dir`.
    ///
    /// Nothing reads it back — the scan's results are already in hand. It is
    /// written to be LOOKED at, which is why a failure here is swallowed: a
    /// full disk must not cost the agent a step.
    fn write_scan_files(&self, out: &element::ScanOut, summary: &str) {
        let header = format!("# {}\n# {summary}\n\n", out.url);
        let _ = std::fs::write(
            self.out_dir.join("tree.txt"),
            format!("{header}{}\n", out.tree),
        );
        // Geometry beside the tree, NOT inside it: tree.txt goes to the model
        // verbatim and coordinates would be noise there.
        let mut hits = serde_json::Map::new();
        for (i, r) in &out.hits {
            hits.insert(i.to_string(), json!([r[0], r[1], r[2], r[3]]));
        }
        let _ = std::fs::write(
            self.out_dir.join("hits.json"),
            serde_json::to_string(&json!({"dpr": out.dpr, "hits": hits})).unwrap_or_default(),
        );
        if let Some(bytes) = out.screenshot.as_deref() {
            let _ = std::fs::write(self.out_dir.join("shot.jpg"), bytes);
        }
        if element::DEBUG {
            let _ = element::write_debug(
                self.scan_count,
                &header,
                &out.tree,
                out.screenshot.as_deref(),
            );
        }
    }

    /// Fold the scan's geometry (DEVICE px) into the element mapping,
    /// converted to CSS px exactly once, here.
    fn load_hits(&mut self, out: &element::ScanOut) {
        self.dpr = if out.dpr == 0.0 { 1.0 } else { out.dpr };
        for (idx, rect) in &out.hits {
            let Some(entry) = self.mapping.get_mut(&idx.to_string()) else { continue };
            let [x, y, w, h] = rect.map(|v| v / self.dpr);
            entry["rect"] = json!([x, y, w, h]);
            entry["point"] = json!([x + w / 2.0, y + h / 2.0]);
        }
    }

    /// One `<all_tabs>` line for a tab: `[n] url (current) - title`.
    fn tab_line(&self, n: usize, target: &Value, current: bool) -> String {
        let url = target.get("url").and_then(Value::as_str).unwrap_or("");
        let title = target.get("title").and_then(Value::as_str).unwrap_or("");
        let mut line = format!("[{n}] {}", if url.is_empty() { BLANK_URL } else { url });
        if current {
            line.push_str(" (current)");
        }
        if !title.is_empty() {
            line.push_str(&format!(" - {title}"));
        }
        line
    }

    /// `<all_tabs>` body: one line per open tab, the driven one marked.
    ///
    /// Read straight from Chrome's /json/list — the SAME list close_tab and
    /// switch_tab index into, so the model-facing [n] and the tab those tools
    /// act on cannot drift apart. It used to come from the scanner
    /// subprocess's own `t` command and get regex-parsed back out of its
    /// stdout — a second listing of the same thing that only agreed by luck.
    /// Chrome's open tabs in a STABLE, left-to-right order.
    ///
    /// /json/list is most-recently-USED order, not tab-strip order. Measured:
    /// with four tabs open it lists them newest-first, and activating the
    /// OLDEST moves that one to the head. Numbering straight off it therefore
    /// renumbers every tab each time one is brought to the front — which
    /// `bind_tab` does on every single scan — so the tab the model called [2]
    /// last step is a different tab this step, and it is told nothing.
    ///
    /// Chrome exposes no tab-strip index over CDP, so the order is KEPT here
    /// rather than asked for: an id is appended the first time it is seen and
    /// never moves again, and a closed tab closes the gap behind it. A new tab
    /// opens on the right and takes the next number up, which is what the
    /// model is told to expect.
    fn ordered_tabs(&mut self) -> Vec<Value> {
        let live = self.open_tabs();
        // Reversed, because Chrome hands them over newest-first: tabs seen for
        // the first time this pass then land oldest-first, and the very first
        // listing of a browser we did not open reads left-to-right too.
        let mut fresh: Vec<String> =
            live.iter().map(|t| target_id_of(t).to_string()).rev().collect();
        fresh.retain(|id| !id.is_empty());
        self.tab_order.retain(|id| fresh.contains(id));
        for id in fresh {
            if !self.tab_order.contains(&id) {
                self.tab_order.push(id);
            }
        }
        self.tab_order
            .iter()
            .filter_map(|id| live.iter().find(|t| target_id_of(t) == id).cloned())
            .collect()
    }

    pub fn read_tabs(&mut self) -> String {
        let current = self.tab_id.clone().unwrap_or_default();
        let targets = self.ordered_tabs();
        // The ids behind the lines, captured in the same pass that renders
        // them. This is the whole point: one listing, two views of it.
        self.tab_ids = targets.iter().map(|t| target_id_of(t).to_string()).collect();
        if self.single_tab {
            // One dedicated tab: the model always sees exactly its own tab as
            // [1]. Other agents' tabs in the shared browser never appear.
            return match targets.first() {
                Some(t) => self.tab_line(1, t, true),
                None => String::new(),
            };
        }
        targets
            .iter()
            .enumerate()
            .map(|(i, t)| self.tab_line(i + 1, t, target_id_of(t) == current))
            .collect::<Vec<_>>()
            .join("\n")
    }

    /// The tab `[n]` from the listing the model was shown.
    ///
    /// Three different failures, each worth telling apart: a number that is not
    /// a tab number at all, one that was never on the list, and one that was on
    /// it but whose tab has closed since.
    pub fn tab_target(&self, index: i64) -> SResult<String> {
        if index < 1 {
            return Err(ScanErr::s(format!(
                "tab numbers start at [1] - there is no tab [{index}]"
            )));
        }
        let id = self.tab_ids.get((index - 1) as usize).ok_or_else(|| {
            ScanErr::s(format!(
                "no tab [{index}] - the tab list has {} tab(s)",
                self.tab_ids.len()
            ))
        })?;
        if !tab_exists(self.port, id) {
            return Err(ScanErr::s(format!(
                "tab [{index}] has been closed since that list was made - read \
                 the fresh <all_tabs> in the next input before acting on a tab"
            )));
        }
        Ok(id.clone())
    }

    /// How many tabs that listing showed.
    pub fn tab_count(&self) -> usize {
        self.tab_ids.len()
    }

    /// Where to go when the driven tab is closed: the neighbour that now holds
    /// its slot in the listing, else the nearest tab still open on either side
    /// of it, else anything at all.
    pub fn neighbour_tab(&self, index: i64, closed: &str) -> Option<String> {
        let i = ((index - 1).max(0) as usize).min(self.tab_ids.len());
        let after = self.tab_ids.iter().skip(i + 1);
        let before = self.tab_ids[..i].iter().rev();
        after
            .chain(before)
            .find(|id| id.as_str() != closed && tab_exists(self.port, id))
            .cloned()
            .or_else(|| {
                page_targets(self.port)
                    .iter()
                    .map(|t| target_id_of(t).to_string())
                    .find(|id| id != closed)
            })
    }

    /// Re-read the listing, so `<all_tabs>` and the ids `[n]` resolves against
    /// both reflect a tab that was just opened or closed.
    pub fn refresh_tabs(&mut self) {
        self.all_tabs = self.read_tabs();
    }

    /// The tabs this agent may see and act on, in Chrome's own order. In
    /// single-tab mode that is exactly one tab: its own.
    pub fn open_tabs(&self) -> Vec<Value> {
        let mut targets = page_targets(self.port);
        if self.single_tab {
            let mine = self.tab_id.clone().unwrap_or_default();
            targets.retain(|t| target_id_of(t) == mine);
        }
        targets
    }

    /// Url of the tab this agent is driving, read live from Chrome's list.
    ///
    /// Live, not `self.url`: that one is the url as of the last SCAN, and an
    /// action that navigates between scans leaves it a step behind.
    /// It used to be recovered by regex out of the rendered `<all_tabs>`
    /// text — parsing a string this side had just formatted.
    pub fn current_tab_url(&self) -> String {
        let Ok(id) = self.current_target_id() else {
            return String::new();
        };
        page_targets(self.port)
            .iter()
            .find(|t| target_id_of(t) == id)
            .and_then(|t| t.get("url").and_then(Value::as_str))
            .unwrap_or("")
            .to_string()
    }

    /// Current page's host — the browser's answer to macOS's app name.
    pub fn application_name(&self) -> String {
        let netloc = self
            .url
            .find("://")
            .map(|i| {
                self.url[i + 3..]
                    .split(['/', '?', '#'])
                    .next()
                    .unwrap_or("")
            })
            .unwrap_or("");
        if netloc.is_empty() {
            "browser".to_string()
        } else {
            netloc.to_string()
        }
    }

    /// [x, y, w, h] of the page, in CSS px — element [1] of the last scan.
    pub fn viewport_rect(&self) -> Option<[f64; 4]> {
        let rect = self.mapping.get("1")?.get("rect")?.as_array()?;
        if rect.len() != 4 {
            return None;
        }
        let mut out = [0.0f64; 4];
        for (i, v) in rect.iter().enumerate() {
            out[i] = v.as_f64()?;
        }
        Some(out)
    }

    // -- cosmetics -----------------------------------------------------------
    //
    // Best-effort dressing of the browser the human watches; none of it may
    // ever cost a scan, so every pass swallows its failures and moves on.

    /// Make Chrome accept the painted logo page's tab icon: touching the icon
    /// link's href after the paint re-fires the announcement at a moment
    /// Chrome accepts it. Plain DOM protocol: no script runs in the page.
    fn nudge_favicon(&mut self, sess: &str) -> Result<(), CdpFail> {
        let root = self.cdp.rpc("DOM.getDocument", json!({"depth": 0}), Some(sess), 5.0)?;
        let root_id = root
            .get("root")
            .and_then(|r| r.get("nodeId"))
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let node = self
            .cdp
            .rpc(
                "DOM.querySelector",
                json!({"nodeId": root_id, "selector": "link[rel~=icon]"}),
                Some(sess),
                5.0,
            )?
            .get("nodeId")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        if node == 0 {
            return Ok(());
        }
        let attrs = self
            .cdp
            .rpc("DOM.getAttributes", json!({"nodeId": node}), Some(sess), 5.0)?;
        let attrs = attrs
            .get("attributes")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut href = String::new();
        let mut i = 0;
        while i + 1 < attrs.len() {
            if attrs[i].as_str() == Some("href") {
                href = attrs[i + 1].as_str().unwrap_or("").to_string();
                break;
            }
            i += 2;
        }
        if href.is_empty() {
            return Ok(());
        }
        self.cdp.rpc(
            "DOM.removeAttribute",
            json!({"nodeId": node, "name": "href"}),
            Some(sess),
            5.0,
        )?;
        self.cdp.rpc(
            "DOM.setAttributeValue",
            json!({"nodeId": node, "name": "href", "value": href}),
            Some(sess),
            5.0,
        )?;
        Ok(())
    }

    /// Target id of the tab this agent is driving.
    ///
    /// The tracked id, in both modes. This used to guess in multi-tab runs —
    /// "whichever target /json/list puts first", which is Chrome's
    /// most-recently-USED order and was only ever right because the scanner
    /// had just called bringToFront on its own tab. A user clicking another
    /// tab between steps was enough to break it.
    pub fn current_target_id(&self) -> Result<String, CdpFail> {
        self.tab_id
            .clone()
            .filter(|id| !id.is_empty())
            .ok_or_else(|| CdpFail::Lost("no tab bound yet".into()))
    }

    /// Bloom over `rect` (CSS px) — the neon mark on whatever was clicked or
    /// typed into. Returns whether it drew; a bloom that cannot be drawn must
    /// never stop the action it was going to decorate.
    pub fn flash(&mut self, rect: &[f64; 4]) -> bool {
        let [x, y, w, h] = rect;
        let attempt = (|| -> Result<bool, CdpFail> {
            let tid = self.current_target_id()?;
            let sess = self.cdp.attach(&tid)?;
            let r = self.cdp.rpc(
                "Runtime.evaluate",
                json!({
                    "expression": format!(
                        "window.__autouseBox && window.__autouseBox({x:.1},{y:.1},{w:.1},{h:.1})"),
                    "returnByValue": true
                }),
                Some(&sess),
                5.0,
            )?;
            Ok(r.get("result")
                .and_then(|v| v.get("value"))
                .map(truthy)
                .unwrap_or(false))
        })();
        attempt.unwrap_or(false)
    }

    /// Clear the bloom. Also called before every scan, so it can never be
    /// frozen into a screenshot and mistaken for part of the page.
    pub fn unflash(&mut self) {
        let sessions: Vec<String> = self.cdp.sessions.values().cloned().collect();
        for sess in sessions {
            let _ = self.cdp.rpc(
                "Runtime.evaluate",
                json!({
                    "expression": "window.__autouseBoxHide && window.__autouseBoxHide()",
                    "returnByValue": true
                }),
                Some(&sess),
                5.0,
            );
        }
    }

    /// Where the surfaces under `rect`'s centre currently sit, as
    /// [innerX, innerY, pageX, pageY]; None when it cannot be read.
    pub fn scroll_probe(&mut self, rect: &[f64; 4]) -> Option<Value> {
        let [x, y, w, h] = rect;
        let (cx, cy) = (x + w / 2.0, y + h / 2.0);
        let attempt = (|| -> Result<Value, CdpFail> {
            let tid = self.current_target_id()?;
            let sess = self.cdp.attach(&tid)?;
            let r = self.cdp.rpc(
                "Runtime.evaluate",
                json!({
                    "expression": format!(
                        "window.__autouseScrollProbe && window.__autouseScrollProbe({cx:.1},{cy:.1})"),
                    "returnByValue": true
                }),
                Some(&sess),
                5.0,
            )?;
            let value = r.get("result").and_then(|v| v.get("value"));
            if let Some(Value::Array(items)) = value {
                if items.len() == 4 {
                    return Ok(Value::Array(items.clone()));
                }
            }
            // No overlay: the page's own offsets still catch a page scroll.
            let m = self.cdp.rpc("Page.getLayoutMetrics", json!({}), Some(&sess), 5.0)?;
            let vp = m
                .get("cssVisualViewport")
                .or_else(|| m.get("visualViewport"))
                .cloned()
                .unwrap_or(json!({}));
            let px = vp.get("pageX").and_then(Value::as_f64).unwrap_or(0.0);
            let py_ = vp.get("pageY").and_then(Value::as_f64).unwrap_or(0.0);
            Ok(json!([-1, -1, py_round(px), py_round(py_)]))
        })();
        attempt.ok()
    }

    /// Arm every open tab so its every document glows, and give a blank tab
    /// still showing the default globe its favicon nudge. Idempotent and
    /// cheap, which is why the few callers can just call it.
    pub fn glow_tabs(&mut self) {
        let src = glow_source(&self.glow_css, &self.glow_js).to_string();
        if src.is_empty() {
            return;
        }
        // A hung-up connection took its sessions — and everything registered
        // inside them — with it, so those records are worthless.
        if self.cdp.generation != self.glow_gen {
            self.glow_armed.clear();
            self.glow_gen = self.cdp.generation;
        }
        let mut targets = page_targets(self.port);
        if self.single_tab {
            // Decorate only this agent's own tab — never inject scripts into
            // (or hold sessions on) tabs that belong to parallel agents.
            let mine = self.tab_id.clone().unwrap_or_default();
            targets.retain(|t| t.get("id").and_then(Value::as_str) == Some(mine.as_str()));
        }
        self.cdp.drain(500);
        for t in targets {
            let tid = t.get("id").and_then(Value::as_str).unwrap_or("").to_string();
            if tid.is_empty() {
                continue;
            }
            let mut sess: Option<String> = None;
            let attempt = (|| -> Result<(), CdpFail> {
                let s = self.cdp.attach(&tid)?;
                sess = Some(s.clone());
                if !self.glow_armed.contains_key(&s) {
                    // Page.enable is not optional and must STAY on: without it
                    // the registered script never fires. Its events are drained.
                    self.cdp.rpc("Page.enable", json!({}), Some(&s), 5.0)?;
                    self.cdp.rpc(
                        "Page.addScriptToEvaluateOnNewDocument",
                        json!({"source": src}),
                        Some(&s),
                        5.0,
                    )?;
                    self.glow_armed.insert(s.clone(), tid.clone());
                    // The document already open predates the registration.
                    self.cdp.rpc(
                        "Runtime.evaluate",
                        json!({"expression": src, "returnByValue": true}),
                        Some(&s),
                        5.0,
                    )?;
                }
                let url = t.get("url").and_then(Value::as_str).unwrap_or("");
                let has_favicon = t
                    .get("faviconUrl")
                    .map(|v| truthy(v))
                    .unwrap_or(false);
                if blank_url(url) && !has_favicon {
                    self.nudge_favicon(&s)?;
                }
                Ok(())
            })();
            if attempt.is_err() {
                // Tabs come and go mid-pass by nature; the next call rebuilds
                // whatever still exists.
                if let Some(s) = sess {
                    self.glow_armed.remove(&s);
                }
                self.cdp.forget(&tid);
            }
        }
    }

    pub fn drop_cosmetics(&mut self) {
        self.cdp.drop_conn();
    }

    // -- the shared session --------------------------------------------------
    //
    // ONE CDP session per tab, dialled by this side and lent out. Every tool
    // that acts on the page borrows it for the length of a single operation
    // and does its own protocol work; the scanner borrows the same one to
    // read. Nothing else opens a connection to Chrome — there is exactly one
    // socket, and this side owns it.

    /// Borrow this agent's session, attached to the tab it is driving.
    ///
    /// The closure gets the live `Cdp` and the tab's sessionId. Failures come
    /// back as ordinary scanner errors, so a tool can report "the tab went
    /// away" the same way it reports anything else.
    pub fn with_tab<T>(
        &mut self,
        f: impl FnOnce(&mut Cdp, &str) -> Result<T, CdpFail>,
    ) -> SResult<T> {
        let tid = self.current_target_id()?;
        let sess = self.cdp.attach(&tid)?;
        Ok(f(&mut self.cdp, &sess)?)
    }

    /// Point this agent at another tab: the scanner re-binds to it, the
    /// window follows, and the glow is re-armed on it.
    pub fn bind_tab(&mut self, target_id: &str) -> SResult<()> {
        if !tab_exists(self.port, target_id) {
            return Err(ScanErr::s("that tab is no longer open"));
        }
        self.tab_id = Some(target_id.to_string());
        // Nothing to tell a subprocess any more: the next scan reads whatever
        // tab this points at, over the session already open on it.
        self.bring_to_front();
        self.glow_tabs();
        Ok(())
    }

    /// Bring the driven tab to the front of the browser window, so a headful
    /// run visibly follows the agent. Best-effort: a tab that refuses to come
    /// forward is still perfectly readable and clickable.
    pub fn bring_to_front(&mut self) {
        let _ = self.with_tab(|cdp, sess| {
            cdp.rpc("Page.bringToFront", json!({}), Some(sess), 5.0)
        });
    }

    /// Forget a tab's session — it closed, and everything inside it died.
    pub fn forget_tab(&mut self, target_id: &str) {
        self.cdp.forget(target_id);
    }

    /// Adopt a freshly created tab: bind to it and dress it before anything
    /// navigates, so its first real page glows from its first paint.
    pub fn adopt_tab(&mut self, target_id: &str) -> SResult<()> {
        self.bind_tab(target_id)
    }

    /// Open a tab and drive it. The caller navigates it afterwards.
    pub fn create_tab(&mut self) -> SResult<String> {
        let id = create_tab_impl(self.port)?;
        self.adopt_tab(&id)?;
        // The listing changed under the model's feet — re-take it now rather
        // than leaving `[n]` pointing into a list that predates this tab.
        self.refresh_tabs();
        Ok(id)
    }

    // -- page dressing ---------------------------------------------------

    /// Paint the logo into the driven tab, leaving the address bar alone.
    ///
    /// Page.setDocumentContent, not a navigation and not script execution: a
    /// file:// url would show its whole path in the address bar, while this
    /// swaps only the content and leaves about:blank in the bar.
    pub fn show_blank_page(&mut self) -> SResult<String> {
        let html = blank_html_impl(&self.logo_page);
        if html.is_empty() {
            return Ok(String::new());
        }
        self.with_tab(|cdp, sess| {
            let fid = cdp
                .rpc("Page.getFrameTree", json!({}), Some(sess), 5.0)?
                .get("frameTree")
                .and_then(|f| f.get("frame"))
                .and_then(|f| f.get("id"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if fid.is_empty() {
                return Err(CdpFail::Lost("no frame to render into".into()));
            }
            cdp.rpc(
                "Page.setDocumentContent",
                json!({"frameId": fid, "html": html}),
                Some(sess),
                5.0,
            )
        })?;
        // The paint carries the icon link but Chrome dropped its announcement
        // mid-rewrite — glow_tabs re-fires it.
        self.glow_tabs();
        Ok(String::new())
    }
}

/// Python truthiness for a JSON value.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python round() — banker's rounding, returns an integer Value.
fn py_round(v: f64) -> i64 {
    v.round_ties_even() as i64
}

// ---------------------------------------------------------------------------
// Regexes (compiled once)
// ---------------------------------------------------------------------------

fn re_mapping_line() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"^\s*\[(\d+)\]\s*<([a-zA-Z0-9_-]+)(.*)$").unwrap())
}

fn re_name() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r">([^<]*)</").unwrap())
}

fn re_role() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r#"role="([^"]*)""#).unwrap())
}

/// `[N] <tag ...>name</tag>` lines -> {"N": {...}}. Only ids the model was
/// actually shown land here, so the controller can reject a hallucinated id
/// before it reaches the browser.
fn parse_mapping(tree_text: &str) -> serde_json::Map<String, Value> {
    let mut mapping = serde_json::Map::new();
    for line in tree_text.lines() {
        let Some(m) = re_mapping_line().captures(line) else { continue };
        let idx = m.get(1).map(|g| g.as_str()).unwrap_or("");
        let tag = m.get(2).map(|g| g.as_str()).unwrap_or("");
        let rest = m.get(3).map(|g| g.as_str()).unwrap_or("");
        let name = re_name()
            .captures(rest)
            .and_then(|c| c.get(1))
            .map(|g| g.as_str().trim().to_string())
            .unwrap_or_default();
        let role = re_role()
            .captures(rest)
            .and_then(|c| c.get(1))
            .map(|g| g.as_str().to_string())
            .unwrap_or_default();
        let index: i64 = idx.parse().unwrap_or(0);
        mapping.insert(
            idx.to_string(),
            json!({
                "index": index,
                "tag": tag,
                "role": role,
                "name": name,
                "line": line.trim(),
            }),
        );
    }
    mapping
}

// ---------------------------------------------------------------------------
// Python-facing argument coercion helpers (mirror int()/float()/str())
// ---------------------------------------------------------------------------

fn py_str(v: &Bound<'_, PyAny>) -> PyResult<String> {
    v.str()?.extract()
}

fn py_float(v: &Bound<'_, PyAny>) -> PyResult<f64> {
    if let Ok(f) = v.extract::<f64>() {
        return Ok(f);
    }
    let s: String = py_str(v)?;
    s.trim()
        .parse::<f64>()
        .map_err(|_| PyValueError::new_err(format!("could not convert string to float: '{s}'")))
}

fn rect4(v: &Bound<'_, PyAny>) -> PyResult<[f64; 4]> {
    let items: Vec<f64> = v
        .try_iter()?
        .map(|item| py_float(&item?))
        .collect::<PyResult<Vec<f64>>>()?;
    if items.len() != 4 {
        return Err(PyValueError::new_err(format!(
            "expected a [x, y, w, h] rect, got {} value(s)",
            items.len()
        )));
    }
    Ok([items[0], items[1], items[2], items[3]])
}

// ---------------------------------------------------------------------------
// Module-level pyfunctions
// ---------------------------------------------------------------------------

/// Open the CDP session the whole run hangs off. True if we launched it.
/// Already listening -> attach to what is there, so a browser the user
/// already has open (or a previous run's) is reused rather than duplicated.
#[pyfunction]
#[pyo3(signature = (port=CHROME_PORT, headless=false))]
pub fn launch_chrome(py: Python<'_>, port: u16, headless: bool) -> PyResult<bool> {
    py.detach(|| launch_chrome_impl(port, headless))
        .map_err(PyErr::from)
}

/// True for the surfaces that carry no page of their own.
#[pyfunction]
#[pyo3(signature = (url=None))]
pub fn is_blank_page(url: Option<Bound<'_, PyAny>>) -> PyResult<bool> {
    let text = match url {
        Some(v) if !v.is_none() => py_str(&v)?,
        _ => String::new(),
    };
    Ok(blank_url(&text))
}

/// The logo page as one line of HTML, ready to render into a blank tab.
#[pyfunction]
pub fn blank_html(py: Python<'_>) -> PyResult<String> {
    let browser = crate::browser_dir(py)?;
    let logo = browser
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.join("logo").join("logo.html"))
        .unwrap_or_default();
    Ok(blank_html_impl(&logo))
}

/// Guarantee the browser has a tab for the agent to work in.
#[pyfunction]
pub fn ensure_tab(py: Python<'_>, port: u16) -> PyResult<bool> {
    py.detach(|| ensure_tab_impl(port)).map_err(PyErr::from)
}

// ---------------------------------------------------------------------------
// BrowserScanner — the object the agent loop and the Python controller share.
// frozen + Mutex so re-entrant calls (service loop -> Python controller ->
// back into the scanner) can never hit a borrow panic, and so every method
// can release the GIL around its I/O.
// ---------------------------------------------------------------------------

#[pyclass(frozen)]
pub struct BrowserScanner {
    pub inner: Arc<Mutex<ScannerInner>>,
    pub frontend_callback: Option<Py<PyAny>>,
    port: u16,
}

impl BrowserScanner {
    /// Rust-side constructor (the #[new] pymethod wraps this) — service.rs
    /// builds the scanner directly during AgentService construction.
    pub fn create(
        browser_dir: &PathBuf,
        port: u16,
        frontend_callback: Option<Py<PyAny>>,
        out_dir: Option<PathBuf>,
        single_tab: bool,
    ) -> Self {
        BrowserScanner {
            inner: Arc::new(Mutex::new(ScannerInner::new(browser_dir, port, out_dir, single_tab))),
            frontend_callback,
            port,
        }
    }

    fn locked<T>(
        &self,
        py: Python<'_>,
        f: impl FnOnce(&mut ScannerInner) -> SResult<T> + Send,
    ) -> PyResult<T>
    where
        T: Send,
    {
        let inner = self.inner.clone();
        py.detach(move || {
            let mut guard = inner
                .lock()
                .map_err(|_| ScanErr::s("scanner state poisoned by an earlier panic"))?;
            f(&mut guard)
        })
        .map_err(PyErr::from)
    }
}

#[pymethods]
impl BrowserScanner {
    #[new]
    #[pyo3(signature = (port=CHROME_PORT, frontend_callback=None, out_dir=None, single_tab=false))]
    fn new(
        py: Python<'_>,
        port: u16,
        frontend_callback: Option<Py<PyAny>>,
        out_dir: Option<String>,
        single_tab: bool,
    ) -> PyResult<Self> {
        let browser = crate::browser_dir(py)?;
        Ok(BrowserScanner::create(
            &browser,
            port,
            frontend_callback,
            out_dir.map(PathBuf::from),
            single_tab,
        ))
    }

    fn start(&self, py: Python<'_>) -> PyResult<()> {
        self.locked(py, |s| s.start())
    }

    /// Quit the scanner. Chrome stays up — the browser outlives the run.
    pub fn stop(&self, py: Python<'_>) -> PyResult<()> {
        self.locked(py, |s| {
            s.stop();
            Ok(())
        })
    }

    pub fn scan_elements(&self, py: Python<'_>) -> PyResult<String> {
        // Wall clock for the WHOLE scan — the number the agent actually waits
        // each step, wider than the binary's own "N interactive, M ms" line.
        let t0 = Instant::now();
        let (out, image_b64) = self.locked(py, |s| {
            let out = s.scan_core()?;
            Ok((out, s.image_b64.clone()))
        })?;

        // The frontend callback runs OUTSIDE the scanner lock, so a callback
        // that turns around and calls the scanner can never deadlock.
        if let (Some(cb), Some(b64)) = (&self.frontend_callback, &image_b64) {
            let _ = cb.call1(py, (b64.as_str(),));
        }

        self.locked(py, move |s| {
            s.glow_tabs();
            s.last_scan_seconds = t0.elapsed().as_secs_f64();
            Ok(())
        })?;
        Ok(out)
    }

    /// (element_tree_text, annotated_image_base64, all_tabs_text).
    pub fn get_scan_data(&self, py: Python<'_>) -> PyResult<(String, Option<String>, String)> {
        self.locked(py, |s| {
            Ok((s.tree_text.clone(), s.image_b64.clone(), s.all_tabs.clone()))
        })
    }

    pub fn get_elements_mapping<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let mapping = self.locked(py, |s| Ok(Value::Object(s.mapping.clone())))?;
        pythonize::pythonize(py, &mapping).map_err(Into::into)
    }

    /// Paint the logo into the current tab, leaving the address bar alone.
    fn show_blank_page(&self, py: Python<'_>) -> PyResult<String> {
        self.locked(py, |s| s.show_blank_page())
    }

    /// Where the surfaces under `rect`'s centre currently sit, as
    /// [innerX, innerY, pageX, pageY]; None when it cannot be read.
    fn scroll_probe<'py>(
        &self,
        py: Python<'py>,
        rect: &Bound<'py, PyAny>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let r = match rect4(rect) {
            Ok(r) => r,
            Err(_) => return Ok(None),
        };
        let value = self.locked(py, move |s| Ok(s.scroll_probe(&r)))?;
        match value {
            Some(v) => Ok(Some(pythonize::pythonize(py, &v)?)),
            None => Ok(None),
        }
    }

    /// Bloom over `rect` — returns whether it drew, and never raises.
    fn flash(&self, py: Python<'_>, rect: &Bound<'_, PyAny>) -> PyResult<bool> {
        let r = match rect4(rect) {
            Ok(r) => r,
            Err(_) => return Ok(false),
        };
        self.locked(py, move |s| Ok(s.flash(&r)))
    }

    fn unflash(&self, py: Python<'_>) -> PyResult<()> {
        self.locked(py, |s| {
            s.unflash();
            Ok(())
        })
    }

    #[getter]
    fn current_url(&self, py: Python<'_>) -> PyResult<String> {
        self.locked(py, |s| Ok(s.url.clone()))
    }

    /// Current page's host — the browser's answer to macOS's app name.
    #[getter]
    pub fn application_name(&self, py: Python<'_>) -> PyResult<String> {
        self.locked(py, |s| Ok(s.application_name()))
    }

    /// [x, y, w, h] of the page, in CSS px — element [1] of the last scan.
    /// None before the first scan.
    #[getter]
    fn viewport_rect(&self, py: Python<'_>) -> PyResult<Option<Vec<f64>>> {
        self.locked(py, |s| Ok(s.viewport_rect().map(|r| r.to_vec())))
    }

    /// The open tabs as `<all_tabs>` would show them, read fresh.
    fn tabs(&self, py: Python<'_>) -> PyResult<String> {
        self.locked(py, |s| Ok(s.read_tabs()))
    }

    /// Read tab [n] from `tabs()` instead of the current one.
    ///
    /// The agent uses the switch_tab TOOL, which does this and reports it to
    /// the model. This is the same move without the reporting, for driving the
    /// scanner by hand.
    fn bind_tab(&self, py: Python<'_>, index: i64) -> PyResult<String> {
        self.locked(py, move |s| {
            let target = s.tab_target(index)?;
            s.bind_tab(&target)?;
            Ok(target)
        })
    }

    /// Numbered marks painted on the screenshot, on or off.
    fn set_marks(&self, py: Python<'_>, on: bool) -> PyResult<()> {
        self.locked(py, move |s| {
            s.set_marks(on);
            Ok(())
        })
    }

    /// Re-read tree/element.config.json without restarting.
    fn reload_config(&self, py: Python<'_>) -> PyResult<()> {
        self.locked(py, |s| {
            s.reload_config();
            Ok(())
        })
    }

    #[getter("_all_tabs")]
    fn all_tabs(&self, py: Python<'_>) -> PyResult<String> {
        self.locked(py, |s| Ok(s.all_tabs.clone()))
    }

    #[getter]
    fn dpr(&self, py: Python<'_>) -> PyResult<f64> {
        self.locked(py, |s| Ok(s.dpr))
    }

    #[getter]
    pub fn last_scan_seconds(&self, py: Python<'_>) -> PyResult<f64> {
        self.locked(py, |s| Ok(s.last_scan_seconds))
    }

    #[getter]
    fn port(&self) -> u16 {
        self.port
    }

    #[getter]
    fn out_dir(&self, py: Python<'_>) -> PyResult<String> {
        self.locked(py, |s| Ok(s.out_dir.display().to_string()))
    }
}
