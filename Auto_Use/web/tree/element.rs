// AutoUse - browser element scanner (pure CDP, Rust)
//
// No JavaScript is executed in the page. Everything comes from:
//   DOMSnapshot.captureSnapshot  -> tree, geometry, paint order, computed styles
//   Accessibility.getFullAXTree  -> role, accessible name, state
//
// Nothing injected, no isolated worlds, no Runtime.enable, no page globals touched.
// Cross-process iframes (OOPIFs) reached via Target.setAutoAttach(flatten=true).
//
// Raw CDP over WebSocket against real installed Chrome.
// No playwright / selenium / puppeteer / chromedriver.

use base64::Engine;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};
use tungstenite::{stream::MaybeTlsStream, Message, WebSocket};

// ============================================================ toggles
// Same switches, same semantics, as mac/tree/element.py's DEBUG / FRONTEND.
// Compile-time consts rather than runtime flags: the branches they gate are
// pure I/O, so a `false` here costs nothing at all in the scan path.

/// Set to true to keep a per-scan record on disk, false for direct-LLM only.
///
/// Every scan writes its OWN folder `debug/iteration_<n>/` holding the tree
/// and the annotated screenshot — the SAME bytes handed to the model, so the
/// dump is a byte-identical record of the payload rather than a re-render.
/// Independent of `--out`, which keeps exactly one always-overwritten pair
/// (`scans/tree.txt` + `scans/shot.jpg`) as the "latest scan" view the REPL
/// prints; that pair is what test.py drives and is unaffected by this flag.
const DEBUG: bool = true;

/// Set to true when an agent/UI is driving this binary and wants an image to
/// display. Mirrors mac's rule exactly: with DEBUG on, the annotated capture
/// IS the frontend image (you want to see the marks you are debugging), so
/// nothing extra is produced. With DEBUG off, the marks are noise to a human,
/// so a PLAIN un-annotated copy is written alongside as `<out>/shot_plain.jpg`
/// for the UI. Costs no second capture — the plain frame already exists in
/// memory before the marks are painted onto it.
const FRONTEND: bool = true;

/// Per-scan debug folder root, relative to the process CWD — same layout and
/// the same relative anchor as macOS, so the agent's `debug/` wipe at startup
/// clears it.
const DEBUG_DIR: &str = "debug";

/// How long the page must be free of network activity before it counts as
/// settled. Short: this is "nothing is in flight right now", not "the site has
/// finished being a website".
const SETTLE_QUIET_MS: u64 = 150;

/// Hard ceiling on waiting for that quiet. Plenty of pages NEVER go quiet —
/// analytics beacons, websockets, long-polling, autoplaying video — so idle is
/// always "whichever comes first, idle or this".
const SETTLE_CEILING_MS: u64 = 3000;

// ============================================================ config

const DEFAULT_CONFIG: &str = r##"{
  "interactive_roles": [
    "button","link","checkbox","radio","tab","menuitem","menuitemcheckbox",
    "menuitemradio","option","switch","textbox","combobox","searchbox",
    "slider","spinbutton","treeitem","listbox","scrollbar","disclosuretriangle",
    "colorwell","date","datetime","inputtime","radiogroup","togglebutton"
  ],
  "landmark_roles": [
    "list","listitem","table","row","grid","gridcell","navigation","main",
    "dialog","alertdialog","form","banner","contentinfo","tablist","menu",
    "menubar","tree","article","heading","alert","status","rowgroup",
    "columnheader","rowheader","toolbar","region","search","complementary"
  ],
  "interactive_tags": ["a","button","input","select","textarea","summary","video","audio"],
  "landmark_tags": [
    "ul","ol","li","table","tr","td","th","nav","main","form","dialog",
    "header","footer","section","article","aside","fieldset",
    "h1","h2","h3","h4","h5","h6","iframe"
  ],
  "skip_tags": [
    "script","style","noscript","meta","link","head","title","template",
    "br","hr","path","defs","g","circle","rect","polygon","use","symbol",
    "clippath","lineargradient","stop","mask","filter","ellipse","line"
  ],
  "signals": {
    "ax_role": true,
    "is_clickable": true,
    "interactive_tag": true,
    "cursor_pointer": true,
    "focusable": false
  },
  "limits": {
    "max_elements": 300,
    "max_name_chars": 90,
    "max_text_chars": 140,
    "max_href_chars": 60,
    "min_width": 2,
    "min_height": 2,
    "viewport_margin": 0
  },
  "occlusion": {
    "enabled": true,
    "min_occluder_viewport_fraction": 0.12
  },
  "noise": {
    "enabled": true,
    "drop_nested_unnamed": true,
    "drop_nested_same_name": true
  },
  "cross_process_frames": true,

  "hosts": {
    "youtube.com": {
      "signals": { "cursor_pointer": false },
      "limits": { "max_elements": 200 }
    },
    "mail.google.com": {
      "signals": { "cursor_pointer": true }
    }
  }
}"##;

/// computed styles requested with the snapshot, in this exact order
const STYLE_PROPS: [&str; 8] = [
    "cursor",
    "pointer-events",
    "visibility",
    "opacity",
    "border-left-width",
    "border-top-width",
    // longhands: the `overflow` shorthand does not always resolve in a snapshot
    "overflow-x",
    "overflow-y",
];
const S_CURSOR: usize = 0;
const S_POINTER: usize = 1;
const S_VIS: usize = 2;
const S_OPACITY: usize = 3;
const S_BL: usize = 4;
const S_BT: usize = 5;
const S_OX: usize = 6;
const S_OY: usize = 7;

fn merge(base: &mut Value, over: &Value) {
    if let (Some(b), Some(o)) = (base.as_object_mut(), over.as_object()) {
        for (k, v) in o {
            match (b.get_mut(k), v) {
                (Some(bv), Value::Object(_)) if bv.is_object() => merge(bv, v),
                _ => {
                    b.insert(k.clone(), v.clone());
                }
            }
        }
    }
}

fn load_config(path: &str) -> Value {
    let mut cfg: Value = serde_json::from_str(DEFAULT_CONFIG).expect("bad embedded config");
    if let Ok(txt) = std::fs::read_to_string(path) {
        match serde_json::from_str::<Value>(&txt) {
            Ok(over) => {
                merge(&mut cfg, &over);
                eprintln!("  config overlay: {}", path);
            }
            Err(e) => eprintln!("  ! {} ignored: {}", path, e),
        }
    }
    cfg
}

fn host_of(url: &str) -> String {
    let s = url.split("://").nth(1).unwrap_or(url);
    let s = s.split('/').next().unwrap_or("");
    let s = s.rsplit('@').next().unwrap_or(s);
    s.split(':').next().unwrap_or("").to_lowercase()
}

/// shallow-merge any matching per-host block over the base
fn config_for(cfg: &Value, url: &str) -> Value {
    let host = host_of(url);
    let mut out = cfg.clone();
    if let Some(o) = out.as_object_mut() {
        o.remove("hosts");
    }
    if let Some(hosts) = cfg.get("hosts").and_then(|h| h.as_object()) {
        for (pat, over) in hosts {
            let p = pat.to_lowercase();
            if host == p || host.ends_with(&format!(".{}", p)) {
                merge(&mut out, over);
            }
        }
    }
    out
}

fn cfg_set(c: &Value, key: &str) -> HashSet<String> {
    c.get(key)
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|s| s.as_str())
                .map(|s| s.to_string())
                .collect()
        })
        .unwrap_or_default()
}

fn cfg_bool(c: &Value, group: &str, key: &str, d: bool) -> bool {
    c.get(group)
        .and_then(|g| g.get(key))
        .and_then(|v| v.as_bool())
        .unwrap_or(d)
}

fn cfg_f64(c: &Value, group: &str, key: &str, d: f64) -> f64 {
    c.get(group)
        .and_then(|g| g.get(key))
        .and_then(|v| v.as_f64())
        .unwrap_or(d)
}

// ============================================================ cdp

type Sock = WebSocket<MaybeTlsStream<TcpStream>>;

struct Cdp {
    ws: Sock,
    id: i64,
    events: Vec<Value>,
    replies: HashMap<i64, Value>,
}

impl Cdp {
    fn connect(ws_url: &str) -> Result<Self, String> {
        let (ws, _) = tungstenite::connect(ws_url).map_err(|e| format!("ws connect: {}", e))?;
        Ok(Cdp {
            ws,
            id: 0,
            events: Vec::new(),
            replies: HashMap::new(),
        })
    }

    fn set_timeout(&self, d: Option<Duration>) {
        if let MaybeTlsStream::Plain(s) = self.ws.get_ref() {
            let _ = s.set_read_timeout(d);
        }
    }

    fn recv(&mut self) -> Result<Value, String> {
        loop {
            match self.ws.read().map_err(|e| format!("ws read: {}", e))? {
                Message::Text(t) => {
                    return serde_json::from_str(&t).map_err(|e| format!("bad json: {}", e))
                }
                Message::Close(_) => return Err("socket closed".into()),
                _ => continue,
            }
        }
    }

    fn rpc(&mut self, method: &str, params: Value, session: Option<&str>) -> Result<Value, String> {
        self.id += 1;
        let mid = self.id;
        let mut msg = json!({ "id": mid, "method": method, "params": params });
        if let Some(s) = session {
            msg["sessionId"] = json!(s);
        }
        self.set_timeout(Some(Duration::from_secs(30)));
        self.ws
            .send(Message::Text(msg.to_string()))
            .map_err(|e| format!("ws send: {}", e))?;

        loop {
            let m = if let Some(v) = self.replies.remove(&mid) {
                v
            } else {
                self.recv()?
            };
            if m.get("method").is_some() {
                self.events.push(m);
                continue;
            }
            if m.get("id").and_then(|v| v.as_i64()) == Some(mid) {
                if let Some(e) = m.get("error") {
                    return Err(format!("{} -> {}", method, e));
                }
                return Ok(m.get("result").cloned().unwrap_or(json!({})));
            }
            if let Some(other) = m.get("id").and_then(|v| v.as_i64()) {
                self.replies.insert(other, m);
            }
        }
    }

    /// collect events that arrive with no request in flight
    /// Read what is waiting, stopping once the socket has been quiet for IDLE
    /// or `ms` total has elapsed — `ms` is a cap, not a sleep.
    ///
    /// This used to arm the read timeout with the whole remaining budget, so a
    /// quiet socket blocked for the entire window every time. attach_all calls
    /// it once per round, which put ~1s of dead waiting into every scan.
    fn drain(&mut self, ms: u64) {
        const IDLE: Duration = Duration::from_millis(60);
        let end = Instant::now() + Duration::from_millis(ms);
        loop {
            let left = end.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            self.set_timeout(Some(IDLE.min(left)));
            match self.recv() {
                Ok(m) => {
                    if m.get("method").is_some() {
                        self.events.push(m);
                    } else if let Some(id) = m.get("id").and_then(|v| v.as_i64()) {
                        self.replies.insert(id, m);
                    }
                }
                Err(_) => break,
            }
        }
        self.set_timeout(Some(Duration::from_secs(30)));
    }

    fn clear_events(&mut self) {
        self.events.clear();
    }

    fn take_events(&mut self, method: &str) -> Vec<Value> {
        let (out, keep): (Vec<Value>, Vec<Value>) = self
            .events
            .drain(..)
            .partition(|e| e.get("method").and_then(|m| m.as_str()) == Some(method));
        self.events = keep;
        out
    }
}

// ============================================================ chrome

fn chrome_paths() -> Vec<String> {
    let mut v: Vec<String> = Vec::new();
    if cfg!(target_os = "windows") {
        v.push(r"C:\Program Files\Google\Chrome\Application\chrome.exe".into());
        v.push(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe".into());
        if let Ok(la) = std::env::var("LOCALAPPDATA") {
            v.push(format!(r"{}\Google\Chrome\Application\chrome.exe", la));
        }
    } else if cfg!(target_os = "macos") {
        v.push("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome".into());
    } else {
        v.push("/usr/bin/google-chrome".into());
        v.push("/usr/bin/chromium".into());
        v.push("/usr/bin/chromium-browser".into());
    }
    v
}

fn find_chrome() -> Result<String, String> {
    for p in chrome_paths() {
        if std::path::Path::new(&p).exists() {
            return Ok(p);
        }
    }
    Err("chrome not found".into())
}

fn port_open(port: u16) -> bool {
    let addr = format!("127.0.0.1:{}", port).parse().unwrap();
    TcpStream::connect_timeout(&addr, Duration::from_millis(400)).is_ok()
}

/// Chrome keeps DevTools HTTP connections alive, so read exactly Content-Length
/// bytes — do not wait for EOF (that times out as EAGAIN / os error 35 on macOS).
fn http_json(port: u16, path: &str, method: &str) -> Result<Value, String> {
    let mut s = TcpStream::connect(("127.0.0.1", port)).map_err(|e| e.to_string())?;
    s.set_read_timeout(Some(Duration::from_secs(5))).ok();
    s.set_write_timeout(Some(Duration::from_secs(5))).ok();
    let req = format!(
        "{} {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        method, path, port
    );
    s.write_all(req.as_bytes()).map_err(|e| e.to_string())?;

    let mut buf = Vec::new();
    let mut tmp = [0u8; 4096];
    let header_end = loop {
        let n = s.read(&mut tmp).map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("connection closed before http headers".into());
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            break i + 4;
        }
        if buf.len() > 64 * 1024 {
            return Err("http headers too large".into());
        }
    };

    let headers = String::from_utf8_lossy(&buf[..header_end]);
    let content_len = headers
        .lines()
        .find_map(|l| {
            let (k, v) = l.split_once(':')?;
            if k.eq_ignore_ascii_case("content-length") {
                v.trim().parse::<usize>().ok()
            } else {
                None
            }
        })
        .ok_or("missing Content-Length")?;

    while buf.len() < header_end + content_len {
        let n = s.read(&mut tmp).map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("connection closed before full body".into());
        }
        buf.extend_from_slice(&tmp[..n]);
    }

    let body = std::str::from_utf8(&buf[header_end..header_end + content_len])
        .map_err(|e| e.to_string())?;
    serde_json::from_str(body.trim()).map_err(|e| format!("{} ({})", e, path))
}

fn browser_info(port: u16) -> Result<Value, String> {
    http_json(port, "/json/version", "GET")
}

fn targets(port: u16) -> Result<Vec<Value>, String> {
    let v = http_json(port, "/json/list", "GET")?;
    Ok(v.as_array()
        .map(|a| {
            a.iter()
                .filter(|t| t.get("type").and_then(|x| x.as_str()) == Some("page"))
                .cloned()
                .collect()
        })
        .unwrap_or_default())
}

fn urlencode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

/// GET on /json/new was dropped around Chrome 111; it is PUT only now.
fn new_tab(port: u16, url: &str) -> Result<Value, String> {
    http_json(port, &format!("/json/new?{}", urlencode(url)), "PUT")
}

fn launch_chrome(port: u16, headless: bool, offscreen: bool) -> Result<bool, String> {
    if port_open(port) {
        return Ok(false);
    }
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".into());
    let profile = format!("{}/.autouse/chrome-{}", home, port);
    std::fs::create_dir_all(&profile).ok();

    let mut cmd = std::process::Command::new(find_chrome()?);
    cmd.arg(format!("--remote-debugging-port={}", port))
        // required since Chrome 136: the default profile ignores the debug port
        .arg(format!("--user-data-dir={}", profile))
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .arg("--disable-backgrounding-occluded-windows")
        .arg("--disable-renderer-backgrounding")
        .arg("--disable-background-timer-throttling");
    // deliberately NOT --enable-automation (that is what sets navigator.webdriver)
    if headless {
        cmd.arg("--headless=new");
    } else if offscreen {
        cmd.arg("--window-position=-32000,-32000")
            .arg("--window-size=1280,900");
    }
    cmd.stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    cmd.spawn().map_err(|e| format!("spawn chrome: {}", e))?;

    for _ in 0..60 {
        if port_open(port) {
            return Ok(true);
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err(format!("chrome did not open debug port {}", port))
}

fn pick_target(port: u16, matching: Option<&str>) -> Result<Value, String> {
    // PICKS a target; never creates one. Whose tabs exist is the caller's
    // business — this binary scans and acts on what it is pointed at.
    // (macOS note for callers: closing every window leaves Chrome running with
    // zero page targets, so "attached successfully" does not imply "has a tab".)
    let ts = targets(port)?;
    if ts.is_empty() {
        return Err(format!(
            "no page targets on port {} - open a tab first",
            port
        ));
    }
    if let Some(m) = matching {
        let m = m.to_lowercase();
        for t in &ts {
            let hay = format!(
                "{}{}",
                t.get("url").and_then(|v| v.as_str()).unwrap_or(""),
                t.get("title").and_then(|v| v.as_str()).unwrap_or("")
            )
            .to_lowercase();
            if hay.contains(&m) {
                return Ok(t.clone());
            }
        }
    }
    Ok(ts[0].clone())
}

// ============================================================ snapshot decoding

fn sid(strings: &[Value], i: i64) -> &str {
    if i >= 0 {
        strings
            .get(i as usize)
            .and_then(|v| v.as_str())
            .unwrap_or("")
    } else {
        ""
    }
}

fn arr_i64(v: Option<&Value>) -> Vec<i64> {
    v.and_then(|x| x.as_array())
        .map(|a| a.iter().map(|x| x.as_i64().unwrap_or(-1)).collect())
        .unwrap_or_default()
}

fn rare_bool(v: Option<&Value>) -> HashSet<usize> {
    v.and_then(|x| x.get("index"))
        .and_then(|x| x.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_u64()).map(|x| x as usize).collect())
        .unwrap_or_default()
}

fn rare_int(v: Option<&Value>) -> HashMap<usize, i64> {
    let idx = arr_i64(v.and_then(|x| x.get("index")));
    let val = arr_i64(v.and_then(|x| x.get("value")));
    idx.iter()
        .zip(val.iter())
        .map(|(i, x)| (*i as usize, *x))
        .collect()
}

fn rare_str(v: Option<&Value>, strings: &[Value]) -> HashMap<usize, String> {
    let idx = arr_i64(v.and_then(|x| x.get("index")));
    let val = arr_i64(v.and_then(|x| x.get("value")));
    idx.iter()
        .zip(val.iter())
        .map(|(i, x)| (*i as usize, sid(strings, *x).to_string()))
        .collect()
}

fn clip(s: &str, n: usize) -> String {
    let flat = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() > n {
        let mut out: String = flat.chars().take(n.saturating_sub(1)).collect();
        out.push('\u{2026}');
        out
    } else {
        flat
    }
}

fn px(s: &str, d: f64) -> f64 {
    s.trim_end_matches("px").trim().parse::<f64>().unwrap_or(d)
}

// ============================================================ records

#[derive(Clone)]
struct Rec {
    tag: String,
    role: String,
    parent_node: i64,
    node: usize,
    /// Snapshot document index (local to the frame/session).
    doc: usize,
    /// Target/session index — unique across OOPIFs so kinship/occlusion
    /// do not mix parent-page nodes with iframe nodes that share doc/node ids.
    frame: usize,
    rect: [f64; 4],
    paint: i64,
    kind: char, // 'a' interactive | 'l' landmark | 't' text
    name: String,
    idx: Option<usize>,
    ty: Option<String>,
    href: Option<String>,
    checked: bool,
    expanded: Option<bool>,
    val: Option<String>,
    editable: bool,
}

fn intersect_rect(a: [f64; 4], b: [f64; 4]) -> Option<[f64; 4]> {
    let x0 = a[0].max(b[0]);
    let y0 = a[1].max(b[1]);
    let x1 = (a[0] + a[2]).min(b[0] + b[2]);
    let y1 = (a[1] + a[3]).min(b[1] + b[3]);
    let w = x1 - x0;
    let h = y1 - y0;
    if w <= 0.0 || h <= 0.0 {
        None
    } else {
        Some([x0, y0, w, h])
    }
}

struct Limits {
    max_elements: usize,
    max_name: usize,
    max_text: usize,
    max_href: usize,
    min_w: f64,
    min_h: f64,
    margin: f64,
}

struct Sets {
    iroles: HashSet<String>,
    lroles: HashSet<String>,
    itags: HashSet<String>,
    ltags: HashSet<String>,
    skip: HashSet<String>,
}

struct Signals {
    ax_role: bool,
    is_clickable: bool,
    interactive_tag: bool,
    cursor_pointer: bool,
    focusable: bool,
}

// ============================================================ scanner

type AxEntry = (String, String, HashMap<String, Value>, bool);

struct Scanner {
    cdp: Cdp,
    cfg: Value,
    /// [id] -> viewport rect from the MOST RECENT scan, in device pixels (the
    /// space DOMSnapshot bounds and the screenshot share). Input events are
    /// dispatched in CSS pixels, so `point()` divides by `dpr`.
    ///
    /// Rebuilt every scan, which is what makes the prompt's "ids are
    /// re-assigned on every scan" rule safe to act on: an id can only ever
    /// resolve against the tree the model was actually shown.
    hits: HashMap<usize, [f64; 4]>,
    /// device px per CSS px, measured on the last scan.
    dpr: f64,
}

impl Scanner {
    fn new(ws_url: &str, cfg: Value) -> Result<Self, String> {
        let mut s = Scanner {
            cdp: Cdp::connect(ws_url)?,
            cfg,
            hits: HashMap::new(),
            dpr: 1.0,
        };
        s.cdp.rpc("Page.enable", json!({}), None)?;
        s.cdp.rpc("DOM.enable", json!({}), None)?;
        let _ = s.cdp.rpc("Accessibility.enable", json!({}), None);
        // Network events are what `settle` counts. Enabling the domain is
        // reporting only — no interception, no request modification.
        let _ = s.cdp.rpc("Network.enable", json!({}), None);
        // A backgrounded tab must still believe it is focused: when several
        // agents share one browser, only one tab can be foreground, and pages
        // that gate behavior on focus (autofocus, visibility timers) would
        // otherwise misbehave for the rest. Best-effort; session-scoped.
        let _ = s.cdp.rpc("Emulation.setFocusEmulationEnabled", json!({"enabled": true}), None);
        // Runtime.enable intentionally never called
        s.activate();
        Ok(s)
    }

    /// Block until the page stops fetching, or the ceiling runs out.
    ///
    /// Replaces the caller's fixed post-action sleep. A guessed constant is
    /// wrong in both directions at once: too long for a page that was ready in
    /// 80ms, too short for one still fetching at 1.2s — and it cannot tell the
    /// difference, so it pays the worst case every single step.
    ///
    /// The signal is in-flight requests, counted from Network events.
    /// document.readyState would be the obvious answer anywhere else, but
    /// reading it means Runtime.evaluate — JavaScript in the page, which is
    /// the one thing this scanner does not do.
    ///
    /// Deliberately NOT waiting on `load`: a modern app fires that with an
    /// empty shell and renders afterwards, so it would report ready on a page
    /// with nothing in it yet.
    ///
    /// Returns how long it waited, so the caller can see the real cost.
    fn settle(&mut self) -> u64 {
        let start = Instant::now();
        let end = start + Duration::from_millis(SETTLE_CEILING_MS);
        let quiet_for = Duration::from_millis(SETTLE_QUIET_MS);
        // Start already "quiet" so a page with nothing happening returns on the
        // first pass instead of serving out the quiet window for no reason.
        let mut last_activity = start - quiet_for;
        // A SET of request ids, not a counter. Counting leaks on redirects:
        // a redirect re-fires requestWillBeSent for the SAME requestId with a
        // `redirectResponse` attached, and the first leg never gets its own
        // loadingFinished — so a counter climbs by two and comes down by one,
        // never reaches zero, and every scan after a redirect serves out the
        // full ceiling. A set is idempotent, so the redirect is a no-op.
        let mut inflight: HashSet<String> = HashSet::new();

        let id_of = |e: &Value| -> Option<String> {
            e.get("params")?
                .get("requestId")?
                .as_str()
                .map(|s| s.to_string())
        };

        loop {
            self.cdp.drain(60);
            let mut saw = false;
            for e in self.cdp.take_events("Network.requestWillBeSent") {
                saw = true;
                if let Some(id) = id_of(&e) {
                    inflight.insert(id);
                }
            }
            for m in ["Network.loadingFinished", "Network.loadingFailed"] {
                for e in self.cdp.take_events(m) {
                    saw = true;
                    if let Some(id) = id_of(&e) {
                        inflight.remove(&id);
                    }
                }
            }
            if saw {
                last_activity = Instant::now();
            }
            if inflight.is_empty() && last_activity.elapsed() >= quiet_for {
                break;
            }
            if Instant::now() >= end {
                break;
            }
        }
        // Everything else buffered while waiting is stale by definition, and
        // left in place it grows for the life of the process.
        self.cdp.clear_events();
        start.elapsed().as_millis() as u64
    }

    /// Bring this target's tab to the foreground of the browser window.
    ///
    /// Pointing the WebSocket at another target changes which page we READ and
    /// ACT on, but does nothing to the window — it keeps showing whatever tab
    /// it showed before. Reading and acting were always correct without this;
    /// what was broken is that a headful browser never visibly followed the
    /// agent, so `switch_tab` looked like it had done nothing at all.
    ///
    /// Best-effort: a target that refuses to come forward is still perfectly
    /// readable and clickable, so a failure here must not abort the switch.
    fn activate(&mut self) {
        let _ = self.cdp.rpc("Page.bringToFront", json!({}), None);
    }

    fn url(&mut self) -> String {
        self.cdp
            .rpc("Page.getFrameTree", json!({}), None)
            .ok()
            .and_then(|v| {
                v.get("frameTree")?
                    .get("frame")?
                    .get("url")?
                    .as_str()
                    .map(|s| s.to_string())
            })
            .unwrap_or_default()
    }

    // ---------------------------------------------------------------- input
    //
    // Trusted input through Input.dispatchMouseEvent / dispatchKeyEvent — the
    // same pipeline a physical mouse and keyboard feed. Still no JavaScript in
    // the page, no Runtime.enable, no injected handlers: the scanner's core
    // promise holds for acting as well as for reading.

    /// Centre of [id] in CSS pixels, resolved against the LAST scan.
    ///
    /// `hits` holds device pixels (what DOMSnapshot bounds and the screenshot
    /// share) while input events are dispatched in CSS pixels, so the divide by
    /// `dpr` is the whole reason this is a function and not a field read. Get it
    /// wrong on a 2x display and every click lands at half the intended offset.
    fn point(&self, idx: usize) -> Result<(f64, f64), String> {
        let r = self
            .hits
            .get(&idx)
            .ok_or_else(|| format!("no [{}] in the last scan - rescan first", idx))?;
        let d = if self.dpr > 0.0 { self.dpr } else { 1.0 };
        Ok(((r[0] + r[2] / 2.0) / d, (r[1] + r[3] / 2.0) / d))
    }

    fn mouse(&mut self, kind: &str, x: f64, y: f64) -> Result<(), String> {
        self.mouse_n(kind, x, y, 1)
    }

    /// Turn a wheel by (dx, dy) over a CSS-pixel point.
    ///
    /// A wheel event is delivered to whatever scroller sits UNDER the point,
    /// which is what makes one command cover both cases the agent needs: a
    /// point over the document scrolls the page, a point inside a scrollable
    /// panel scrolls that panel and leaves the page where it was. Nothing
    /// here has to know which is which, and no JS runs in the page to find
    /// out.
    ///
    /// The pointer is moved first: a scroller that has never seen the pointer
    /// may ignore the wheel, and hover-driven panels need the move anyway to
    /// be the thing under it.
    fn scroll_at(&mut self, x: f64, y: f64, dx: f64, dy: f64) -> Result<(), String> {
        self.cdp.rpc(
            "Input.dispatchMouseEvent",
            json!({ "type": "mouseMoved", "x": x, "y": y, "buttons": 0 }),
            None,
        )?;
        self.cdp.rpc(
            "Input.dispatchMouseEvent",
            json!({
                "type": "mouseWheel",
                "x": x,
                "y": y,
                "deltaX": dx,
                "deltaY": dy,
                "buttons": 0
            }),
            None,
        )?;
        Ok(())
    }

    fn mouse_n(&mut self, kind: &str, x: f64, y: f64, count: u32) -> Result<(), String> {
        self.cdp.rpc(
            "Input.dispatchMouseEvent",
            json!({
                "type": kind,
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": count,
                "buttons": if kind == "mousePressed" { 1 } else { 0 },
            }),
            None,
        )?;
        Ok(())
    }

    /// Press and release at a CSS-pixel point the CALLER resolved.
    ///
    /// The id-based `click` below stays for the REPL, where a human types an
    /// id. The agent uses this: it already holds the scan's geometry, so
    /// resolving there keeps the tree the model saw and the point acted on
    /// provably from the same scan.
    fn click_at(&mut self, x: f64, y: f64, w: f64, h: f64, hold_ms: u64, times: u32)
        -> Result<(), String> {
        // Centre is derived HERE from the rect, so there is exactly one
        // argument shape on the wire and no way to pass a point where a rect
        // is expected (or the reverse).
        let (x, y) = (x + w / 2.0, y + h / 2.0);
        self.cdp.rpc(
            "Input.dispatchMouseEvent",
            json!({ "type": "mouseMoved", "x": x, "y": y, "buttons": 0 }),
            None,
        )?;
        // A double click is ONE gesture with a rising clickCount, not two
        // independent clicks: the page distinguishes them by that count, and
        // dblclick only fires when the second press reports 2.
        let times = times.clamp(1, 2);
        for n in 1..=times {
            self.mouse_n("mousePressed", x, y, n)?;
            if hold_ms > 0 {
                std::thread::sleep(Duration::from_millis(hold_ms));
            }
            self.mouse_n("mouseReleased", x, y, n)?;
        }
        Ok(())
    }

    /// Focus a point, clear the field, type, optionally submit.
    fn input_at(&mut self, x: f64, y: f64, w: f64, h: f64, text: &str, enter: bool)
        -> Result<(), String> {
        self.click_at(x, y, w, h, 0, 1)?;
        self.type_into_focused(text, enter)
    }

    /// Press and release on [id]. `hold_ms` > 0 keeps the button down, which is
    /// what "hold to confirm" controls and human-verification holds need.
    fn click(&mut self, idx: usize, hold_ms: u64) -> Result<(), String> {
        // Membership check: point() resolves the same id, but a miss must
        // read as "rescan first" rather than whatever point() would say.
        self.hits
            .get(&idx)
            .ok_or_else(|| format!("no [{}] in the last scan - rescan first", idx))?;
        let (x, y) = self.point(idx)?;
        // Move first. Hover handlers open the menus and tooltips the press is
        // then meant to land inside, and some widgets ignore a press that
        // arrives with no prior movement over them.
        self.cdp.rpc(
            "Input.dispatchMouseEvent",
            json!({ "type": "mouseMoved", "x": x, "y": y, "buttons": 0 }),
            None,
        )?;
        self.mouse("mousePressed", x, y)?;
        if hold_ms > 0 {
            std::thread::sleep(Duration::from_millis(hold_ms));
        }
        self.mouse("mouseReleased", x, y)?;
        Ok(())
    }

    fn key(&mut self, key: &str, code: &str, vk: i64, text: &str) -> Result<(), String> {
        for kind in ["keyDown", "keyUp"] {
            let mut p = json!({
                "type": kind,
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
            });
            if kind == "keyDown" && !text.is_empty() {
                p["text"] = json!(text);
            }
            self.cdp.rpc("Input.dispatchKeyEvent", p, None)?;
        }
        Ok(())
    }

    /// Focus [id], clear it, type `text`, optionally submit with Enter.
    fn input(&mut self, idx: usize, text: &str, enter: bool) -> Result<(), String> {
        self.click(idx, 0)?; // focus the field
        self.type_into_focused(text, enter)
    }

    /// Clear whatever is focused and type into it.
    fn type_into_focused(&mut self, text: &str, enter: bool) -> Result<(), String> {

        // `commands` is Chrome's editing-command channel. selectAll through it
        // works whatever the platform modifier is, so there is no cmd-vs-ctrl
        // branch here and no dependence on the page honouring a synthetic
        // ctrl+a that a JS keydown handler may well swallow.
        self.cdp.rpc(
            "Input.dispatchKeyEvent",
            json!({"type": "keyDown", "key": "a", "code": "KeyA",
                   "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
                   "commands": ["selectAll"]}),
            None,
        )?;
        self.cdp.rpc(
            "Input.dispatchKeyEvent",
            json!({"type": "keyUp", "key": "a", "code": "KeyA",
                   "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65}),
            None,
        )?;

        if text.is_empty() {
            // insertText("") is a no-op, so an explicit clear needs a real
            // Backspace against the selection.
            self.key("Backspace", "Backspace", 8, "")?;
        } else {
            // insertText replaces the selection in ONE event. Per-character key
            // events are both far slower and far less reliable on
            // React-style inputs, which re-render between keystrokes.
            self.cdp
                .rpc("Input.insertText", json!({ "text": text }), None)?;
        }

        if enter {
            self.key("Enter", "Enter", 13, "\r")?;
        }
        Ok(())
    }

    /// Replace the current document with `html`, without navigating.
    ///
    /// Page.setDocumentContent, not a navigation and not script execution — so
    /// the no-JS-in-the-page promise holds. The point is the address bar: a
    /// file:// URL shows its whole path there, while this leaves whatever the
    /// caller already put in the bar (about:blank) and only swaps the content.
    /// The caller decides WHAT to render and WHEN; this only performs it.
    fn set_content(&mut self, html: &str) -> Result<(), String> {
        let fid = self
            .cdp
            .rpc("Page.getFrameTree", json!({}), None)?
            .get("frameTree")
            .and_then(|f| f.get("frame"))
            .and_then(|f| f.get("id"))
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        if fid.is_empty() {
            return Err("no frame to render into".into());
        }
        self.cdp.rpc(
            "Page.setDocumentContent",
            json!({ "frameId": fid, "html": html }),
            None,
        )?;
        Ok(())
    }

    fn goto(&mut self, url: &str, settle_ms: u64) -> Result<(), String> {
        self.cdp.rpc("Page.navigate", json!({ "url": url }), None)?;
        let end = Instant::now() + Duration::from_secs(30);
        while Instant::now() < end {
            self.cdp.drain(250);
            if !self.cdp.take_events("Page.loadEventFired").is_empty() {
                break;
            }
        }
        std::thread::sleep(Duration::from_millis(settle_ms));
        Ok(())
    }

    fn reload(&mut self, settle_ms: u64) -> Result<(), String> {
        self.cdp.rpc("Page.reload", json!({}), None)?;
        std::thread::sleep(Duration::from_millis(settle_ms));
        Ok(())
    }

    /// Step through this tab's session history: -1 is back, +1 is forward.
    ///
    /// Page.navigateToHistoryEntry rather than a synthetic Alt+Left: the key
    /// only works when the page has focus and nothing on it swallows the
    /// shortcut, while the history API is the browser's own move and reports
    /// honestly when there is nowhere to go.
    ///
    /// Refusing at the end of the history is deliberate. Silently doing
    /// nothing would leave the caller unable to tell "went back" from "was
    /// already at the first page", which is the same blindness the scroll
    /// tool had to solve — here the answer is simply known up front.
    fn history(&mut self, delta: i64, settle_ms: u64) -> Result<(), String> {
        let h = self.cdp.rpc("Page.getNavigationHistory", json!({}), None)?;
        let cur = h
            .get("currentIndex")
            .and_then(|v| v.as_i64())
            .ok_or("could not read the tab's history")?;
        let entries = h
            .get("entries")
            .and_then(|v| v.as_array())
            .ok_or("could not read the tab's history")?;
        let want = cur + delta;
        if want < 0 || want as usize >= entries.len() {
            return Err(format!(
                "no page to go {} to - this tab is at the {} of its history",
                if delta < 0 { "back" } else { "forward" },
                if delta < 0 { "start" } else { "end" }
            ));
        }
        let id = entries[want as usize]
            .get("id")
            .and_then(|v| v.as_i64())
            .ok_or("history entry has no id")?;
        self.cdp
            .rpc("Page.navigateToHistoryEntry", json!({ "entryId": id }), None)?;
        // Same wait as goto: a history move is a navigation like any other,
        // and the caller's next scan must not land mid-load.
        let end = Instant::now() + Duration::from_secs(30);
        while Instant::now() < end {
            self.cdp.drain(250);
            if !self.cdp.take_events("Page.loadEventFired").is_empty() {
                break;
            }
        }
        std::thread::sleep(Duration::from_millis(settle_ms));
        Ok(())
    }

    // -- sessions (OOPIF) -------------------------------------------------

    /// (session, parent_session, target/frame id) in tree order; root first
    fn attach_all(&mut self) -> Vec<(Option<String>, Option<String>, Option<String>)> {
        let mut found = vec![(None, None, None)];
        if !self
            .cfg
            .get("cross_process_frames")
            .and_then(|v| v.as_bool())
            .unwrap_or(true)
        {
            return found;
        }
        let mut pending: Vec<Option<String>> = vec![None];
        let mut seen_sess: HashSet<String> = HashSet::new();
        let mut seen_tid: HashSet<String> = HashSet::new();

        for _ in 0..6 {
            // depth cap on nested OOPIFs
            if pending.is_empty() {
                break;
            }
            for sess in &pending {
                let _ = self.cdp.rpc(
                    "Target.setAutoAttach",
                    json!({
                        "autoAttach": true,
                        "waitForDebuggerOnStart": false,
                        "flatten": true
                    }),
                    sess.as_deref(),
                );
            }
            self.cdp.drain(500);
            let mut next = Vec::new();
            for e in self.cdp.take_events("Target.attachedToTarget") {
                // envelope sessionId is the PARENT session
                let parent = e
                    .get("sessionId")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                let p = match e.get("params") {
                    Some(p) => p,
                    None => continue,
                };
                let sid_ = match p.get("sessionId").and_then(|v| v.as_str()) {
                    Some(s) => s.to_string(),
                    None => continue,
                };
                let info = p.get("targetInfo").cloned().unwrap_or(json!({}));
                let ttype = info.get("type").and_then(|v| v.as_str()).unwrap_or("");
                if seen_sess.contains(&sid_) || (ttype != "iframe" && ttype != "page") {
                    continue;
                }
                seen_sess.insert(sid_.clone());
                let tid = info
                    .get("targetId")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                if let Some(t) = &tid {
                    seen_tid.insert(t.clone());
                }
                found.push((Some(sid_.clone()), parent, tid));
                next.push(Some(sid_));
            }
            pending = next;
        }

        // Chrome often does not re-emit attachedToTarget for OOPIFs that already
        // exist; pull them explicitly. targetId == frameId for out-of-process frames.
        if let Ok(tg) = self.cdp.rpc("Target.getTargets", json!({}), None) {
            for info in tg
                .get("targetInfos")
                .and_then(|v| v.as_array())
                .cloned()
                .unwrap_or_default()
            {
                let ttype = info.get("type").and_then(|v| v.as_str()).unwrap_or("");
                if ttype != "iframe" {
                    continue;
                }
                let tid = match info.get("targetId").and_then(|v| v.as_str()) {
                    Some(t) => t.to_string(),
                    None => continue,
                };
                if !seen_tid.insert(tid.clone()) {
                    continue;
                }
                let sid_ = match self.cdp.rpc(
                    "Target.attachToTarget",
                    json!({ "targetId": tid, "flatten": true }),
                    None,
                ) {
                    Ok(v) => match v.get("sessionId").and_then(|s| s.as_str()) {
                        Some(s) => s.to_string(),
                        None => continue,
                    },
                    Err(_) => continue,
                };
                if !seen_sess.insert(sid_.clone()) {
                    continue;
                }
                found.push((Some(sid_.clone()), None, Some(tid)));
            }
        }
        found
    }

    fn prepare(&mut self, sess: Option<&str>) {
        let _ = self.cdp.rpc("Page.enable", json!({}), sess);
        let _ = self.cdp.rpc("DOM.enable", json!({}), sess);
        let _ = self.cdp.rpc("Accessibility.enable", json!({}), sess);
    }

    fn snapshot(&mut self, sess: Option<&str>) -> Result<Value, String> {
        self.cdp.rpc(
            "DOMSnapshot.captureSnapshot",
            json!({
                "computedStyles": STYLE_PROPS,
                "includePaintOrder": true,
                "includeDOMRects": false
            }),
            sess,
        )
    }

    /// backendDOMNodeId -> (role, name, state, ignored)
    fn axmap(&mut self, sess: Option<&str>) -> HashMap<i64, AxEntry> {
        let mut out = HashMap::new();
        let nodes = match self
            .cdp
            .rpc("Accessibility.getFullAXTree", json!({}), sess)
        {
            Ok(v) => v.get("nodes").cloned().unwrap_or(json!([])),
            Err(_) => return out,
        };
        for n in nodes.as_array().unwrap_or(&vec![]) {
            let b = match n.get("backendDOMNodeId").and_then(|v| v.as_i64()) {
                Some(b) => b,
                None => continue,
            };
            let role = n
                .get("role")
                .and_then(|r| r.get("value"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let name = n
                .get("name")
                .and_then(|r| r.get("value"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let mut state = HashMap::new();
            if let Some(props) = n.get("properties").and_then(|v| v.as_array()) {
                for p in props {
                    if let Some(k) = p.get("name").and_then(|v| v.as_str()) {
                        let v = p
                            .get("value")
                            .and_then(|x| x.get("value"))
                            .cloned()
                            .unwrap_or(Value::Null);
                        state.insert(k.to_string(), v);
                    }
                }
            }
            let ignored = n.get("ignored").and_then(|v| v.as_bool()).unwrap_or(false);
            out.insert(b, (role, name, state, ignored));
        }
        out
    }

    // -- document walk ----------------------------------------------------

    #[allow(clippy::too_many_arguments)]
    fn walk_doc(
        snap: &Value,
        doc_i: usize,
        frame: usize,
        origin: (f64, f64),
        frame_clip: [f64; 4],
        ax: &HashMap<i64, AxEntry>,
        sets: &Sets,
        sig: &Signals,
        lim: &Limits,
        out: &mut Vec<Rec>,
        abs_bounds: &mut HashMap<i64, [f64; 4]>,
        depth: usize,
    ) {
        if depth > 8 {
            return;
        }
        let empty: Vec<Value> = Vec::new();
        let strings = snap
            .get("strings")
            .and_then(|v| v.as_array())
            .unwrap_or(&empty);
        let docs = match snap.get("documents").and_then(|v| v.as_array()) {
            Some(d) => d,
            None => return,
        };
        let doc = match docs.get(doc_i) {
            Some(d) => d,
            None => return,
        };
        let nodes = doc.get("nodes").cloned().unwrap_or(json!({}));
        let layout = doc.get("layout").cloned().unwrap_or(json!({}));

        let parent_idx = arr_i64(nodes.get("parentIndex"));
        let node_name = arr_i64(nodes.get("nodeName"));
        let node_type = arr_i64(nodes.get("nodeType"));
        let backend = arr_i64(nodes.get("backendNodeId"));
        let clickable = rare_bool(nodes.get("isClickable"));
        let content_doc = rare_int(nodes.get("contentDocumentIndex"));
        let input_value = rare_str(nodes.get("inputValue"), strings);
        let input_checked = rare_bool(nodes.get("inputChecked"));
        let attrs = nodes
            .get("attributes")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();

        let lay_node = arr_i64(layout.get("nodeIndex"));
        let lay_bounds = layout
            .get("bounds")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let lay_styles = layout
            .get("styles")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let lay_paint = arr_i64(layout.get("paintOrders"));
        let lay_text = arr_i64(layout.get("text"));

        // node index -> layout index (first wins)
        let mut n2l: HashMap<usize, usize> = HashMap::new();
        for (li, ni) in lay_node.iter().enumerate() {
            n2l.entry(*ni as usize).or_insert(li);
        }

        let style = |li: usize, k: usize| -> &str {
            lay_styles
                .get(li)
                .and_then(|r| r.as_array())
                .and_then(|r| r.get(k))
                .and_then(|v| v.as_i64())
                .map(|i| sid(strings, i))
                .unwrap_or("")
        };
        let attr = |ni: usize, name: &str| -> Option<String> {
            let a = attrs.get(ni)?.as_array()?;
            let mut k = 0;
            while k + 1 < a.len() {
                if sid(strings, a[k].as_i64().unwrap_or(-1)) == name {
                    return Some(sid(strings, a[k + 1].as_i64().unwrap_or(-1)).to_string());
                }
                k += 2;
            }
            None
        };
        let bounds = |li: usize| -> [f64; 4] {
            lay_bounds
                .get(li)
                .and_then(|b| b.as_array())
                .map(|b| {
                    let g = |i: usize| b.get(i).and_then(|v| v.as_f64()).unwrap_or(0.0);
                    [g(0), g(1), g(2), g(3)]
                })
                .unwrap_or([0.0; 4])
        };

        // DOMSnapshot puts painted strings on #text nodes, not the parent element.
        // Walk children in order and fold phrasing wrappers (em/span/…) so
        // <span><em>Gmail</em> is email…</span> becomes one readable string.
        let mut text_at: HashMap<usize, String> = HashMap::new();
        for ni in 0..node_name.len() {
            if node_type.get(ni).copied().unwrap_or(0) != 3 {
                continue;
            }
            let li = match n2l.get(&ni) {
                Some(l) => *l,
                None => continue,
            };
            let t = sid(strings, lay_text.get(li).copied().unwrap_or(-1));
            if t.trim().is_empty() {
                continue;
            }
            text_at.insert(ni, t.to_string());
        }

        let mut kids: HashMap<usize, Vec<usize>> = HashMap::new();
        for (i, p) in parent_idx.iter().enumerate() {
            if *p >= 0 {
                kids.entry(*p as usize).or_default().push(i);
            }
        }
        let is_phrasing = |tag: &str| -> bool {
            matches!(
                tag,
                "em" | "strong" | "b" | "i" | "u" | "span" | "code" | "mark"
                    | "small" | "sub" | "sup" | "wbr" | "abbr" | "time" | "data"
            )
        };
        let mut rich_text: HashMap<usize, String> = HashMap::new();
        fn rich_of(
            ni: usize,
            node_type: &[i64],
            node_name: &[i64],
            strings: &[Value],
            kids: &HashMap<usize, Vec<usize>>,
            text_at: &HashMap<usize, String>,
            rich_text: &mut HashMap<usize, String>,
            is_phrasing: &dyn Fn(&str) -> bool,
        ) -> String {
            if let Some(t) = rich_text.get(&ni) {
                return t.clone();
            }
            let mut out = String::new();
            if let Some(ch) = kids.get(&ni) {
                for &c in ch {
                    match node_type.get(c).copied().unwrap_or(0) {
                        3 => {
                            if let Some(t) = text_at.get(&c) {
                                out.push_str(t);
                            }
                        }
                        1 => {
                            let tag =
                                sid(strings, node_name.get(c).copied().unwrap_or(-1)).to_lowercase();
                            if is_phrasing(&tag) {
                                out.push_str(&rich_of(
                                    c,
                                    node_type,
                                    node_name,
                                    strings,
                                    kids,
                                    text_at,
                                    rich_text,
                                    is_phrasing,
                                ));
                            }
                        }
                        _ => {}
                    }
                }
            }
            let flat = out.split_whitespace().collect::<Vec<_>>().join(" ");
            rich_text.insert(ni, flat.clone());
            flat
        }

        let mut emitted_els: HashSet<usize> = HashSet::new();
        for ni in 0..node_name.len() {
            if node_type.get(ni).copied().unwrap_or(0) != 1 {
                continue; // elements only
            }
            if emitted_els.contains(&ni) {
                continue;
            }
            let tag = sid(strings, node_name[ni]).to_lowercase();
            if sets.skip.contains(&tag) {
                continue;
            }
            let li = match n2l.get(&ni) {
                Some(l) => *l,
                None => continue,
            };
            let bn = backend.get(ni).copied().unwrap_or(-1);
            let b = bounds(li);

            // recurse into same-process child document
            if let Some(cd) = content_doc.get(&ni) {
                let cdi = *cd as usize;
                if let Some(cdoc) = docs.get(cdi) {
                    let sx = cdoc
                        .get("scrollOffsetX")
                        .and_then(|v| v.as_f64())
                        .unwrap_or(0.0);
                    let sy = cdoc
                        .get("scrollOffsetY")
                        .and_then(|v| v.as_f64())
                        .unwrap_or(0.0);
                    let bl = px(style(li, S_BL), 0.0);
                    let bt = px(style(li, S_BT), 0.0);
                    let child_origin = (
                        origin.0 + b[0] + bl - sx,
                        origin.1 + b[1] + bt - sy,
                    );
                    // Same-process iframe: clip to its content box so overflow is
                    // not painted onto the parent page.
                    let child_box = [
                        origin.0 + b[0] + bl,
                        origin.1 + b[1] + bt,
                        (b[2] - 2.0 * bl).max(0.0),
                        (b[3] - 2.0 * bt).max(0.0),
                    ];
                    if let Some(child_clip) = intersect_rect(frame_clip, child_box) {
                        Self::walk_doc(
                            snap,
                            cdi,
                            frame,
                            child_origin,
                            child_clip,
                            ax,
                            sets,
                            sig,
                            lim,
                            out,
                            abs_bounds,
                            depth + 1,
                        );
                    }
                }
            }

            let x = b[0] + origin.0;
            let y = b[1] + origin.1;
            let (w, h) = (b[2], b[3]);
            if bn >= 0 {
                abs_bounds.insert(bn, [x, y, w, h]);
            }
            let Some([x, y, w, h]) = intersect_rect([x, y, w, h], frame_clip) else {
                continue;
            };
            if w < lim.min_w || h < lim.min_h {
                continue;
            }
            if style(li, S_VIS) == "hidden" {
                continue;
            }
            let op = style(li, S_OPACITY);
            if !op.is_empty() && px(op, 1.0) < 0.05 {
                continue;
            }

            let (role, ax_name, state, ignored) = ax
                .get(&bn)
                .cloned()
                .unwrap_or_else(|| (String::new(), String::new(), HashMap::new(), false));
            let pointer = style(li, S_POINTER);
            let cursor = style(li, S_CURSOR);

            let truthy = |v: Option<&Value>| -> bool {
                matches!(v, Some(Value::Bool(true))) || matches!(v, Some(Value::String(s)) if s == "true")
            };

            let mut act = if sig.ax_role && sets.iroles.contains(&role) && !ignored {
                true
            } else if sig.interactive_tag && sets.itags.contains(&tag) {
                true
            } else if sig.is_clickable && clickable.contains(&ni) {
                true
            } else if sig.cursor_pointer && cursor == "pointer" {
                true
            } else {
                sig.focusable && truthy(state.get("focusable"))
            };
            if act && (truthy(state.get("disabled")) || pointer == "none") {
                act = false;
            }

            let mut text = sid(strings, lay_text.get(li).copied().unwrap_or(-1)).to_string();
            if text.trim().is_empty() {
                text = rich_of(
                    ni,
                    &node_type,
                    &node_name,
                    strings,
                    &kids,
                    &text_at,
                    &mut rich_text,
                    &is_phrasing,
                );
            }
            let text = clip(&text, lim.max_text);
            let land = sets.lroles.contains(&role) || sets.ltags.contains(&tag);
            if !act && text.is_empty() && !land {
                continue;
            }
            // body/html own-text is almost always inter-tag whitespace labels; emitting
            // them as records warps the tree (everything nests under <body>). Real copy
            // is emitted below as #text nodes instead.
            if !act && !land && (tag == "body" || tag == "html") {
                continue;
            }

            let display_name = if ax_name.is_empty() { &text } else { &ax_name };
            let kind = if act {
                'a'
            } else if land {
                'l'
            } else {
                't'
            };
            let mut rec = Rec {
                tag: tag.clone(),
                role: role.clone(),
                parent_node: parent_idx.get(ni).copied().unwrap_or(-1),
                node: ni,
                doc: doc_i,
                frame,
                rect: [x, y, w, h],
                paint: lay_paint.get(li).copied().unwrap_or(0),
                kind,
                name: clip(display_name, lim.max_name),
                idx: None,
                ty: None,
                href: None,
                checked: false,
                expanded: None,
                val: None,
                editable: false,
            };
            if act {
                rec.ty = attr(ni, "type").map(|s| s.to_lowercase());
                rec.href = attr(ni, "href").map(|s| clip(&s, lim.max_href));
                rec.checked = input_checked.contains(&ni) || truthy(state.get("checked"));
                if state.contains_key("expanded") {
                    rec.expanded = Some(truthy(state.get("expanded")));
                }
                rec.val = input_value.get(&ni).map(|v| clip(v, 40));
                rec.editable = truthy(state.get("editable"));
            }
            emitted_els.insert(ni);
            // Text-block containers absorb the inline descendants whose text
            // rich_of already folded into this name (<em>Gmail</em>, leftover
            // #text). It has to mirror rich_of's traversal exactly: rich_of
            // only descends through phrasing tags, so <a>/<button> text is
            // deliberately left out of the name for the child to emit itself.
            // Absorbing those subtrees anyway swallowed every inline link and
            // its text with it — a Wikipedia paragraph came out as
            // "encyklopedii, którą ." with each link punched out of it.
            if kind == 't' && !rec.name.is_empty() {
                let mut stack: Vec<usize> = kids.get(&ni).cloned().unwrap_or_default();
                while let Some(c) = stack.pop() {
                    match node_type.get(c).copied().unwrap_or(0) {
                        3 => {
                            emitted_els.insert(c);
                        }
                        1 => {
                            let ctag = sid(strings, node_name.get(c).copied().unwrap_or(-1))
                                .to_lowercase();
                            // rich_of skipped it, so it still owns its own text
                            if !is_phrasing(&ctag) || sets.itags.contains(&ctag) {
                                continue;
                            }
                            // a clickable <span> is phrasing but still a control
                            let cbn = backend.get(c).copied().unwrap_or(-1);
                            let crole = ax.get(&cbn).map(|e| e.0.as_str()).unwrap_or("");
                            if clickable.contains(&c) || sets.iroles.contains(crole) {
                                continue;
                            }
                            if !emitted_els.insert(c) {
                                continue;
                            }
                            if let Some(ch) = kids.get(&c) {
                                stack.extend(ch.iter().copied());
                            }
                        }
                        _ => {}
                    }
                }
            }
            out.push(rec);
        }

        // Visible #text nodes (e.g. "Initial page" / "Frame 1:") — elements-only
        // walks miss these because layout.text lives on the text node.
        for ni in 0..node_name.len() {
            if node_type.get(ni).copied().unwrap_or(0) != 3 {
                continue;
            }
            if emitted_els.contains(&ni) {
                continue;
            }
            let p = parent_idx.get(ni).copied().unwrap_or(-1);
            // skip labels already surfaced via the parent element's name
            if p >= 0 && emitted_els.contains(&(p as usize)) {
                continue;
            }
            let li = match n2l.get(&ni) {
                Some(l) => *l,
                None => continue,
            };
            // Same visibility gate the element loop applies. Without it every
            // collapsed menu still has layout boxes (visibility:hidden reserves
            // space, unlike display:none) and its labels were marked as if they
            // were on screen — Wikipedia's folded main menu and Tools panel.
            if style(li, S_VIS) == "hidden" {
                continue;
            }
            let op = style(li, S_OPACITY);
            if !op.is_empty() && px(op, 1.0) < 0.05 {
                continue;
            }
            let raw = sid(strings, lay_text.get(li).copied().unwrap_or(-1));
            let name = clip(raw, lim.max_name);
            if name.is_empty() {
                continue;
            }
            let b = bounds(li);
            // Clip to every ancestor that hides its overflow. A text node keeps
            // its full natural layout box even when the parent crops it, so the
            // "visually hidden" skip-link pattern (1x1 box, overflow:hidden,
            // clip rect) otherwise reads as a full-width on-screen label. This
            // also drops text scrolled out of an overflow:auto panel.
            let mut r = [b[0] + origin.0, b[1] + origin.1, b[2], b[3]];
            let mut anc = p;
            let mut cropped = false;
            for _ in 0..40 {
                if anc < 0 {
                    break;
                }
                let ai = anc as usize;
                if let Some(&ali) = n2l.get(&ai) {
                    let ox = style(ali, S_OX);
                    let oy = style(ali, S_OY);
                    if (!ox.is_empty() && ox != "visible") || (!oy.is_empty() && oy != "visible") {
                        let ab = bounds(ali);
                        match intersect_rect(
                            r,
                            [ab[0] + origin.0, ab[1] + origin.1, ab[2], ab[3]],
                        ) {
                            Some(c) => r = c,
                            None => {
                                cropped = true;
                                break;
                            }
                        }
                    }
                }
                anc = parent_idx.get(ai).copied().unwrap_or(-1);
            }
            if cropped {
                continue;
            }
            let Some([x, y, w, h]) = intersect_rect(r, frame_clip) else {
                continue;
            };
            if w < lim.min_w || h < lim.min_h {
                continue;
            }
            out.push(Rec {
                tag: "#text".into(),
                role: String::new(),
                parent_node: p,
                node: ni,
                doc: doc_i,
                frame,
                rect: [x, y, w, h],
                paint: lay_paint.get(li).copied().unwrap_or(0),
                kind: 't',
                name,
                idx: None,
                ty: None,
                href: None,
                checked: false,
                expanded: None,
                val: None,
                editable: false,
            });
        }
    }

    // -- occlusion --------------------------------------------------------

    /// No elementFromPoint without JS. Approximate with paint order + containment.
    /// Drop marks that carry no information the user cannot already get from an
    /// interactive ancestor.
    ///
    /// `cursor: pointer` inherits and DOMSnapshot's isClickable is set on
    /// descendants too, so every <svg>, wrapper <div role="generic"> and
    /// <span role="none"> inside a button scores as interactive and gets its
    /// own numbered box stacked on the same pixels. On a Google results page
    /// that was most of the marks: one "AI Mode" link produced four.
    ///
    /// Survivors are reparented onto the nearest surviving ancestor so removing
    /// a wrapper does not flatten its children to the root of the tree.
    fn drop_noise(recs: &mut Vec<Rec>, cfg: &Value) -> usize {
        if !cfg_bool(cfg, "noise", "enabled", true) {
            return 0;
        }
        let drop_unnamed = cfg_bool(cfg, "noise", "drop_nested_unnamed", true);
        let drop_same = cfg_bool(cfg, "noise", "drop_nested_same_name", true);
        let drop_generic = cfg_bool(cfg, "noise", "drop_unnamed_generic", true);
        if !drop_unnamed && !drop_same && !drop_generic {
            return 0;
        }

        let by_node: HashMap<(usize, usize, i64), usize> = recs
            .iter()
            .enumerate()
            .map(|(k, r)| ((r.frame, r.doc, r.node as i64), k))
            .collect();
        let up = |k: usize| -> Option<usize> {
            by_node
                .get(&(recs[k].frame, recs[k].doc, recs[k].parent_node))
                .copied()
                .filter(|&p| p != k)
        };

        let mut drop = vec![false; recs.len()];
        for k in 0..recs.len() {
            if recs[k].kind != 'a' {
                continue;
            }
            // a real form control is never just decoration around its parent
            if matches!(recs[k].tag.as_str(), "input" | "textarea" | "select") {
                continue;
            }
            // An unlabelled wrapper with no role of its own and nowhere to go
            // is not something anyone can act on -- an icon button keeps its
            // real role (button/link/...) and survives this.
            if drop_generic
                && recs[k].name.is_empty()
                && recs[k].href.is_none()
                && recs[k].val.is_none()
                && matches!(recs[k].role.as_str(), "" | "generic" | "none" | "presentation")
            {
                drop[k] = true;
                continue;
            }
            // nearest interactive ancestor
            let mut cur = k;
            let mut anc = None;
            for _ in 0..64 {
                match up(cur) {
                    Some(p) => {
                        cur = p;
                        if recs[p].kind == 'a' {
                            anc = Some(p);
                            break;
                        }
                    }
                    None => break,
                }
            }
            let Some(a) = anc else { continue };
            // a different destination is its own target, keep it
            if recs[k].href.is_some() && recs[k].href != recs[a].href {
                continue;
            }
            let unnamed = recs[k].name.is_empty();
            let same = !unnamed && recs[k].name == recs[a].name;
            if (unnamed && drop_unnamed) || (same && drop_same) {
                drop[k] = true;
            }
        }

        let mut reparent: Vec<i64> = recs.iter().map(|r| r.parent_node).collect();
        for k in 0..recs.len() {
            if drop[k] {
                continue;
            }
            let mut cur = k;
            let mut par = -1i64;
            for _ in 0..200 {
                match up(cur) {
                    Some(p) => {
                        if !drop[p] {
                            par = recs[p].node as i64;
                            break;
                        }
                        cur = p;
                    }
                    None => break,
                }
            }
            reparent[k] = par;
        }
        for (k, r) in recs.iter_mut().enumerate() {
            r.parent_node = reparent[k];
        }

        let dropped = drop.iter().filter(|d| **d).count();
        let mut k = 0;
        recs.retain(|_| {
            let keep = !drop[k];
            k += 1;
            keep
        });
        dropped
    }

    fn occluded(recs: &[Rec], vw: f64, vh: f64, cfg: &Value) -> HashSet<usize> {
        let mut hidden = HashSet::new();
        if !cfg_bool(cfg, "occlusion", "enabled", true) {
            return hidden;
        }
        let frac = cfg_f64(cfg, "occlusion", "min_occluder_viewport_fraction", 0.12);
        let min_area = frac * vw * vh;
        let big: Vec<usize> = (0..recs.len())
            .filter(|&i| recs[i].rect[2] * recs[i].rect[3] >= min_area)
            .collect();
        if big.is_empty() {
            return hidden;
        }

        // (frame, doc, node) -> record index, so a child never occludes its own parent
        let by_node: HashMap<(usize, usize, i64), usize> = recs
            .iter()
            .enumerate()
            .map(|(k, r)| ((r.frame, r.doc, r.node as i64), k))
            .collect();
        let is_kin = |a: usize, b: usize| -> bool {
            for (from, to) in [(a, b), (b, a)] {
                let mut cur = from;
                for _ in 0..40 {
                    if cur == to {
                        return true;
                    }
                    match by_node.get(&(
                        recs[cur].frame,
                        recs[cur].doc,
                        recs[cur].parent_node,
                    )) {
                        Some(p) => cur = *p,
                        None => break,
                    }
                }
            }
            false
        };

        for i in 0..recs.len() {
            let r = &recs[i];
            let cx = r.rect[0] + r.rect[2] / 2.0;
            let cy = r.rect[1] + r.rect[3] / 2.0;
            for &j in &big {
                // Paint order is per-frame; never let a parent-page box "occlude"
                // iframe content (or the reverse).
                if j == i || recs[j].frame != r.frame || recs[j].paint <= r.paint {
                    continue;
                }
                let o = &recs[j].rect;
                if !(o[0] <= cx && cx <= o[0] + o[2] && o[1] <= cy && cy <= o[1] + o[3]) {
                    continue;
                }
                if is_kin(i, j) {
                    continue;
                }
                hidden.insert(i);
                break;
            }
        }
        hidden
    }

    // -- scan -------------------------------------------------------------

    fn scan(&mut self, marks: bool, screenshot: bool) -> Result<ScanOut, String> {
        // Wait for the page rather than for a clock. Anything the previous
        // action set in motion — a navigation, an XHR, a lazy image — is still
        // arriving when this is called.
        let settled_ms = self.settle();
        let url = self.url();
        let cfg = config_for(&self.cfg, &url);

        let sets = Sets {
            iroles: cfg_set(&cfg, "interactive_roles"),
            lroles: cfg_set(&cfg, "landmark_roles"),
            itags: cfg_set(&cfg, "interactive_tags"),
            ltags: cfg_set(&cfg, "landmark_tags"),
            skip: cfg_set(&cfg, "skip_tags"),
        };
        let sig = Signals {
            ax_role: cfg_bool(&cfg, "signals", "ax_role", true),
            is_clickable: cfg_bool(&cfg, "signals", "is_clickable", true),
            interactive_tag: cfg_bool(&cfg, "signals", "interactive_tag", true),
            cursor_pointer: cfg_bool(&cfg, "signals", "cursor_pointer", true),
            focusable: cfg_bool(&cfg, "signals", "focusable", false),
        };
        let lim = Limits {
            max_elements: cfg_f64(&cfg, "limits", "max_elements", 300.0) as usize,
            max_name: cfg_f64(&cfg, "limits", "max_name_chars", 90.0) as usize,
            max_text: cfg_f64(&cfg, "limits", "max_text_chars", 140.0) as usize,
            max_href: cfg_f64(&cfg, "limits", "max_href_chars", 60.0) as usize,
            min_w: cfg_f64(&cfg, "limits", "min_width", 2.0),
            min_h: cfg_f64(&cfg, "limits", "min_height", 2.0),
            margin: cfg_f64(&cfg, "limits", "viewport_margin", 0.0),
        };

        let metrics = self.cdp.rpc("Page.getLayoutMetrics", json!({}), None)?;
        // DOMSnapshot layout.bounds are in device pixels (same as layoutViewport).
        // cssLayoutViewport is CSS pixels — using it clips everything below ~vh/dpr
        // (e.g. buttons at y=770 dropped when css vh=733 on a 2x display).
        let vp = metrics
            .get("layoutViewport")
            .cloned()
            .or_else(|| metrics.get("cssLayoutViewport").cloned())
            .unwrap_or(json!({}));
        let vw = vp.get("clientWidth").and_then(|v| v.as_f64()).unwrap_or(1280.0);
        let vh = vp.get("clientHeight").and_then(|v| v.as_f64()).unwrap_or(800.0);
        let css_vw = metrics
            .get("cssLayoutViewport")
            .and_then(|v| v.get("clientWidth"))
            .and_then(|v| v.as_f64())
            .unwrap_or(vw);
        let dpr = if css_vw > 0.0 { vw / css_vw } else { 1.0 };
        self.dpr = dpr;

        let sessions = self.attach_all();
        let n_sessions = sessions.len();
        let mut recs: Vec<Rec> = Vec::new();
        let mut abs_bounds: HashMap<i64, [f64; 4]> = HashMap::new();
        let mut origins: HashMap<String, (f64, f64)> = HashMap::new();
        let mut skipped = 0usize;

        let page_clip = [
            -lim.margin,
            -lim.margin,
            vw + 2.0 * lim.margin,
            vh + 2.0 * lim.margin,
        ];
        for (frame_i, (sess, parent, frame_id)) in sessions.iter().enumerate() {
            let mut base = (0.0, 0.0);
            let mut frame_clip = page_clip;
            let mut iframe_box: Option<[f64; 4]> = None;
            if let Some(s) = sess {
                self.prepare(Some(s));
                // locate this OOPIF's <iframe> box inside its parent session
                let owner = frame_id.as_ref().and_then(|f| {
                    self.cdp
                        .rpc(
                            "DOM.getFrameOwner",
                            json!({ "frameId": f }),
                            parent.as_deref(),
                        )
                        .ok()
                        .and_then(|v| v.get("backendNodeId").and_then(|x| x.as_i64()))
                });
                let Some(oid) = owner else {
                    skipped += 1;
                    continue;
                };
                // Prefer content box (CSS → device). Border-box from the snapshot
                // is a few device-px too large and shifts every OOPIF mark.
                let content = self
                    .cdp
                    .rpc(
                        "DOM.getBoxModel",
                        json!({ "backendNodeId": oid }),
                        parent.as_deref(),
                    )
                    .ok()
                    .and_then(|v| {
                        let c = v.get("model")?.get("content")?.as_array()?;
                        let g = |i: usize| c.get(i)?.as_f64();
                        let x0 = g(0)?;
                        let y0 = g(1)?;
                        let x1 = g(2)?;
                        let y1 = g(5)?;
                        Some([
                            x0 * dpr,
                            y0 * dpr,
                            (x1 - x0) * dpr,
                            (y1 - y0) * dpr,
                        ])
                    });
                let ob = match content.or_else(|| abs_bounds.get(&oid).copied()) {
                    Some(ob) => ob,
                    None => {
                        skipped += 1;
                        continue;
                    }
                };
                base = (ob[0], ob[1]);
                iframe_box = Some(ob);
                origins.insert(s.clone(), base);
            }

            let snap = match self.snapshot(sess.as_deref()) {
                Ok(s) => s,
                Err(_) => {
                    skipped += 1;
                    continue;
                }
            };
            let d0 = match snap.get("documents").and_then(|d| d.as_array()) {
                Some(a) if !a.is_empty() => a[0].clone(),
                _ => continue,
            };
            let ax = self.axmap(sess.as_deref());
            let sx = d0.get("scrollOffsetX").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let sy = d0.get("scrollOffsetY").and_then(|v| v.as_f64()).unwrap_or(0.0);
            // OOPIF: clip to the iframe content box so scrolled-off iframe content
            // is not painted as ghost marks on the parent page below the frame.
            if let Some(ob) = iframe_box {
                frame_clip = match intersect_rect(page_clip, ob) {
                    Some(c) => c,
                    None => {
                        skipped += 1;
                        continue;
                    }
                };
            }
            let origin = (base.0 - sx, base.1 - sy);
            Self::walk_doc(
                &snap,
                0,
                frame_i,
                origin,
                frame_clip,
                &ax,
                &sets,
                &sig,
                &lim,
                &mut recs,
                &mut abs_bounds,
                0,
            );
        }

        let hidden = Self::occluded(&recs, vw, vh, &cfg);
        let n_occluded = hidden.len();
        for i in &hidden {
            if recs[*i].kind == 'a' {
                recs[*i].kind = if recs[*i].name.is_empty() { 'x' } else { 't' };
            }
        }
        recs.retain(|r| r.kind != 'x');
        let n_noise = Self::drop_noise(&mut recs, &cfg);

        // [1] is ALWAYS the page itself, on every scan of every page: the
        // surface `scroll` acts on when the whole document should move, and
        // the one id a model can count on without having seen the tree yet.
        // Reserved here, before anything else is numbered, so a page's own
        // first element is [2] and the reservation can never shift.
        //
        // It carries the viewport rect, which makes it resolve like any other
        // id — a scroll aimed at [1] lands in the middle of the page — and it
        // gives the caller the viewport size to scale a scroll step by,
        // without a second round trip to ask for it.
        recs.insert(
            0,
            Rec {
                tag: "page".into(),
                role: String::new(),
                // No parent, and a node id no real element can hold, so the
                // synthetic record cannot collide with the DOM in the depth,
                // kinship or occlusion maps.
                parent_node: -1,
                node: usize::MAX,
                doc: 0,
                // Frame 0: render() starts a `<frame>` block whenever this
                // changes, and the page is not a frame boundary.
                frame: 0,
                rect: [0.0, 0.0, vw, vh],
                paint: -1,
                kind: 'v',
                name: String::new(),
                idx: Some(1),
                ty: None,
                href: None,
                checked: false,
                expanded: None,
                val: None,
                editable: false,
            },
        );

        // Number interactive elements and substantial text in document order
        // so search snippets sit next to their result in the index sequence.
        let mut n = 1usize;   // [1] is the page, above
        for r in recs.iter_mut() {
            if r.kind == 'v' {
                continue;     // already numbered, and never re-numbered
            }
            let substantial_text = r.kind == 't'
                && !r.name.is_empty()
                && r.name.chars().count() >= 12
                && r.rect[2] >= 40.0
                && r.rect[3] >= 12.0
                && r.rect[2] * r.rect[3] >= 800.0;
            let want = r.kind == 'a' || substantial_text;
            if !want {
                continue;
            }
            if n < lim.max_elements {
                // 1-based on purpose: [0] never exists — matching the tab
                // numbering and the prompt's index rule, and making an
                // unfilled/defaulted id=0 a clean miss instead of a silent
                // click on the first element.
                r.idx = Some(n + 1);
                n += 1;
            } else if r.kind == 'a' {
                r.kind = 't';
            }
        }

        // Freeze this scan's [id] -> rect map — the ONLY thing a later
        // click/input may resolve an id against. Replaced wholesale rather
        // than merged, which is what makes the prompt's "ids are re-assigned
        // on EVERY scan" rule safe: a stale id from an earlier tree misses
        // instead of silently landing on whatever now occupies that slot.
        self.hits = recs
            .iter()
            .filter_map(|r| r.idx.map(|i| (i, r.rect)))
            .collect();

        let mut shot: Option<Vec<u8>> = None;
        let mut plain: Option<Vec<u8>> = None;
        if screenshot {
            // Keep the visible window on the tab we are actually working in.
            // captureScreenshot targets this session and is correct either way
            // — the point is the human watching a headful run, who otherwise
            // sees a window frozen on some other tab while the agent works. It
            // also re-asserts the agent's tab if a user clicked away between
            // steps. One cheap CDP call.
            self.activate();
            let b64 = self
                .cdp
                .rpc(
                    "Page.captureScreenshot",
                    json!({ "format": "jpeg", "quality": 75, "captureBeyondViewport": false }),
                    None,
                )?
                .get("data")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let raw = base64::engine::general_purpose::STANDARD
                .decode(b64)
                .map_err(|e| e.to_string())?;
            // FRONTEND's un-annotated copy, taken here because `raw` is about
            // to be consumed by draw_marks. Only when the UI genuinely needs a
            // DIFFERENT image than the model's: with DEBUG on the annotated
            // frame is the one you want to look at, and with marks off `shot`
            // already IS the plain frame — cloning in either case is waste.
            if FRONTEND && !DEBUG && marks {
                plain = Some(raw.clone());
            }
            shot = Some(if marks {
                draw_marks(&raw, &recs).unwrap_or(raw)
            } else {
                raw
            });
        }

        Ok(ScanOut {
            tree: render(&recs),
            count: n,
            sessions: n_sessions,
            skipped,
            occluded: n_occluded,
            noise: n_noise,
            screenshot: shot,
            plain,
            url,
            settled_ms,
            hits: {
                let mut v: Vec<(usize, [f64; 4])> =
                    self.hits.iter().map(|(k, r)| (*k, *r)).collect();
                v.sort_by_key(|(k, _)| *k);
                v
            },
            dpr,
        })
    }
}

struct ScanOut {
    tree: String,
    count: usize,
    sessions: usize,
    skipped: usize,
    occluded: usize,
    noise: usize,
    screenshot: Option<Vec<u8>>,
    /// Un-annotated capture for the UI. Some(..) only when FRONTEND wants an
    /// image distinct from `screenshot` — see the FRONTEND const.
    plain: Option<Vec<u8>>,
    url: String,
    /// ms spent waiting for the page to go quiet before this scan.
    settled_ms: u64,
    /// [id] -> viewport rect in DEVICE px, and the device-px-per-CSS-px ratio
    /// to convert them. Published so the CALLER can resolve an id to a point
    /// itself: the tree it was handed and the geometry behind that tree then
    /// come from one scan, instead of the caller holding names while this
    /// process privately holds the coordinates they refer to.
    hits: Vec<(usize, [f64; 4])>,
    dpr: f64,
}

// ============================================================ debug dump

/// One scan's own folder under `debug/iteration_<n>/`, holding the tree and
/// the EXACT annotated bytes the model is handed — so the record is
/// byte-identical to the payload by construction, not a re-render of it.
/// Mirrors mac/tree/element.py's debug layout. Callers gate on DEBUG.
fn write_debug(
    iteration: usize,
    header: &str,
    tree: &str,
    shot: Option<&[u8]>,
) -> Result<String, String> {
    let dir = format!("{}/iteration_{}", DEBUG_DIR, iteration);
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    std::fs::write(format!("{}/tree.txt", dir), format!("{}{}\n", header, tree))
        .map_err(|e| e.to_string())?;
    if let Some(bytes) = shot {
        std::fs::write(format!("{}/annotated_screenshot.jpg", dir), bytes)
            .map_err(|e| e.to_string())?;
    }
    Ok(dir)
}

// ============================================================ render

fn line_of(r: &Rec) -> String {
    match (r.kind, r.idx) {
        ('a', Some(i)) => {
            let mut bits: Vec<String> = Vec::new();
            if let Some(t) = &r.ty {
                bits.push(format!("type=\"{}\"", t));
            }
            if !r.role.is_empty() && r.role != r.tag {
                bits.push(format!("role=\"{}\"", r.role));
            }
            if let Some(h) = &r.href {
                bits.push(format!("href=\"{}\"", h));
            }
            if let Some(v) = &r.val {
                bits.push(format!("value=\"{}\"", v));
            }
            if r.checked {
                bits.push("checked".into());
            }
            if let Some(e) = r.expanded {
                bits.push(if e { "expanded".into() } else { "collapsed".to_string() });
            }
            if r.editable {
                bits.push("editable".into());
            }
            let head = if bits.is_empty() {
                r.tag.clone()
            } else {
                format!("{} {}", r.tag, bits.join(" "))
            };
            let body = if r.name.is_empty() {
                format!("<{} />", head)
            } else {
                format!("<{}>{}</{}>", head, r.name, r.tag)
            };
            format!("[{}] {}", i, body)
        }
        ('l', _) => {
            let head = if r.role.is_empty() {
                r.tag.clone()
            } else {
                format!("{} role=\"{}\"", r.tag, r.role)
            };
            if r.name.is_empty() {
                format!("<{}>", head)
            } else {
                format!("<{}>{}</{}>", head, r.name, r.tag)
            }
        }
        // The page itself. Named rather than left bare so the model can see
        // what [1] is without being told, and marked scrollable because that
        // is the only thing it is for.
        ('v', Some(i)) => format!("[{}] <page scrollable>the whole page</page>", i),
        ('t', Some(i)) => format!("[{}] <text>{}</text>", i, r.name),
        _ => r.name.clone(),
    }
}

fn render(recs: &[Rec]) -> String {
    let idx: HashMap<(usize, usize, i64), usize> = recs
        .iter()
        .enumerate()
        .map(|(k, r)| ((r.frame, r.doc, r.node as i64), k))
        .collect();

    // iterative depth so a deep DOM cannot blow the stack
    let mut depth = vec![usize::MAX; recs.len()];
    for k in 0..recs.len() {
        if depth[k] != usize::MAX {
            continue;
        }
        let mut chain = Vec::new();
        let mut cur = k;
        let mut d;
        loop {
            if depth[cur] != usize::MAX {
                d = depth[cur];
                break;
            }
            chain.push(cur);
            match idx.get(&(recs[cur].frame, recs[cur].doc, recs[cur].parent_node)) {
                Some(&p) if p != cur && chain.len() < 200 => cur = p,
                _ => {
                    d = usize::MAX;
                    break;
                }
            }
        }
        for c in chain.into_iter().rev() {
            d = if d == usize::MAX { 0 } else { d + 1 };
            depth[c] = d;
        }
    }

    let mut lines = vec!["<element>".to_string()];
    let mut prev_frame: Option<usize> = None;
    for (k, r) in recs.iter().enumerate() {
        if let Some(p) = prev_frame {
            if p != r.frame {
                lines.push("  <frame>".to_string());
            }
        }
        prev_frame = Some(r.frame);
        let txt = line_of(r);
        if !txt.is_empty() {
            lines.push(format!("{}{}", "  ".repeat(depth[k] + 1), txt));
        }
    }
    lines.push("</element>".to_string());
    lines.join("\n")
}

// ============================================================ marks

const PAL: [[u8; 3]; 6] = [
    [225, 29, 72],
    [14, 165, 233],
    [22, 163, 74],
    [217, 119, 6],
    [124, 58, 237],
    [8, 145, 178],
];

/// 3x5 bitmap digits - avoids pulling in a font crate
const DIGITS: [[u8; 5]; 10] = [
    [0b111, 0b101, 0b101, 0b101, 0b111],
    [0b010, 0b110, 0b010, 0b010, 0b111],
    [0b111, 0b001, 0b111, 0b100, 0b111],
    [0b111, 0b001, 0b111, 0b001, 0b111],
    [0b101, 0b101, 0b111, 0b001, 0b001],
    [0b111, 0b100, 0b111, 0b001, 0b111],
    [0b111, 0b100, 0b111, 0b101, 0b111],
    [0b111, 0b001, 0b001, 0b001, 0b001],
    [0b111, 0b101, 0b111, 0b101, 0b111],
    [0b111, 0b101, 0b111, 0b001, 0b111],
];

/// Marks are painted onto the image here. The page is never touched.
fn draw_marks(jpeg: &[u8], recs: &[Rec]) -> Option<Vec<u8>> {
    use image::{codecs::jpeg::JpegEncoder, ExtendedColorType, ImageFormat};
    let img = image::load_from_memory_with_format(jpeg, ImageFormat::Jpeg).ok()?;
    let mut rgb = img.to_rgb8();
    let (iw, ih) = (rgb.width() as f64, rgb.height() as f64);
    // Snapshot bounds and the screenshot are both device pixels of the same
    // viewport, so rects land on the image 1:1 — do NOT rescale them.
    // The old `iw / vw` "measured dpr" was wrong: layoutViewport.clientWidth
    // excludes the scrollbar (2370) while the shot is the full width (2400),
    // so every mark was stretched ~1.3% and drifted right/down the further it
    // sat from the origin — ~30px off at the right edge of a 2x display.
    let stroke = 2i64;
    let sc = 2i64; // font pixel size

    let mut put = |x: i64, y: i64, c: [u8; 3]| {
        if x >= 0 && y >= 0 && (x as f64) < iw && (y as f64) < ih {
            rgb.put_pixel(x as u32, y as u32, image::Rgb(c));
        }
    };

    for r in recs {
        let i = match r.idx {
            Some(i) => i,
            None => continue,
        };
        // The page carries an id but no box: an outline around the whole
        // screenshot marks nothing and would just crowd the real elements.
        if r.kind == 'v' {
            continue;
        }
        let c = PAL[i % PAL.len()];
        let x0 = r.rect[0].round() as i64;
        let y0 = r.rect[1].round() as i64;
        let x1 = (r.rect[0] + r.rect[2]).round() as i64;
        let y1 = (r.rect[1] + r.rect[3]).round() as i64;

        for t in 0..stroke {
            for x in x0..=x1 {
                put(x, y0 + t, c);
                put(x, y1 - t, c);
            }
            for y in y0..=y1 {
                put(x0 + t, y, c);
                put(x1 - t, y, c);
            }
        }

        // index badge inside the box, top-left
        let label = i.to_string();
        let pad = sc;
        let lw = label.len() as i64 * (4 * sc) + 2 * pad;
        let lh = 5 * sc + 2 * pad;
        let box_w = (x1 - x0).max(0);
        let box_h = (y1 - y0).max(0);
        // shrink badge if the element is tiny so it still fits inside
        let (lw, lh, sc_l) = if lw > box_w || lh > box_h {
            let fit = ((box_w.max(1) as f64 / lw as f64).min(box_h.max(1) as f64 / lh as f64))
                .clamp(0.35, 1.0);
            let sc2 = ((sc as f64) * fit).round().max(1.0) as i64;
            let pad2 = sc2;
            (
                label.len() as i64 * (4 * sc2) + 2 * pad2,
                5 * sc2 + 2 * pad2,
                sc2,
            )
        } else {
            (lw, lh, sc)
        };
        let lx = x0;
        let ly = y0;
        for yy in ly..ly + lh {
            for xx in lx..lx + lw {
                put(xx, yy, c);
            }
        }
        for (di, ch) in label.chars().enumerate() {
            let d = match ch.to_digit(10) {
                Some(d) => DIGITS[d as usize],
                None => continue,
            };
            let ox = lx + sc_l + di as i64 * (4 * sc_l);
            let oy = ly + sc_l;
            for (row, bits) in d.iter().enumerate() {
                for col in 0..3 {
                    if bits & (1 << (2 - col)) != 0 {
                        for py in 0..sc_l {
                            for px_ in 0..sc_l {
                                put(
                                    ox + col * sc_l + px_,
                                    oy + row as i64 * sc_l + py,
                                    [255, 255, 255],
                                );
                            }
                        }
                    }
                }
            }
        }
    }

    let mut out = Vec::new();
    JpegEncoder::new_with_quality(&mut out, 80)
        .encode(rgb.as_raw(), rgb.width(), rgb.height(), ExtendedColorType::Rgb8)
        .ok()?;
    Some(out)
}

// ============================================================ cli

const HELP: &str = "
  s | scan       scan current page -> tree.txt + shot.jpg   (or just hit enter)
  g <url>        navigate current tab
  r | reload     reload current tab
  bk | back      go back one page in this tab's history
  fw | forward   go forward one page
  t | tabs       list open tabs
  n <url>        open a new tab
  u <n>          switch to tab n
  cl <id>        click [id] from the last scan
  hd <id> <s>    press and hold [id] for <s> seconds
  in <id> <text> clear [id] and type text
  ie <id> <text> same, then press Enter
  cx x y w h ms n  click a CSS-px rect; n=2 is a double click
  ix x y w h e t type at a CSS-pixel point; e=1 presses Enter
  sx x y dx dy   wheel at a CSS-pixel point (scrolls whatever is under it)
  bl <html>      render html as the current document (no navigation)
  m | marks      toggle number overlay
  c | config     reload config overlay file
  q | quit       exit (browser stays open)
";

struct Args {
    port: u16,
    url: Option<String>,
    /// Bind to this exact CDP target id at startup — no fallback to another
    /// tab. Used when several agents share one browser: each binary must land
    /// on its own agent's tab, never on whichever tab is frontmost.
    target_id: Option<String>,
    headless: bool,
    offscreen: bool,
    no_marks: bool,
    out: String,
    config: String,
    goto: Option<String>,
    settle: u64,
    /// What a "blank" tab should show. Defaults to about:blank; the agent
    /// passes its own inert page so a blank surface never lands on Chrome's
    /// New Tab page, which carries a Google search box and reads to a model
    /// like Google is already open.
    blank: String,
}

fn parse_args() -> Args {
    let mut a = Args {
        port: 9222,
        url: None,
        target_id: None,
        headless: false,
        offscreen: false,
        no_marks: false,
        out: "scans".into(),
        config: "element.config.json".into(),
        goto: None,
        settle: 2000,
        blank: "about:blank".into(),
    };
    let v: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < v.len() {
        let next = |i: usize| v.get(i + 1).cloned().unwrap_or_default();
        match v[i].as_str() {
            "--port" => {
                a.port = next(i).parse().unwrap_or(9222);
                i += 1;
            }
            "--url" => {
                a.url = Some(next(i));
                i += 1;
            }
            "--target-id" => {
                a.target_id = Some(next(i));
                i += 1;
            }
            "--out" => {
                a.out = next(i);
                i += 1;
            }
            "--config" => {
                a.config = next(i);
                i += 1;
            }
            "--goto" => {
                a.goto = Some(next(i));
                i += 1;
            }
            "--blank" => {
                a.blank = next(i);
                i += 1;
            }
            "--settle" => {
                a.settle = (next(i).parse::<f64>().unwrap_or(2.0) * 1000.0) as u64;
                i += 1;
            }
            "--headless" => a.headless = true,
            "--offscreen" => a.offscreen = true,
            "--no-marks" => a.no_marks = true,
            other => eprintln!("unknown arg: {}", other),
        }
        i += 1;
    }
    a
}

fn main() {
    let a = parse_args();

    let launched = match launch_chrome(a.port, a.headless, a.offscreen) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("{}", e);
            std::process::exit(1);
        }
    };
    println!(
        "chrome {} on port {}",
        if launched { "launched" } else { "attached" },
        a.port
    );
    // Port can accept TCP before /json/version is ready; retry briefly.
    let info = {
        let mut last = String::new();
        let mut ok = None;
        for _ in 0..40 {
            match browser_info(a.port) {
                Ok(v) => {
                    ok = Some(v);
                    break;
                }
                Err(e) => {
                    last = e;
                    std::thread::sleep(Duration::from_millis(100));
                }
            }
        }
        ok.ok_or_else(|| last)
    };
    match info {
        Ok(v) => println!(
            "  {}",
            v.get("Browser").and_then(|x| x.as_str()).unwrap_or("?")
        ),
        Err(e) => {
            eprintln!(
                "port {} is open but not answering as Chrome: {}",
                a.port, e
            );
            std::process::exit(1);
        }
    }

    std::fs::create_dir_all(&a.out).ok();
    let mut cfg = load_config(&a.config);

    let mut target = if let Some(id) = &a.target_id {
        // Exact binding for shared-browser (parallel agent) runs: only THIS
        // target will do. The tab was just created by the parent, so ride out
        // the /json/list propagation delay — but never grab a different tab.
        let mut found = None;
        for _ in 0..20 {
            found = targets(a.port)
                .ok()
                .and_then(|ts| {
                    ts.into_iter()
                        .find(|t| t.get("id").and_then(|v| v.as_str()) == Some(id.as_str()))
                });
            if found.is_some() {
                break;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        match found {
            Some(t) => t,
            None => {
                eprintln!("target {} not found on port {}", id, a.port);
                std::process::exit(1);
            }
        }
    } else {
        match pick_target(a.port, a.url.as_deref()) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("{}", e);
                std::process::exit(1);
            }
        }
    };
    let ws = |t: &Value| -> String {
        t.get("webSocketDebuggerUrl")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    };

    let mut sc = match Scanner::new(&ws(&target), cfg.clone()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{}", e);
            std::process::exit(1);
        }
    };
    if let Some(u) = &a.goto {
        let _ = sc.goto(u, a.settle);
    }

    let mut marks = !a.no_marks;
    // Scan counter behind DEBUG's per-scan folders (mac's _debug_iteration).
    let mut iteration = 0usize;
    println!("DEBUG = {}   FRONTEND = {}", DEBUG, FRONTEND);
    if DEBUG {
        println!("  per-scan record -> {}/iteration_<n>/", DEBUG_DIR);
    }
    println!("{}", HELP);

    let stdin = std::io::stdin();
    loop {
        print!("autouse> ");
        std::io::stdout().flush().ok();
        let mut line = String::new();
        if stdin.lock().read_line(&mut line).unwrap_or(0) == 0 {
            println!();
            break;
        }
        let line = line.trim();
        let raw = if line.is_empty() { "s" } else { line };
        let (cmd, arg) = match raw.split_once(' ') {
            Some((c, r)) => (c.to_lowercase(), r.trim().to_string()),
            None => (raw.to_lowercase(), String::new()),
        };

        let res: Result<(), String> = (|| {
            match cmd.as_str() {
                "q" | "quit" | "exit" => return Err("__quit__".into()),

                "h" | "help" | "?" => println!("{}", HELP),

                // Acting on the last scan. Two-letter names throughout: the
                // obvious single letters are already taken (c = config,
                // s = scan, h = help), and an agent typing `c 12` expecting a
                // click would otherwise silently reload the config instead.
                "cl" | "click" => {
                    let i: usize = arg.trim().parse().map_err(|_| "usage: cl <id>".to_string())?;
                    sc.click(i, 0)?;
                    println!("clicked [{}]", i);
                }

                "hd" | "hold" => {
                    let (a1, a2) = arg.trim().split_once(' ').unwrap_or((arg.trim(), "2"));
                    let i: usize = a1
                        .trim()
                        .parse()
                        .map_err(|_| "usage: hd <id> <seconds>".to_string())?;
                    let secs: f64 = a2.trim().parse().unwrap_or(2.0);
                    sc.click(i, (secs * 1000.0) as u64)?;
                    println!("held [{}] for {}s", i, secs);
                }

                "in" | "input" | "ie" => {
                    let (a1, txt) = arg
                        .split_once(' ')
                        .ok_or_else(|| format!("usage: {} <id> <text>", cmd))?;
                    let i: usize = a1
                        .trim()
                        .parse()
                        .map_err(|_| format!("usage: {} <id> <text>", cmd))?;
                    let enter = cmd == "ie";
                    sc.input(i, txt, enter)?;
                    println!("typed into [{}]{}", i, if enter { " + Enter" } else { "" });
                }

                // Point-based twins of cl/hd/in/ie. The agent resolves an [id]
                // to a point from the geometry the scan published, so the tree
                // it showed the model and the point it acts on are provably
                // the same scan. x/y/w/h are CSS pixels.
                "cx" | "ix" => {
                    // cx: x y w h hold_ms            -> 5 fields
                    // ix: x y w h enter <text...>      -> 6, the last holding
                    // the REST of the line. Splitting into 7 made the text a
                    // single token and silently dropped everything after the
                    // first space, so "rust lang" typed as "rust".
                    // cx: x y w h hold_ms times       -> 6 fields
                    // ix: x y w h enter <text...>       -> 6, the last holding
                    // the REST of the line. Splitting into 7 made the text a
                    // single token and silently dropped everything after the
                    // first space, so "rust lang" typed as "rust".
                    let mut it = arg.splitn(6, ' ');
                    let mut num = |name: &str| -> Result<f64, String> {
                        it.next()
                            .and_then(|v| v.trim().parse::<f64>().ok())
                            .ok_or_else(|| format!("{}: bad or missing {}", cmd, name))
                    };
                    let (x, y, w, h) = (num("x")?, num("y")?, num("w")?, num("h")?);
                    if cmd == "cx" {
                        let hold = num("hold_ms").unwrap_or(0.0).max(0.0) as u64;
                        let times = num("times").unwrap_or(1.0).max(1.0) as u32;
                        sc.click_at(x, y, w, h, hold, times)?;
                        println!("clicked ({:.0},{:.0}) x{}", x, y, times.clamp(1, 2));
                    } else {
                        let enter = num("enter").unwrap_or(0.0) != 0.0;
                        let text = it.next().unwrap_or("");
                        sc.input_at(x, y, w, h, text, enter)?;
                        println!("typed at ({:.0},{:.0}){}", x, y,
                                 if enter { " + Enter" } else { "" });
                    }
                }

                "sx" | "scroll" => {
                    // sx: x y dx dy — CSS pixels, wheel deltas. Point-based
                    // like cx/ix: the caller resolved which surface to scroll
                    // from the scan it showed the model, and this dispatches.
                    let mut it = arg.split_whitespace();
                    let mut num = |name: &str| -> Result<f64, String> {
                        it.next()
                            .and_then(|v| v.parse::<f64>().ok())
                            .ok_or_else(|| format!("sx: bad or missing {}", name))
                    };
                    let (x, y, dx, dy) = (num("x")?, num("y")?, num("dx")?, num("dy")?);
                    sc.scroll_at(x, y, dx, dy)?;
                    println!("scrolled ({:.0},{:.0}) by ({:.0},{:.0})", x, y, dx, dy);
                }

                "bl" | "blank" => {
                    if arg.is_empty() {
                        println!("usage: bl <html>");
                        return Ok(());
                    }
                    sc.set_content(&arg)?;
                    println!("blank page rendered");
                }

                "m" | "marks" => {
                    marks = !marks;
                    println!("marks: {}", marks);
                }

                "c" | "config" => {
                    cfg = load_config(&a.config);
                    sc.cfg = cfg.clone();
                    println!("config reloaded");
                }

                "g" | "goto" => {
                    if arg.is_empty() {
                        println!("usage: g <url>");
                        return Ok(());
                    }
                    let u = if arg.contains("://") {
                        arg.clone()
                    } else {
                        format!("https://{}", arg)
                    };
                    sc.goto(&u, a.settle)?;
                    println!("-> {}", u);
                }

                "r" | "reload" => sc.reload(a.settle)?,

                "bk" | "back" => {
                    sc.history(-1, a.settle)?;
                    println!("went back");
                }

                "fw" | "forward" => {
                    sc.history(1, a.settle)?;
                    println!("went forward");
                }

                "t" | "tabs" => {
                    let cur = ws(&target);
                    for (i, x) in targets(a.port)?.iter().enumerate() {
                        let mark = if ws(x) == cur { "*" } else { " " };
                        println!(
                            " {}[{}] {} | {}",
                            mark,
                            i,
                            x.get("title").and_then(|v| v.as_str()).unwrap_or(""),
                            x.get("url").and_then(|v| v.as_str()).unwrap_or("")
                        );
                    }
                }

                "n" | "new" => {
                    let u = if arg.is_empty() {
                        a.blank.clone()
                    } else if arg.contains("://") {
                        arg.clone()
                    } else {
                        format!("https://{}", arg)
                    };
                    let created = new_tab(a.port, &u)?;
                    std::thread::sleep(Duration::from_secs(1));
                    // Bind to the target /json/new actually created, by its id.
                    // `targets().last()` was wrong: /json/list is ordered
                    // most-recently-USED, not creation order, so it routinely
                    // handed back the tab we came from — and then every scan
                    // and click for the rest of the run landed on that page
                    // instead of the one just opened.
                    let created_id = created
                        .get("id")
                        .and_then(|v| v.as_str())
                        .unwrap_or_default()
                        .to_string();
                    let ts = targets(a.port)?;
                    target = ts
                        .iter()
                        .find(|t| {
                            t.get("id").and_then(|v| v.as_str()) == Some(created_id.as_str())
                        })
                        .cloned()
                        .or_else(|| {
                            created
                                .get("webSocketDebuggerUrl")
                                .is_some()
                                .then(|| created.clone())
                        })
                        .or_else(|| ts.last().cloned())
                        .ok_or("no tabs")?;
                    sc = Scanner::new(&ws(&target), cfg.clone())?;
                    println!("-> {}", u);
                }

                "u" | "use" => {
                    let i: usize = arg.parse().map_err(|_| "usage: u <n>".to_string())?;
                    let ts = targets(a.port)?;
                    target = ts.get(i).cloned().ok_or("no such tab")?;
                    sc = Scanner::new(&ws(&target), cfg.clone())?;
                    println!(
                        "-> {}",
                        target.get("title").and_then(|v| v.as_str()).unwrap_or("")
                    );
                }

                "s" | "scan" => {
                    let t0 = Instant::now();
                    let out = sc.scan(marks, true)?;
                    let ms = t0.elapsed().as_secs_f64() * 1000.0;
                    iteration += 1;

                    // Always one current pair — each scan overwrites the last.
                    let tp = format!("{}/tree.txt", a.out);
                    let header = format!(
                        "# {}\n# {} interactive, {:.0} ms ({} ms settle), {} sessions, {} frames skipped, {} occluded, {} noise\n\n",
                        out.url, out.count, ms, out.settled_ms, out.sessions, out.skipped, out.occluded, out.noise
                    );
                    std::fs::write(&tp, format!("{}{}\n", header, out.tree))
                        .map_err(|e| e.to_string())?;

                    println!("{}", out.tree);
                    println!(
                        "\n{} interactive, {:.0} ms ({} ms settle) | sessions {}, skipped {}, occluded {}, noise {}",
                        out.count, ms, out.settled_ms, out.sessions, out.skipped, out.occluded, out.noise
                    );
                    println!("tree -> {}", tp);

                    // Geometry beside the tree, NOT inside it: tree.txt goes
                    // to the model verbatim and coordinates would be noise
                    // there. Same scan, separate file.
                    {
                        let mut m = serde_json::Map::new();
                        for (i, r) in &out.hits {
                            m.insert(i.to_string(), json!([r[0], r[1], r[2], r[3]]));
                        }
                        let hp = format!("{}/hits.json", a.out);
                        std::fs::write(
                            &hp,
                            serde_json::to_string(&json!({ "dpr": out.dpr, "hits": m }))
                                .unwrap_or_default(),
                        )
                        .map_err(|e| e.to_string())?;
                    }

                    if let Some(bytes) = out.screenshot.as_deref() {
                        let sp = format!("{}/shot.jpg", a.out);
                        std::fs::write(&sp, bytes).map_err(|e| e.to_string())?;
                        println!("shot -> {}", sp);
                    }

                    // FRONTEND, DEBUG off: the un-annotated frame for the UI.
                    if let Some(bytes) = out.plain.as_deref() {
                        let pp = format!("{}/shot_plain.jpg", a.out);
                        std::fs::write(&pp, bytes).map_err(|e| e.to_string())?;
                        println!("plain -> {}", pp);
                    }

                    // DEBUG: this scan's own folder, kept across scans (the
                    // pair above is overwritten every time).
                    if DEBUG {
                        let dir =
                            write_debug(iteration, &header, &out.tree, out.screenshot.as_deref())?;
                        println!("debug -> {}/", dir);
                    }
                }

                other => println!("? {}   (h for help)", other),
            }
            Ok(())
        })();

        match res {
            Ok(()) => {}
            Err(e) if e == "__quit__" => break,
            Err(e) => println!("! {}", e),
        }
    }

    println!("browser still running on port {}", a.port);
}