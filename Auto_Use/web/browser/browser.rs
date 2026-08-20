// Copyright 2026 Ashish Yadav — Auto-Use

//! Browser session — opening Chrome and keeping the CDP link alive.
//!
//! THIS side owns Chrome: it launches the browser with the remote-debugging
//! port open, and Auto_Use/web/tree/element.rs simply ATTACHES to that port.
//! The scanner binary is spawned as a subprocess REPL — commands are the same
//! one-liners test.py's REPL takes:
//!   s | n <url> | g <url> | t | u <n> | cl <id> | hd <id> <s> | in <id> <text> | ie <id> <text>

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use base64::Engine;
use percent_encoding::{AsciiSet, CONTROLS};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use regex::Regex;
use serde_json::{json, Value};

use crate::ScannerError;

pub const CHROME_PORT: u16 = 9222;
const PROMPT: &[u8] = b"autouse> ";
// A cold scan of a heavy page plus its settle legitimately takes seconds, and
// element.rs has its own 30s navigation ceiling inside.
const SCANNER_TIMEOUT: f64 = 90.0;
// A blank tab is always about:blank — the URL the ADDRESS BAR shows. The logo
// is painted into it afterwards with Page.setDocumentContent.
const BLANK_URL: &str = "about:blank";

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
enum CdpFail {
    Clean(String),
    Lost(String),
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

/// Last `n` characters of a byte buffer, decoded lossily — Python's
/// `buf.decode('utf-8', 'replace')[-n:]`.
fn lossy_tail(buf: &[u8], n: usize) -> String {
    let s = String::from_utf8_lossy(buf);
    let count = s.chars().count();
    if count <= n {
        s.into_owned()
    } else {
        s.chars().skip(count - n).collect()
    }
}

/// Python `str(float)` — "2.0" for a whole number, not "2".
fn py_float_repr(v: f64) -> String {
    let s = format!("{}", v);
    if s.contains('.') || s.contains('e') || s.contains("inf") || s.contains("nan") {
        s
    } else {
        format!("{s}.0")
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

struct Cdp {
    port: u16,
    ws: Option<tungstenite::WebSocket<TcpStream>>,
    id: u64,
    /// targetId -> sessionId (flatten mode)
    sessions: HashMap<String, String>,
    /// Bumped on every hang-up. Sessions and anything registered inside them
    /// die with the connection, so a caller holding session-keyed state
    /// watches this to know its state is stale.
    generation: u64,
}

impl Cdp {
    fn new(port: u16) -> Self {
        Cdp { port, ws: None, id: 0, sessions: HashMap::new(), generation: 0 }
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
        self.generation += 1;
    }

    fn rpc(
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
        let deadline = Instant::now() + Duration::from_secs_f64(timeout);
        while Instant::now() < deadline {
            let ws = self.ws.as_mut().expect("still connected in loop");
            match ws.read() {
                Ok(tungstenite::Message::Text(t)) => {
                    let m: Value = match serde_json::from_str(t.as_ref()) {
                        Ok(v) => v,
                        Err(_) => continue,
                    };
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

    /// Discard whatever events are already waiting, so an idle socket can
    /// never back up between cosmetics passes.
    fn drain(&mut self, cap: usize) {
        let Some(ws) = self.ws.as_mut() else { return };
        if ws.get_ref().set_nonblocking(true).is_err() {
            self.drop_conn();
            return;
        }
        for _ in 0..cap {
            match ws.read() {
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
        if let Some(ws) = self.ws.as_mut() {
            ws.get_ref().set_nonblocking(false).ok();
        }
    }

    /// SessionId for a tab, dialing and attaching only when needed.
    fn attach(&mut self, target_id: &str) -> Result<String, CdpFail> {
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
        Ok(s)
    }

    fn forget(&mut self, target_id: &str) {
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
    if headless {
        cmd.arg("--headless=new");
    }
    // Start ON about:blank rather than letting Chrome show its New Tab page,
    // so the very first surface the agent sees is inert.
    cmd.arg(BLANK_URL);
    cmd.stdout(Stdio::null()).stderr(Stdio::null());
    cmd.spawn()
        .map_err(|e| ScanErr::s(format!("could not launch Chrome: {e}")))?;

    for _ in 0..60 {
        if port_open(port) {
            let mode = if headless { "headless" } else { "headful" };
            println!("Chrome launched on port {port} ({mode})");
            return Ok(true);
        }
        check_py_signals()?;
        sleep_s(0.25);
    }
    Err(ScanErr::s(format!("Chrome did not open the debug port {port}")))
}

// ---------------------------------------------------------------------------
// The scanner process + its scan state. Pure Rust, no GIL held during I/O —
// the pyclass wrapper below releases the GIL around every call in here.
// ---------------------------------------------------------------------------

/// The scanner's stdout and stderr, merged into one byte buffer — Python's
/// `stderr=subprocess.STDOUT`.
///
/// This used to be a hand-built POSIX pipe read with `poll()`, but
/// pipe/fcntl/dup/poll are Unix-only and the web agent has to build on Windows
/// too. One pump thread per stream plus a condvar gives the same contract with
/// nothing platform-specific: bytes land the instant they arrive (the prompt
/// carries no trailing newline, so anything line-buffered deadlocks), and a
/// reader can wait with a deadline.
struct ScannerOut {
    state: Mutex<ScannerOutState>,
    data: Condvar,
}

struct ScannerOutState {
    buf: Vec<u8>,
    /// Pump threads still running. 0 means every stream hit EOF.
    open: usize,
}

impl ScannerOut {
    fn new(streams: usize) -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(ScannerOutState { buf: Vec::new(), open: streams }),
            data: Condvar::new(),
        })
    }

    /// Drain one stream into the shared buffer until it closes.
    fn pump(self: &Arc<Self>, mut src: impl Read + Send + 'static) {
        let me = Arc::clone(self);
        thread::spawn(move || {
            let mut tmp = [0u8; 65536];
            loop {
                match src.read(&mut tmp) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let mut st = me.state.lock().unwrap();
                        st.buf.extend_from_slice(&tmp[..n]);
                        me.data.notify_all();
                    }
                }
            }
            let mut st = me.state.lock().unwrap();
            st.open = st.open.saturating_sub(1);
            me.data.notify_all();
        });
    }
}

pub struct ScannerInner {
    pub port: u16,
    out_dir: PathBuf,
    web_dir: PathBuf,
    tree_dir: PathBuf,
    logo_page: PathBuf,
    glow_css: PathBuf,
    glow_js: PathBuf,
    proc: Option<Child>,
    stdin: Option<ChildStdin>,
    /// The child's stdout+stderr, merged (like Python's stderr=subprocess.STDOUT).
    out: Option<Arc<ScannerOut>>,
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
    /// The dedicated tab's CDP target id (single-tab mode only). Re-created
    /// if the tab dies; never pointed at another agent's tab.
    my_tab_id: Option<String>,
}

// No Drop impl: the pump threads own the read ends now, and they exit on EOF
// when the child's pipes close. There is no raw fd left to hand back.

impl ScannerInner {
    pub fn new(browser_dir: &PathBuf, port: u16, out_dir: Option<PathBuf>, single_tab: bool) -> Self {
        // web/browser -> parent=web; web/tree holds the scanner crate, and
        // parent.parent=Auto_Use holds the shared logo.
        let web_dir = browser_dir.parent().map(|p| p.to_path_buf()).unwrap_or_default();
        let tree_dir = web_dir.join("tree");
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
            web_dir,
            tree_dir,
            logo_page,
            glow_css: browser_dir.join("glow").join("glow.css"),
            glow_js: browser_dir.join("glow").join("glow.js"),
            proc: None,
            stdin: None,
            out: None,
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
            my_tab_id: None,
        }
    }

    // -- process -----------------------------------------------------------

    fn element_bin(&self) -> PathBuf {
        // Built as a second target of the web crate — one target/ for the
        // whole web side.
        self.web_dir.join("target").join("release").join(exe_name("element"))
    }

    /// Build element.rs on first use — a minute once, then never again.
    fn ensure_binary(&self) -> SResult<PathBuf> {
        let bin = self.element_bin();
        if bin.exists() {
            return Ok(bin);
        }
        let cargo = which("cargo")
            .unwrap_or_else(|| home_dir().join(".cargo").join("bin").join(exe_name("cargo")));
        if !cargo.exists() {
            return Err(ScanErr::s(
                "cargo not found — the page scanner is a Rust binary and needs \
                 Rust installed (https://rustup.rs) to build once.",
            ));
        }
        println!("Building the element scanner (first run, ~1 min)...");
        let status = Command::new(&cargo)
            .args(["build", "--release", "--manifest-path"])
            .arg(self.web_dir.join("Cargo.toml"))
            .current_dir(&self.web_dir)
            .status()
            .map_err(|e| ScanErr::s(format!("cargo build failed to start: {e}")))?;
        if !status.success() {
            return Err(ScanErr::s(format!("cargo build failed with {status}")));
        }
        if !bin.exists() {
            return Err(ScanErr::s(format!("build succeeded but {} is missing", bin.display())));
        }
        Ok(bin)
    }

    fn proc_alive(&mut self) -> bool {
        match self.proc.as_mut() {
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => false,
        }
    }

    pub fn start(&mut self) -> SResult<()> {
        if self.proc_alive() {
            return Ok(());
        }
        let binary = self.ensure_binary()?;
        std::fs::create_dir_all(&self.out_dir)
            .map_err(|e| ScanErr::s(format!("could not create {}: {e}", self.out_dir.display())))?;
        // element.rs binds to an existing target and errors if there is none,
        // so the tab has to exist before it starts.
        if self.single_tab {
            // Parallel mode: this agent drives ONE tab of its own. Create it
            // (or re-create it if it died along with the binary) and bind the
            // scanner to it by exact target id — never to whatever tab another
            // agent has in front.
            let alive = self.my_tab_id.as_deref().is_some_and(|id| {
                page_targets(self.port)
                    .iter()
                    .any(|t| t.get("id").and_then(Value::as_str) == Some(id))
            });
            if !alive {
                self.my_tab_id = Some(create_tab_impl(self.port)?);
            }
        } else {
            ensure_tab_impl(self.port)?;
        }

        // stdout AND stderr are pumped into one buffer (Python's stderr=STDOUT)
        // by ScannerOut, so the prompt shows up the moment it is written — it
        // has no trailing newline, so anything line-buffered deadlocks on it.
        self.out = None;
        let mut cmd = Command::new(&binary);
        cmd.arg("--port")
            .arg(self.port.to_string())
            .arg("--out")
            .arg(&self.out_dir)
            .arg("--config")
            .arg(self.tree_dir.join("element.config.json"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(id) = &self.my_tab_id {
            // Bind by exact target id; the binary refuses to fall back to
            // another tab if this one is gone.
            cmd.arg("--target-id").arg(id);
        }
        // element is a console binary, so Windows gives it a console window of
        // its own and the user watches a REPL banner they are not meant to
        // drive. Everything it prints is already piped and consumed by
        // read_until_prompt (which deliberately swallows that banner), so the
        // window carries no information. Same flag the Python side passes on
        // every spawn - see windows/controller/view.py. Nothing to do on Unix.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let spawned = cmd.spawn();
        let mut child = match spawned {
            Ok(c) => c,
            Err(e) => return Err(ScanErr::s(format!("could not start the scanner: {e}"))),
        };
        let (child_out, child_err) = (child.stdout.take(), child.stderr.take());
        let out = ScannerOut::new(child_out.is_some() as usize + child_err.is_some() as usize);
        if let Some(s) = child_out {
            out.pump(s);
        }
        if let Some(s) = child_err {
            out.pump(s);
        }
        self.stdin = child.stdin.take();
        self.out = Some(out);
        self.proc = Some(child);

        // Consume the startup banner WITHOUT printing it — it is 18 lines of
        // noise before every run. The text is still captured, so a failed
        // start reports it in the error.
        self.read_until_prompt()?;

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

    /// Quit the scanner. Chrome stays up — the browser outlives the run.
    pub fn stop(&mut self) {
        // Hanging up unregisters the glow script with its session, so nothing
        // the user opens afterwards glows.
        self.cdp.drop_conn();
        let Some(mut child) = self.proc.take() else {
            self.stdin = None;
            self.out = None;
            return;
        };
        let alive = matches!(child.try_wait(), Ok(None));
        if alive {
            let quit_sent = self
                .stdin
                .as_mut()
                .map(|w| w.write_all(b"q\n").and_then(|_| w.flush()).is_ok())
                .unwrap_or(false);
            let mut exited = false;
            if quit_sent {
                let deadline = Instant::now() + Duration::from_secs(10);
                while Instant::now() < deadline {
                    if !matches!(child.try_wait(), Ok(None)) {
                        exited = true;
                        break;
                    }
                    sleep_s(0.05);
                }
            }
            if !exited {
                child.kill().ok();
                child.wait().ok();
            }
        }
        self.stdin = None;
        self.out = None;
    }

    // -- protocol ----------------------------------------------------------

    /// Read stdout up to the next `autouse> `. The prompt carries no trailing
    /// newline; the deadline bounds the wait so a wedged binary surfaces as an
    /// error instead of hanging the whole agent.
    fn read_until_prompt(&mut self) -> SResult<String> {
        let out = match self.out.as_ref() {
            Some(o) => Arc::clone(o),
            None => return Err(ScanErr::s("scanner has no output pipe")),
        };
        let mut buf: Vec<u8> = Vec::new();
        let deadline = Instant::now() + Duration::from_secs_f64(SCANNER_TIMEOUT);
        loop {
            // Take everything the pumps have produced. Bytes move out of the
            // shared buffer, so they are consumed exactly once — same contract
            // as the raw read() this replaced.
            let closed = {
                let mut st = out.state.lock().unwrap();
                buf.append(&mut st.buf);
                st.open == 0
            };
            if buf.ends_with(PROMPT) {
                let body = &buf[..buf.len() - PROMPT.len()];
                return Ok(String::from_utf8_lossy(body).into_owned());
            }
            let exited = match self.proc.as_mut() {
                None => true,
                Some(child) => !matches!(child.try_wait(), Ok(None)),
            };
            if exited {
                let code = self
                    .proc
                    .as_mut()
                    .and_then(|c| c.try_wait().ok().flatten())
                    .map(|st| {
                        st.code()
                            .map(|c| c.to_string())
                            .unwrap_or_else(|| st.to_string())
                    })
                    .unwrap_or_else(|| "?".to_string());
                return Err(ScanErr::s(format!(
                    "scanner exited (code {code}): {}",
                    lossy_tail(&buf, 400)
                )));
            }
            if closed {
                return Err(ScanErr::s(format!(
                    "scanner closed its output: {}",
                    lossy_tail(&buf, 400)
                )));
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(ScanErr::s(format!(
                    "scanner did not respond within {SCANNER_TIMEOUT:.0}s: {}",
                    lossy_tail(&buf, 400)
                )));
            }
            check_py_signals()?;
            // Cap the sleep at a second so Ctrl-C stays responsive, exactly as
            // the old poll() timeout did.
            let wait = remaining.min(Duration::from_secs(1));
            let st = out.state.lock().unwrap();
            if st.buf.is_empty() && st.open > 0 {
                let _ = out.data.wait_timeout(st, wait);
            }
        }
    }

    fn cmd(&mut self, line: &str) -> SResult<String> {
        if !self.proc_alive() {
            self.start()?;
        }
        let payload = format!("{}\n", line.trim_end_matches('\n'));
        let write_ok = self
            .stdin
            .as_mut()
            .map(|w| w.write_all(payload.as_bytes()).and_then(|_| w.flush()))
            .transpose();
        if write_ok.is_err() || self.stdin.is_none() {
            return Err(ScanErr::s("scanner stdin is closed"));
        }
        let out = self.read_until_prompt()?;
        for ln in out.lines() {
            if let Some(rest) = ln.strip_prefix("! ") {
                // element.rs's failure convention
                return Err(ScanErr::s(rest.trim().to_string()));
            }
        }
        Ok(out)
    }

    // -- scan --------------------------------------------------------------

    /// Everything scan_elements does except the frontend callback, the glow
    /// pass and the timing stamp — those run in the pyclass wrapper so the
    /// callback fires without this lock held.
    pub fn scan_core(&mut self) -> SResult<String> {
        self.start()?;
        // The click box is per-action and must not be frozen into the scan's
        // screenshot. The glow stays up: it is a thin, blurred edge the model
        // can ignore.
        self.unflash();
        let out = self.cmd("s")?;

        let raw = std::fs::read_to_string(self.out_dir.join("tree.txt")).unwrap_or_default();
        // The binary stamps two `# ` header lines (url, then counters) ahead
        // of the tree. The url is worth keeping; neither belongs in the
        // model's <element_tree>.
        self.url = String::new();
        let body: &str = if raw.starts_with('#') {
            let (head, rest) = match raw.find("\n\n") {
                Some(i) => (&raw[..i], &raw[i + 2..]),
                None => (raw.as_str(), ""),
            };
            // FIRST `# ` line is the url, second is the counters.
            for ln in head.lines() {
                if let Some(u) = ln.strip_prefix("# ") {
                    self.url = u.trim().to_string();
                    break;
                }
            }
            if rest.is_empty() {
                &raw
            } else {
                rest
            }
        } else {
            &raw
        };
        self.tree_text = prune_empty_containers(body.trim());

        self.image_b64 = std::fs::read(self.out_dir.join("shot.jpg"))
            .ok()
            .map(|bytes| base64::engine::general_purpose::STANDARD.encode(bytes));

        self.mapping = parse_mapping(&self.tree_text);
        self.load_hits();
        self.all_tabs = self.read_tabs();
        Ok(out)
    }

    /// Fold the scan's geometry (hits.json, DEVICE px) into the element
    /// mapping, converted to CSS px exactly once, here.
    fn load_hits(&mut self) {
        self.dpr = 1.0;
        let data: Value = match std::fs::read_to_string(self.out_dir.join("hits.json"))
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
        {
            Some(v) => v,
            None => return,
        };
        let dpr = data.get("dpr").and_then(Value::as_f64).unwrap_or(1.0);
        self.dpr = if dpr == 0.0 { 1.0 } else { dpr };
        let Some(hits) = data.get("hits").and_then(Value::as_object) else { return };
        for (key, rect) in hits {
            let Some(entry) = self.mapping.get_mut(key.as_str()) else { continue };
            let Some(vals) = rect.as_array() else { continue };
            if vals.len() != 4 {
                continue;
            }
            let mut css = [0.0f64; 4];
            let mut ok = true;
            for (i, v) in vals.iter().enumerate() {
                match v.as_f64() {
                    Some(f) => css[i] = f / self.dpr,
                    None => {
                        ok = false;
                        break;
                    }
                }
            }
            if !ok {
                continue;
            }
            let [x, y, w, h] = css;
            entry["rect"] = json!([x, y, w, h]);
            entry["point"] = json!([x + w / 2.0, y + h / 2.0]);
        }
    }

    /// `<all_tabs>` body: one line per open tab, current one marked. The
    /// binary numbers tabs from 0; the model-facing list is 1-based.
    fn read_tabs(&mut self) -> String {
        if self.single_tab {
            // One dedicated tab: the model always sees exactly its own tab as
            // [1]. Other agents' tabs in the shared browser never appear.
            let Some(id) = self.my_tab_id.clone() else {
                return String::new();
            };
            for t in page_targets(self.port) {
                if t.get("id").and_then(Value::as_str) == Some(id.as_str()) {
                    let url = t.get("url").and_then(Value::as_str).unwrap_or("");
                    let title = t.get("title").and_then(Value::as_str).unwrap_or("");
                    let mut line = format!(
                        "[1] {}",
                        if url.is_empty() { "about:blank" } else { url }
                    );
                    line.push_str(" (current)");
                    if !title.is_empty() {
                        line.push_str(&format!(" - {title}"));
                    }
                    return line;
                }
            }
            return String::new();
        }
        let out = match self.cmd("t") {
            Ok(o) => o,
            Err(ScanErr::Scanner(_)) => return String::new(),
            Err(_) => return String::new(),
        };
        let re = re_tab_line();
        let mut lines: Vec<String> = Vec::new();
        for ln in out.lines() {
            let Some(m) = re.captures(ln) else { continue };
            let current = m.get(1).map(|g| g.as_str()).unwrap_or("");
            let idx: i64 = m
                .get(2)
                .and_then(|g| g.as_str().parse().ok())
                .unwrap_or(0);
            let title = m.get(3).map(|g| g.as_str()).unwrap_or("");
            let url = m.get(4).map(|g| g.as_str()).unwrap_or("");
            let mut line = format!(
                "[{}] {}",
                idx + 1,
                if url.is_empty() { "about:blank" } else { url }
            );
            if current == "*" {
                line.push_str(" (current)");
            }
            if !title.is_empty() {
                line.push_str(&format!(" - {title}"));
            }
            lines.push(line);
        }
        lines.join("\n")
    }

    /// Url of the tab the scanner is bound to, from its own tab listing.
    fn current_tab_url(&mut self) -> String {
        let tabs = self.read_tabs();
        for ln in tabs.lines() {
            if ln.contains("(current)") {
                if let Some(m) = re_cur_tab().captures(ln) {
                    return m.get(1).map(|g| g.as_str().to_string()).unwrap_or_default();
                }
            }
        }
        String::new()
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

    /// Target id of the tab the scanner is bound to. Chrome's /json/list is
    /// ordered most-recently-USED and the scanner calls Page.bringToFront on
    /// its own tab, so the first page target is the one being driven. In
    /// single-tab mode that heuristic would point at OTHER agents' tabs, so
    /// the tracked dedicated-tab id is authoritative instead.
    fn current_target_id(&self) -> Result<String, CdpFail> {
        if self.single_tab {
            return self
                .my_tab_id
                .clone()
                .ok_or_else(|| CdpFail::Lost("no dedicated tab yet".into()));
        }
        let targets = page_targets(self.port);
        if targets.is_empty() {
            return Err(CdpFail::Lost("no page target".into()));
        }
        Ok(targets[0]
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string())
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
            let mine = self.my_tab_id.clone().unwrap_or_default();
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

    // -- actions -----------------------------------------------------------

    /// Paint the logo into the current tab, leaving the address bar alone.
    pub fn show_blank_page(&mut self) -> SResult<String> {
        let html = blank_html_impl(&self.logo_page);
        if html.is_empty() {
            return Ok(String::new());
        }
        let out = self.cmd(&format!("bl {html}"))?;
        // The paint carries the icon link but Chrome dropped its announcement
        // mid-rewrite — glow_tabs re-fires it.
        self.glow_tabs();
        Ok(out)
    }

    pub fn new_tab(&mut self, url: &str) -> SResult<String> {
        // An empty value means "a blank tab". A DESTINATION also goes
        // blank-first: the tab is created empty, armed with the glow script,
        // and only then navigated — its first real page glows from its first
        // paint instead of arriving bare and getting dressed a beat later.
        let target = url.trim().to_string();
        let out = self.cmd(&format!("n {BLANK_URL}"))?;
        self.glow_tabs(); // arm the tab that was just created
        if !target.is_empty() {
            return self.goto(&target);
        }
        self.show_blank_page()?;
        Ok(out)
    }

    pub fn switch_tab(&mut self, index: i64) -> SResult<String> {
        // Model-facing tab numbers are 1-based; the binary's `u` is 0-based.
        // This is the only place the shift is undone.
        self.cmd(&format!("u {}", index - 1))
    }

    pub fn close_tab(&mut self, index: i64) -> SResult<String> {
        // Tab LIFECYCLE is this side's business — the same side that creates
        // tabs over Chrome's HTTP endpoint (ensure_tab) closes them there
        // too; element.rs only ever PICKS a target from what exists. Both
        // sides read the same /json/list, so the model-facing [n] (1-based)
        // maps straight onto that ordering.
        let idx = index - 1;
        let targets = page_targets(self.port);
        if idx < 0 || idx as usize >= targets.len() {
            return Err(ScanErr::s("no such tab"));
        }
        // The LAST tab is refused rather than closed: a browser with zero
        // page targets leaves the scanner nothing to bind to, and "close the
        // last tab" almost always means "navigate it".
        if targets.len() <= 1 {
            return Err(ScanErr::s("cannot close the last tab - navigate it instead"));
        }
        let victim_id = targets[idx as usize]
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let was_current = self
            .current_target_id()
            .map(|id| id == victim_id)
            .unwrap_or(false);
        // /json/close answers with plain text ("Target is closing");
        // chrome_http tolerates that, and success is judged by the only
        // thing that matters — the target leaving the list.
        chrome_http(self.port, &format!("/json/close/{victim_id}"), "GET")?;
        let mut gone = false;
        for _ in 0..20 {
            let still_there = page_targets(self.port)
                .iter()
                .any(|t| t.get("id").and_then(Value::as_str) == Some(victim_id.as_str()));
            if !still_there {
                gone = true;
                break;
            }
            sleep_s(0.1);
        }
        if !gone {
            return Err(ScanErr::s("tab did not close"));
        }
        // The cosmetics session died with the tab.
        self.cdp.forget(&victim_id);
        if was_current {
            // The scanner's bound tab is gone, and its session with it —
            // re-bind the way a browser does, to the neighbour now holding
            // this slot (or the new last tab when the closed one was last).
            let remaining = page_targets(self.port);
            let j = (idx as usize).min(remaining.len().saturating_sub(1));
            return self.cmd(&format!("u {j}"));
        }
        Ok(String::new())
    }

    pub fn goto(&mut self, url: &str) -> SResult<String> {
        self.cmd(&format!("g {url}"))
    }

    pub fn reload(&mut self) -> SResult<String> {
        self.cmd("r")
    }

    pub fn back(&mut self) -> SResult<String> {
        self.cmd("bk")
    }

    pub fn forward(&mut self) -> SResult<String> {
        self.cmd("fw")
    }

    pub fn click_point(&mut self, rect: &[f64; 4], hold_seconds: f64, times: i64) -> SResult<String> {
        let [x, y, w, h] = rect;
        let ms = (hold_seconds.max(0.0) * 1000.0) as i64;
        let n = times.clamp(1, 2);
        self.cmd(&format!("cx {x:.1} {y:.1} {w:.1} {h:.1} {ms} {n}"))
    }

    pub fn scroll_point(&mut self, rect: &[f64; 4], dx: f64, dy: f64) -> SResult<String> {
        let [x, y, w, h] = rect;
        let (cx, cy) = (x + w / 2.0, y + h / 2.0);
        self.cmd(&format!("sx {cx:.1} {cy:.1} {dx:.1} {dy:.1}"))
    }

    pub fn input_point(&mut self, rect: &[f64; 4], value: &str, enter: bool) -> SResult<String> {
        let [x, y, w, h] = rect;
        let value = value.replace('\r', " ").replace('\n', " ");
        let flag = if enter { 1 } else { 0 };
        self.cmd(&format!("ix {x:.1} {y:.1} {w:.1} {h:.1} {flag} {value}"))
    }

    pub fn click(&mut self, index: i64) -> SResult<String> {
        self.cmd(&format!("cl {index}"))
    }

    pub fn hold_click(&mut self, index: i64, seconds: f64) -> SResult<String> {
        self.cmd(&format!("hd {index} {}", py_float_repr(seconds)))
    }

    pub fn input(&mut self, index: i64, value: &str, enter: bool) -> SResult<String> {
        // A newline would end the command line itself; the model means
        // "submit", which is exactly what `enter` is for.
        let value = value.replace('\r', " ").replace('\n', " ");
        self.cmd(&format!("{} {index} {value}", if enter { "ie" } else { "in" }))
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

fn re_tab_line() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"^\s*(\*?)\s*\[(\d+)\]\s*(.*?)\s*\|\s*(\S*)\s*$").unwrap())
}

fn re_cur_tab() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"^\[\d+\]\s+(\S+)").unwrap())
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

fn py_int(v: &Bound<'_, PyAny>) -> PyResult<i64> {
    if let Ok(i) = v.extract::<i64>() {
        return Ok(i);
    }
    if v.extract::<f64>().is_ok() && !v.is_instance_of::<pyo3::types::PyString>() {
        return Ok(v.extract::<f64>()? as i64);
    }
    let s: String = py_str(v)?;
    s.trim()
        .parse::<i64>()
        .map_err(|_| PyValueError::new_err(format!("invalid literal for int() with base 10: '{s}'")))
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

    #[pyo3(signature = (url=""))]
    fn new_tab(&self, py: Python<'_>, url: &str) -> PyResult<String> {
        let url = url.to_string();
        self.locked(py, move |s| s.new_tab(&url))
    }

    fn switch_tab(&self, py: Python<'_>, index: &Bound<'_, PyAny>) -> PyResult<String> {
        let idx = py_int(index)?;
        self.locked(py, move |s| s.switch_tab(idx))
    }

    fn close_tab(&self, py: Python<'_>, index: &Bound<'_, PyAny>) -> PyResult<String> {
        let idx = py_int(index)?;
        self.locked(py, move |s| s.close_tab(idx))
    }

    fn goto(&self, py: Python<'_>, url: &str) -> PyResult<String> {
        let url = url.to_string();
        self.locked(py, move |s| s.goto(&url))
    }

    fn reload(&self, py: Python<'_>) -> PyResult<String> {
        self.locked(py, |s| s.reload())
    }

    /// Step back one page in this tab's history. Raises when there is nowhere
    /// to go — "already at the first page" is an answer, not a silent no-op.
    fn back(&self, py: Python<'_>) -> PyResult<String> {
        self.locked(py, |s| s.back())
    }

    /// Step forward one page in this tab's history. Raises at the end.
    fn forward(&self, py: Python<'_>) -> PyResult<String> {
        self.locked(py, |s| s.forward())
    }

    /// Click the centre of `rect` (CSS px, [x, y, w, h]).
    #[pyo3(signature = (rect, hold_seconds=None, times=None))]
    fn click_point(
        &self,
        py: Python<'_>,
        rect: &Bound<'_, PyAny>,
        hold_seconds: Option<&Bound<'_, PyAny>>,
        times: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<String> {
        let r = rect4(rect)?;
        let hold = match hold_seconds {
            Some(v) if !v.is_none() => py_float(v)?,
            _ => 0.0,
        };
        let n = match times {
            Some(v) if !v.is_none() && v.is_truthy()? => py_int(v)?,
            _ => 1,
        };
        self.locked(py, move |s| s.click_point(&r, hold, n))
    }

    /// Turn a wheel by (dx, dy) CSS px over the centre of `rect`. Which
    /// surface moves is decided by the browser: the scroller under that point
    /// takes the delta.
    fn scroll_point(
        &self,
        py: Python<'_>,
        rect: &Bound<'_, PyAny>,
        dx: &Bound<'_, PyAny>,
        dy: &Bound<'_, PyAny>,
    ) -> PyResult<String> {
        let r = rect4(rect)?;
        let dx = py_float(dx)?;
        let dy = py_float(dy)?;
        self.locked(py, move |s| s.scroll_point(&r, dx, dy))
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

    /// Type into the element at `rect` (CSS px), optionally submitting.
    #[pyo3(signature = (rect, value, enter=None))]
    fn input_point(
        &self,
        py: Python<'_>,
        rect: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        enter: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<String> {
        let r = rect4(rect)?;
        let text = py_str(value)?;
        let submit = match enter {
            Some(v) => v.is_truthy()?,
            None => false,
        };
        self.locked(py, move |s| s.input_point(&r, &text, submit))
    }

    fn click(&self, py: Python<'_>, index: &Bound<'_, PyAny>) -> PyResult<String> {
        let idx = py_int(index)?;
        self.locked(py, move |s| s.click(idx))
    }

    fn hold_click(
        &self,
        py: Python<'_>,
        index: &Bound<'_, PyAny>,
        seconds: &Bound<'_, PyAny>,
    ) -> PyResult<String> {
        let idx = py_int(index)?;
        let secs = py_float(seconds)?;
        self.locked(py, move |s| s.hold_click(idx, secs))
    }

    #[pyo3(signature = (index, value, enter=None))]
    fn input(
        &self,
        py: Python<'_>,
        index: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        enter: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<String> {
        let idx = py_int(index)?;
        let text = py_str(value)?;
        let submit = match enter {
            Some(v) => v.is_truthy()?,
            None => false,
        };
        self.locked(py, move |s| s.input(idx, &text, submit))
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
