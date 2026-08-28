// Copyright 2026 Cursortouch — Auto-Use

//! LLMManager — routes requests to the correct LLM provider.
//!
//! The web driver speaks NATIVE TOOL CALLING — the tool registry below IS the
//! output contract. There is no JSON-envelope response schema: the prompt's
//! four blocks ride as tracking params on the calls themselves. The only
//! schema-less, tool-less path is mode="text" (the memory-compression
//! handoff), which wants plain prose.
//!
//! Exposed to Python as the `LLMManager` class with the same constructor and
//! surface the Python original had — CompressionController builds its second
//! text-mode manager from this class.

use std::collections::HashSet;
use std::io::IsTerminal;
use std::path::Path;
use std::sync::{Mutex, Once, OnceLock};
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use serde_json::{json, Map, Value};

use crate::browser::truthy;
use crate::agent::main_driver::view::py_str_of;
use crate::llm_provider::{anthropic, google, groq, openai, openrouter, perplexity, together};

/// The transcript carries an optional per-turn `provider_meta` on assistant
/// messages — the provider's OWN metadata for that turn. Only these providers
/// translate it; for everyone else the key is stripped before the request is
/// built (an unknown key there is a 400).
pub const META_KEY: &str = "provider_meta";
pub const META_PROVIDERS: [&str; 2] = ["openrouter", "google"];

// ---------------------------------------------------------------------------
// Tracking params — THREE in quality mode ({thinking, memory, next_goal}),
// `memory` alone in fast mode. The first call of a step carries them, later
// calls pass "".
// ---------------------------------------------------------------------------

pub fn main_track_params() -> &'static Vec<(String, String)> {
    static P: OnceLock<Vec<(String, String)>> = OnceLock::new();
    P.get_or_init(|| vec![
        ("thinking".to_string(), r#"Follow the <thinking> rules - the five labeled stages "OBSERVE: ... VERIFY: ... PROGRESS: ... PLAN: ... PREDICT: ..." (FULL) at think triggers, a short freeform paragraph (RECOVERY) on a local failure, or exactly "not required" when the SKIP TEST passes. Never empty. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step."#.to_string()),
        ("memory".to_string(), r#"Follow the <memory> rules - line 1 is the verdict on the previous step's guard, judged on the CURRENT page: "S<n> ok" or "S<n> fail: <short why>" ("S1 start" on the first step). Then key context (current site/page state, tool name + result) and, for any step touching UI, the "Targets: id N (tag/role/visible text)" line resolved from the CURRENT element_tree. 2-4 concise lines. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step."#.to_string()),
        ("next_goal".to_string(), r#"Follow the <next_goal> rules - "Doing: <this step> (ToDo: <task>). If <visible change>, then Next: <action on named target | think: <decision>>." Name successor targets by NAME/ROLE only, NEVER by [id] (ids are re-assigned every scan). Fill on the FIRST tool call of the step; pass "" on every additional call in the same step."#.to_string()),
    ])
}

pub fn main_track_params_fast() -> &'static Vec<(String, String)> {
    static P: OnceLock<Vec<(String, String)>> = OnceLock::new();
    P.get_or_init(|| vec![
        ("memory".to_string(), r#"Your ONLY reasoning field: the verdict on what the last action did to the page ("S<n> ok" / "S<n> fail: <why>"), plus the context the next step needs (site/page state, resolved [id]s, key values). Keep it tight. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step."#.to_string()),
    ])
}

/// Build one canonical tool def — name + parameters + description. All
/// params are required, so the controller always sees every field of an
/// action; the tracking params ride ahead of the action fields.
fn tool(name: &str, params: &[(&str, Value)], description: &str, track: &[(String, String)]) -> Value {
    let mut props = Map::new();
    for (b, d) in track {
        props.insert(b.clone(), json!({"type": "string", "description": d}));
    }
    for (k, schema) in params {
        props.insert((*k).to_string(), schema.clone());
    }
    let required: Vec<Value> = props.keys().map(|k| json!(k)).collect();
    let mut out = Map::new();
    out.insert("name".into(), json!(name));
    out.insert(
        "parameters".into(),
        json!({"type": "object", "properties": Value::Object(props), "required": required}),
    );
    if !description.is_empty() {
        out.insert("description".into(), json!(description));
    }
    Value::Object(out)
}

/// The MAIN DRIVER's registry — one tool per action type. route_action and
/// the frontend's tool-flow map key on these names and fields, so they must
/// never drift. Each description is the system prompt's former
/// <tool_capability> entry VERBATIM — the prompt no longer carries a tool
/// list, so this registry is the single source of tool documentation. The
/// only additions are schema-required params the prompt text never named
/// (click's `times`, input's `enter` false case), appended as extra rules.
fn main_tools(track: &[(String, String)]) -> Vec<Value> {
    vec![
        tool("new_tab", &[("value", json!({"type": "string"}))],
             r#"Open a new browser tab. ALWAYS recommended when the current tab already holds task-relevant content - never hijack an occupied tab; check <all_tabs> first.
  1. Format: new_tab {"value": "<url_or_empty>"} - keep value "" to open a blank tab when the destination url is unknown.
  2. Examples:
    1. new_tab {"value": "https://www.amazon.com"}
    2. new_tab {"value": ""} - a blank tab, for when the destination url is not known yet
  3. A page landing in a new tab is a NEW SURFACE: survey before routing deep (see <thinking>)."#, track),

        tool("switch_tab", &[("id", json!({"type": "integer"}))],
             r#"Switch to a tab that is already open, making it current so the next scan and every following action land on it.
  1. Format: switch_tab {"id": <n_from_all_tabs>}
  2. Example: switch_tab {"id": 2}
  3. `id` is the [n] from <all_tabs>, NOT an [id] from <element_tree>. They are separate numberings that both look like small integers.
  4. Prefer this over re-opening a page you already have: switching keeps that tab's session, scroll position and half-filled forms, while a fresh new_tab throws all of it away.
  5. The tab you land on is a NEW SURFACE: survey before routing deep (see <thinking>)."#, track),

        tool("close_tab", &[("id", json!({"type": "integer"}))],
             r#"Close a tab that is open, removing it from <all_tabs>. Everything it held - session, scroll position, half-filled forms - is gone for good.
  1. Format: close_tab {"id": <n_from_all_tabs>}
  2. Example: close_tab {"id": 3}
  3. `id` is the [n] from <all_tabs>, NOT an [id] from <element_tree>.
  4. Use it to tidy up a tab whose job is finished. NEVER close a tab that still holds task-relevant state; if in doubt, leave it open - an extra tab costs nothing, a closed one cannot be reopened.
  5. Closing the CURRENT tab moves you to a neighbouring tab. The LAST remaining tab cannot be closed - the action FAILS and says so; navigate it with `update_tab` instead.
  6. Closing RE-NUMBERS <all_tabs>: read the fresh list in the next input before any other tab action."#, track),

        tool("update_tab", &[("value", json!({"type": "string"}))],
             r#"Navigate the CURRENT tab to a url, replacing whatever it is showing. Same tab, new page - nothing is opened and nothing is closed.
  1. Format: update_tab {"value": "<url>"}
  2. Example: update_tab {"value": "https://www.wikipedia.org"}
  3. Use it when the current page has served its purpose and the tab can be reused: undoing a wrong turn, following a url you can construct directly, or leaving a redirect you did not want.
  4. NOT for a page you still need - that is `new_tab`, which leaves this one open. And never type a url into the browser's address bar or a new-tab search box: those are browser chrome, not page elements, so they carry no [id] and typing there is not a page interaction. `update_tab` is how you navigate.
  5. The page that loads is a NEW SURFACE: survey before routing deep (see <thinking>)."#, track),

        tool("navigate_tab", &[("value", json!({"type": "string", "enum": ["back", "forward", "reload"]}))],
             r#"Move the CURRENT tab without naming a destination - step through its history, or re-request the page it is on. Same tab throughout.
  1. Format: navigate_tab {"value": "<back|forward|reload>"}
  2. Examples:
    1. navigate_tab {"value": "back"} - return to the previous page, e.g. from a product page to the results you opened it from
    2. navigate_tab {"value": "reload"} - re-request the same url for a fresh copy of the page
  3. "back" undoes a wrong turn while keeping the page you came from intact - the results list, its scroll position and its filters are all still there, which re-searching would throw away. Prefer it over rebuilding a page you already had.
  4. "forward" only exists after a "back": it returns to the page you left.
  5. "reload" is for a page that is stuck or stale: a spinner that never resolved, a list that did not update after your action, a transient error page. It is NOT a verification step - it discards unsaved form input and closes any open dialog, so read the page first and reload only when a fresh load is genuinely what you need.
  6. If there is no page to go to, the action FAILS and says so - the tab is at the start or the end of its history. Do not retry the same move.
  7. Whatever loads is a NEW SURFACE: survey before routing deep (see <thinking>)."#, track),

        tool("click", &[("id", json!({"type": "integer"})), ("times", json!({"type": "integer"}))],
             r#"Single click on an interactable element (default click, nothing more).
  1. Format: click {"id": <id_from_element_tree>, "times": <1_or_2>}
  2. Positive examples:
    1. click {"id": 19, "times": 1} - a normal single click
    2. click {"id": 7, "times": 2} - a DOUBLE click on the same element
  3. Negative examples (NEVER emit these):
    1. click {"id": 0, "times": 1} - WRONG: 0 is not a real element id; always use an [id] from the current <element_tree>
    2. click {"id": 19, "times": 3} - WRONG: `times` above 2 is meaningless; use 1, or 2 for a double click
  4. Clicking a `collapsed` element expands it; its children arrive in the NEXT <element_tree>.
  5. `times` 1 is the normal case and what nearly every web control wants. Use 2 only where a double click is genuinely the gesture: a file-manager style row that opens on double click, selecting a word inside a text field. Values above 2 are clamped to 2.
  6. `times` 2 is ONE double-click gesture, not two clicks. If you actually want two separate clicks, emit two `click` calls with "times": 1 - a toggle pressed twice ends up back where it started."#, track),

        tool("hold_click", &[("id", json!({"type": "integer"})), ("time", json!({"type": "integer"}))],
             r#"Press and hold the element for a duration, then release.
  1. Format: hold_click {"id": <id_from_element_tree>, "time": <seconds>} - `time` is the hold duration in seconds.
  2. Example: hold_click {"id": 19, "time": 2}
  3. Use for press-and-hold controls ("hold to confirm" buttons, human-verification holds); for a normal click use `click`."#, track),

        tool("input", &[("id", json!({"type": "integer"})), ("value", json!({"type": "string"})), ("enter", json!({"type": "boolean"}))],
             r#"Clear the element, then type the value into it.
  1. Format: input {"id": <id_from_element_tree>, "value": "<text_to_type>", "enter": <true_or_false>}
  2. Positive examples:
    1. input {"id": 21, "value": "iphone 16", "enter": false} - fill the field, do not submit
    2. input {"id": 21, "value": "iphone 16", "enter": true} - fill AND submit, in one action
  3. Negative examples (NEVER emit these):
    1. input {"id": 0, "value": "iphone 16", "enter": false} - WRONG: 0 is not a real element id; always use an [id] from the current <element_tree>
    2. input {"id": 21, "value": "", "enter": false} - WRONG: an empty value types nothing; it only clears the field
  4. "enter": true presses Enter after typing - use it to submit searches/forms in the same action. There is no separate key tool here, so this is the ONLY way to submit a search box or a single-field form - never fill a field and stop before the submit that completes it.
  5. Use false when the field is one of several: an early Enter submits a half-filled form."#, track),

        tool("scroll", &[("id", json!({"type": "integer"})), ("direction", json!({"type": "string", "enum": ["up", "down", "left", "right"]}))],
             r#"Bring off-screen content into view. Changes only WHAT IS VISIBLE - nothing is activated, opened or submitted.
  1. Format: scroll {"id": <id_from_element_tree>, "direction": "<up|down|left|right>"}
  2. Examples:
    1. scroll {"id": 1, "direction": "down"} - the whole page down one screenful
    2. scroll {"id": 14, "direction": "down"} - scroll the list/panel/dropdown that [14] sits inside, leaving the page where it is
  3. `id` picks WHICH surface moves, because the scroll is delivered AT that element and whatever scrolls around it takes it. [1] is the page itself. For a list, dropdown, sidebar, panel, table or carousel, pass the [id] of ANY element you can see INSIDE it and that region scrolls while the page stays put - the container never needs an [id] of its own.
  4. One scroll moves about three quarters of the visible surface, so consecutive scrolls overlap and nothing is skipped.
  5. The result says whether anything actually moved. "NO EFFECT - nothing moved" means that surface is at its end in that direction: do not repeat it - change direction, change region, or scroll [1].
  6. Every [id] is re-assigned afterwards: read the new <element_tree> before acting on what you now see."#, track),

        tool("wait", &[("value", json!({"type": "string"}))],
             r#"Pause execution to allow page loading or to trigger a fresh page scan.
  1. Format: wait {"value": "<seconds>"}
  2. Example: wait {"value": "2"}
  3. Rarely needed: the scanner already waits for the page to stop fetching before every scan. Use it for something that is NOT network work - an animation settling, a countdown on the page - not for ordinary page loads."#, track),

        tool("scratchpad", &[("value", json!({"type": "string"}))],
             r#"Your durable note store - the record of MILESTONES ACHIEVED plus any key fact you need later. Follow <scratchpad>.
  1. Format: scratchpad {"value": "<one_line_verified_note>"}
  2. Examples:
    1. Smaller milestone: scratchpad {"value": "**Milestone:** signed in to amazon.com - account menu shows the user name"}
    2. Smaller milestone: scratchpad {"value": "**Milestone:** filters applied - 128GB + Prime delivery + 4 stars and up"}
    3. Greater milestone: scratchpad {"value": "**Done:** order placed on amazon.com - confirmation **#114-2698**"}
    4. Key fact: scratchpad {"value": "Product page: https://www.amazon.com/dp/B0DGHYDZR9 - iPhone 16 128GB"}
    5. Answer: scratchpad {"value": "**Key metric:** Disney+ revenue (Q3 2025) = **$2.1B**"}
  3. Write a milestone the moment one lands, at EVERY size. A smaller milestone (signed in, filters applied, the right product page reached, one form section filled, a cookie wall cleared) is recorded exactly like a greater one (order placed, booking confirmed, the answer to <user_request> found). The small ones are what tell a later step how far the route already got - without them a re-route restarts from zero.
  4. Only write after visual confirmation on the CURRENT page - never assume an action landed.
  5. One fact per call, one line. If several things are confirmed in the same step, emit one separate `scratchpad` call for each - never batch them into one entry.
  6. The live file is rendered in your input as <scratchpad> once any entries exist - check it before recording, so entries never duplicate.
  7. Use for: milestones (small and large), metrics/numbers/final answers, important findings, exact urls of pages that matter.
  8. Write `value` in Markdown - inline only (`**bold**`, backticks, links), never a line break."#, track),

        tool("todo_list", &[("value", json!({"type": "string"}))],
             r#"Create or re-capture the ToDo. Follow <todo_capability>.
  1. Format: todo_list {"value": "Objective: <goal>\n- [ ] <task_1>\n- [ ] <task_2>"}
  2. Example: todo_list {"value": "Objective: buy an iPhone 16 on amazon\n- [ ] open amazon.com\n- [ ] search for iphone 16\n- [ ] place the order"}"#, track),

        tool("update_todo", &[("value", json!({"type": "string"}))],
             r#"Mark ONE task complete - only after its effect is visually confirmed in the current input.
  1. Format: update_todo {"value": "<task_number>"}
  2. Example: update_todo {"value": "1"}"#, track),

        tool("done", &[("value", json!({"type": "string"}))],
             r#"Ends the loop - the completion tool. Follow <task_completion>: a dedicated final step, never combined with any other action.
  1. Format: done {"value": "<end_to_end_summary>"}
  2. Example: done {"value": "Opened amazon.com, searched for the iPhone 16 128GB and recorded its price: $799"}"#, track),
    ]
}

pub fn main_tools_quality() -> &'static Vec<Value> {
    static T: OnceLock<Vec<Value>> = OnceLock::new();
    T.get_or_init(|| main_tools(main_track_params()))
}

pub fn main_tools_fast() -> &'static Vec<Value> {
    static T: OnceLock<Vec<Value>> = OnceLock::new();
    T.get_or_init(|| main_tools(main_track_params_fast()))
}

/// Tools that don't exist for a single-tab (parallel) agent: its whole run
/// happens in one dedicated tab of a shared browser, so the tab lifecycle is
/// off the table. update_tab/navigate_tab stay — they act on the current tab.
const SINGLE_TAB_EXCLUDED: [&str; 3] = ["new_tab", "switch_tab", "close_tab"];

fn without_tab_tools(tools: &[Value]) -> Vec<Value> {
    tools
        .iter()
        .filter(|t| {
            t.get("name")
                .and_then(Value::as_str)
                .map(|n| !SINGLE_TAB_EXCLUDED.contains(&n))
                .unwrap_or(true)
        })
        .cloned()
        .collect()
}

pub fn main_tools_quality_single_tab() -> &'static Vec<Value> {
    static T: OnceLock<Vec<Value>> = OnceLock::new();
    T.get_or_init(|| without_tab_tools(main_tools_quality()))
}

pub fn main_tools_fast_single_tab() -> &'static Vec<Value> {
    static T: OnceLock<Vec<Value>> = OnceLock::new();
    T.get_or_init(|| without_tab_tools(main_tools_fast()))
}

/// Per-action defaults — guarantees route_action always receives every field
/// of an action, even one the model left out.
pub fn main_action_defaults() -> &'static Value {
    static D: OnceLock<Value> = OnceLock::new();
    D.get_or_init(|| {
        json!({
            "new_tab": {"value": ""},
            "switch_tab": {"id": 0},
            "close_tab": {"id": 0},
            "update_tab": {"value": ""},
            "navigate_tab": {"value": "reload"},
            "click": {"id": 0, "times": 1},
            "hold_click": {"id": 0, "time": 1},
            "input": {"id": 0, "value": "", "enter": false},
            "scroll": {"id": 0, "direction": "down"},
            "wait": {"value": ""},
            "scratchpad": {"value": ""},
            "todo_list": {"value": ""},
            "update_todo": {"value": ""},
            "done": {"value": ""},
        })
    })
}

pub fn main_tool_names() -> &'static HashSet<String> {
    static N: OnceLock<HashSet<String>> = OnceLock::new();
    N.get_or_init(|| {
        main_tools_quality()
            .iter()
            .filter_map(|t| t.get("name").and_then(Value::as_str))
            .map(str::to_string)
            .collect()
    })
}

// -- dialect emitters --------------------------------------------------------

/// OpenAI/OpenRouter/Groq/Together chat-completions function format.
pub fn tools_openai(registry: &[Value]) -> Vec<Value> {
    registry
        .iter()
        .map(|t| json!({"type": "function", "function": t}))
        .collect()
}

/// Chat-completions format with structured-outputs `strict` mode: the
/// provider then ENFORCES the schema at decode time (every `required` field
/// present, no extra keys) instead of treating it as advisory — without it a
/// model can omit `id` and the call still comes back. OpenAI-only: strict
/// demands `additionalProperties: false`, which Gemini's declaration parser
/// rejects, and Groq/OpenRouter route to models with uneven strict support;
/// for every non-strict provider, tool_calls_to_steps' missing-field reject
/// is the enforcement layer.
pub fn tools_openai_strict(registry: &[Value]) -> Vec<Value> {
    registry
        .iter()
        .map(|t| {
            let mut f = t.as_object().cloned().unwrap_or_default();
            if let Some(Value::Object(params)) = f.get_mut("parameters") {
                params.insert("additionalProperties".into(), json!(false));
            }
            f.insert("strict".into(), json!(true));
            json!({"type": "function", "function": Value::Object(f)})
        })
        .collect()
}

fn with_description(mut tool: Map<String, Value>, source: &Value) -> Value {
    if let Some(desc) = source.get("description").filter(|d| truthy(d)) {
        tool.insert("description".into(), desc.clone());
    }
    Value::Object(tool)
}

/// Anthropic Messages API tools format.
pub fn tools_anthropic(registry: &[Value]) -> Vec<Value> {
    registry
        .iter()
        .map(|t| {
            let mut m = Map::new();
            m.insert("name".into(), t.get("name").cloned().unwrap_or(Value::Null));
            m.insert(
                "input_schema".into(),
                t.get("parameters").cloned().unwrap_or(Value::Null),
            );
            with_description(m, t)
        })
        .collect()
}

/// Gemini function declarations.
pub fn tools_gemini(registry: &[Value]) -> Vec<Value> {
    registry
        .iter()
        .map(|t| {
            let mut m = Map::new();
            m.insert("name".into(), t.get("name").cloned().unwrap_or(Value::Null));
            m.insert(
                "parameters".into(),
                t.get("parameters").cloned().unwrap_or(Value::Null),
            );
            with_description(m, t)
        })
        .collect()
}

/// Perplexity agent API (Responses-style flat function tools).
pub fn tools_perplexity(registry: &[Value]) -> Vec<Value> {
    registry
        .iter()
        .map(|t| {
            let mut m = Map::new();
            m.insert("type".into(), json!("function"));
            m.insert("name".into(), t.get("name").cloned().unwrap_or(Value::Null));
            m.insert(
                "parameters".into(),
                t.get("parameters").cloned().unwrap_or(Value::Null),
            );
            with_description(m, t)
        })
        .collect()
}

// -- tool_calls -> steps -----------------------------------------------------

/// Python `int(x)` (falls back to Err for unparseable values).
fn int_of(v: &Value) -> Result<i64, ()> {
    match v {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .ok_or(()),
        Value::Bool(b) => Ok(if *b { 1 } else { 0 }),
        Value::String(s) => s.trim().parse::<i64>().map_err(|_| ()),
        _ => Err(()),
    }
}

/// Best-effort coercion to the default's type (models sometimes send '5' for 5).
fn coerce(value: &Value, default: &Value) -> Value {
    match default {
        Value::Bool(_) => {
            if let Value::Bool(_) = value {
                value.clone()
            } else {
                let s = py_str_of(value).trim().to_lowercase();
                json!(matches!(s.as_str(), "true" | "1" | "yes"))
            }
        }
        Value::Number(n) if n.is_i64() || n.is_u64() => match int_of(value) {
            Ok(i) => json!(i),
            Err(_) => default.clone(),
        },
        Value::String(_) => {
            if value.is_string() {
                value.clone()
            } else {
                json!(py_str_of(value))
            }
        }
        _ => value.clone(),
    }
}

/// Convert normalized provider tool calls into (actions, calls, rejects,
/// track): actions for route_action (tracking params STRIPPED), calls echoed
/// back next request keyed by id, rejects for unknown/empty calls, track =
/// the tracking params stitched from the step's calls.
#[allow(clippy::type_complexity)]
pub fn tool_calls_to_steps(
    tool_calls: &Value,
    track_params: &[(String, String)],
) -> (Vec<Value>, Vec<Value>, Vec<Value>, Vec<(String, String)>) {
    let mut actions: Vec<Value> = Vec::new();
    let mut calls: Vec<Value> = Vec::new();
    let mut rejects: Vec<Value> = Vec::new();
    let mut track: Vec<(String, String)> =
        track_params.iter().map(|(k, _)| (k.clone(), String::new())).collect();
    let names = main_tool_names();
    let defaults_map = main_action_defaults();

    let list = tool_calls.as_array().cloned().unwrap_or_default();
    for (i, call) in list.iter().enumerate() {
        let name = call
            .get("name")
            .filter(|v| truthy(v))
            .map(|v| py_str_of(v).trim().to_string())
            .unwrap_or_default();
        let args = match call.get("arguments") {
            Some(Value::Object(o)) => o.clone(),
            _ => Map::new(),
        };
        let call_id = call
            .get("id")
            .filter(|v| truthy(v))
            .map(py_str_of)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| format!("call_{i}"));
        for (key, slot) in track.iter_mut() {
            let v = args
                .get(key.as_str())
                .filter(|v| truthy(v))
                .map(|v| py_str_of(v).trim().to_string())
                .unwrap_or_default();
            if !v.is_empty() && slot.is_empty() {
                *slot = v;
            }
        }
        let defaults = defaults_map.get(&name).and_then(Value::as_object);
        let known_name = defaults.is_some() && names.contains(&name);
        if !known_name {
            let shown = if name.is_empty() { "(unnamed)" } else { name.as_str() };
            let mut sorted_names: Vec<&String> = names.iter().collect();
            sorted_names.sort();
            rejects.push(json!({
                "id": call_id,
                "name": shown,
                "arguments": Value::Object(args),
                "error": format!(
                    "No tool named '{shown}' exists. Available tools: {}. Call one of those instead.",
                    sorted_names.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ")
                ),
            }));
            continue;
        }
        let defaults = defaults.unwrap();
        // A MISSING action field is a schema violation, not an omitted
        // optional — every param on every tool is `required`. Letting the
        // defaults fill it would silently promote a malformed turn into a
        // real page action: an `input` missing `id` becomes id 0, which
        // targets nothing and then reports a misleading stale-id error —
        // observed in the wild as a run burning 8 straight steps
        // re-resolving a fresh id while never actually sending one. Reject
        // instead, naming the missing fields, so the model fixes the CALL.
        // (Tracking params are exempt: they are legitimately "" after the
        // first call of a step.)
        let missing: Vec<String> = defaults
            .keys()
            .filter(|k| !args.contains_key(k.as_str()))
            .cloned()
            .collect();
        if !missing.is_empty() {
            let mut fields: Vec<String> = defaults.keys().cloned().collect();
            fields.sort();
            rejects.push(json!({
                "id": call_id,
                "name": name,
                "arguments": Value::Object(args),
                "error": format!(
                    "'{name}' was called without its required field(s): {}. Every field must be present: {}. Re-issue the call with ALL of them filled in — never leave one out.",
                    missing.join(", "),
                    fields.join(", ")
                ),
            }));
            continue;
        }
        let mut action = Map::new();
        action.insert("type".into(), json!(name));
        for (key, default) in defaults {
            let value = match args.get(key) {
                Some(v) => coerce(v, default),
                None => default.clone(),
            };
            action.insert(key.clone(), value);
        }
        actions.push(Value::Object(action));
        // Echo back only the fields this tool actually HAS — models
        // sometimes add junk keys, and `calls` is replayed verbatim into the
        // next request, so a kept extra teaches the model to repeat it.
        let known: HashSet<&str> = defaults
            .keys()
            .map(String::as_str)
            .chain(track.iter().map(|(k, _)| k.as_str()))
            .collect();
        let kept: Map<String, Value> = args
            .iter()
            .filter(|(k, _)| known.contains(k.as_str()))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        calls.push(json!({"id": call_id, "name": name, "arguments": Value::Object(kept)}));
    }
    (actions, calls, rejects, track)
}

// -- environment -------------------------------------------------------------

/// python-dotenv's load_dotenv(): find a .env walking up from the package,
/// load KEY=VALUE lines, never overriding variables already set.
pub fn load_dotenv_once(start: &Path) {
    static ONCE: Once = Once::new();
    let start = start.to_path_buf();
    ONCE.call_once(move || {
        let mut dir = Some(start.as_path());
        while let Some(d) = dir {
            let candidate = d.join(".env");
            if candidate.is_file() {
                if let Ok(text) = std::fs::read_to_string(&candidate) {
                    for line in text.lines() {
                        let line = line.trim();
                        if line.is_empty() || line.starts_with('#') {
                            continue;
                        }
                        let line = line.strip_prefix("export ").unwrap_or(line);
                        if let Some((k, v)) = line.split_once('=') {
                            let key = k.trim();
                            let mut val = v.trim();
                            if (val.starts_with('"') && val.ends_with('"') && val.len() >= 2)
                                || (val.starts_with('\'') && val.ends_with('\'') && val.len() >= 2)
                            {
                                val = &val[1..val.len() - 1];
                            }
                            if !key.is_empty() && std::env::var_os(key).is_none() {
                                std::env::set_var(key, val);
                            }
                        }
                    }
                }
                return;
            }
            dir = d.parent();
        }
    });
}

// -- usage normalization -----------------------------------------------------

fn int_or_zero(v: Option<&Value>) -> i64 {
    match v {
        Some(v) if truthy(v) => int_of(v).unwrap_or(0),
        _ => 0,
    }
}

/// Normalize a provider usage dict to {input_tokens, output_tokens,
/// total_tokens, context_tokens}, tolerating both key styles.
/// context_tokens is the TRUE size of the prompt actually sent this turn —
/// cached tokens still occupy the context window, so the cache classes are
/// added back (exact for every provider; non-Anthropic sets them to 0).
pub fn normalize_usage(u: Option<&Value>) -> Value {
    let u = u.cloned().unwrap_or(json!({}));
    let inp = if u.get("input_tokens").is_some() {
        int_or_zero(u.get("input_tokens"))
    } else {
        int_or_zero(u.get("prompt_tokens"))
    };
    let out = if u.get("output_tokens").is_some() {
        int_or_zero(u.get("output_tokens"))
    } else {
        int_or_zero(u.get("completion_tokens"))
    };
    let tot = match int_or_zero(u.get("total_tokens")) {
        0 => inp + out,
        t => t,
    };
    let cache_read = int_or_zero(u.get("cache_read_input_tokens"));
    let cache_create = int_or_zero(u.get("cache_creation_input_tokens"));
    json!({
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": tot,
        "context_tokens": inp + cache_read + cache_create,
    })
}

// -- the manager -------------------------------------------------------------

/// Sub-agent emergency fallbacks — cover a SINGLE failing call, then dropped.
fn pick_cli_fallback(provider: &str, model: &str) -> Option<String> {
    let candidates: &[&str] = match provider {
        "groq" => &["qwen3.6-27b"],
        "openai" => &["gpt-5.6-luna", "gpt-5.6-terra"],
        "openrouter" => &["gemini-3.6-flash", "gpt-5.6-luna"],
        "anthropic" => &["claude-haiku-4.5", "claude-sonnet-5"],
        "google" => &["gemini-3.6-flash", "gemini-3.1-pro"],
        "perplexity" => &["gemini-3.6-flash", "gpt-5.6-luna"],
        "together" => &["minimax-m3", "inkling"],
        _ => &[],
    };
    // Vertex and AI-Studio are different clients — a vertex model may only
    // fall back to another vertex one.
    if provider == "google" && model.ends_with("-vertex") {
        return candidates
            .iter()
            .map(|c| format!("{c}-vertex"))
            .find(|c| c != model);
    }
    candidates.iter().map(|c| c.to_string()).find(|c| c != model)
}

pub enum ProviderImpl {
    OpenRouter(openrouter::service::OpenRouterProvider),
    Groq(groq::service::GroqProvider),
    OpenAI(openai::service::OpenAIProvider),
    Anthropic(anthropic::service::AnthropicProvider),
    Google(google::service::GoogleProvider),
    Perplexity(perplexity::service::PerplexityProvider),
    Together(together::service::TogetherProvider),
}

impl ProviderImpl {
    fn send(
        &self,
        messages: &[Value],
        model: &str,
        shot: Option<&str>,
    ) -> Result<Value, String> {
        match self {
            ProviderImpl::OpenRouter(p) => p.send_request(messages, model, shot),
            ProviderImpl::Groq(p) => p.send_request(messages, model, shot),
            ProviderImpl::OpenAI(p) => p.send_request(messages, model, shot),
            ProviderImpl::Anthropic(p) => p.send_request(messages, model, shot),
            ProviderImpl::Google(p) => p.send_request(messages, model, shot),
            ProviderImpl::Perplexity(p) => p.send_request(messages, model, shot),
            ProviderImpl::Together(p) => p.send_request(messages, model, shot),
        }
    }
}

pub enum SendOutcome {
    /// Native-tools mode: {"text", "tool_calls", "provider_meta"}.
    Native(Value),
    /// mode="text": plain prose.
    Text(String),
}

struct CoreState {
    model: String,
    has_vision: bool,
    display_name: String,
    model_info: Value,
    last_usage: Value,
    last_call_seconds: f64,
}

/// Manager to route requests to the correct LLM provider.
#[pyclass(frozen, name = "LLMManager")]
pub struct LLMManager {
    pub provider: String,
    pub model_short_name: String,
    pub runtime_api_key: Option<String>,
    /// Sub-agent flag — NOT a "native tools" switch. Stays false for the
    /// MAIN DRIVER because each provider gates its screenshot splice on it.
    pub cli_agent: bool,
    pub mode: String,
    pub speed: String,
    pub native_tools: bool,
    cli_fallback_model: Option<String>,
    primary_model_info: Value,
    provider_impl: ProviderImpl,
    state: Mutex<CoreState>,
}

fn resolve_model_info(provider: &str, short_name: &str) -> Value {
    match provider {
        "openrouter" => openrouter::view::get_model_info(short_name),
        "groq" => groq::view::get_model_info(short_name),
        "openai" => openai::view::get_model_info(short_name),
        "anthropic" => anthropic::view::get_model_info(short_name),
        "google" => google::view::get_model_info(short_name),
        "perplexity" => perplexity::view::get_model_info(short_name),
        "together" => together::view::get_model_info(short_name),
        _ => json!({"api_name": short_name, "vision": true, "display_name": short_name}),
    }
}

fn key_from_env(runtime: &Option<String>, env_key: &str, label: &str) -> PyResult<String> {
    if let Some(k) = runtime {
        if !k.is_empty() {
            return Ok(k.clone());
        }
    }
    match std::env::var(env_key) {
        Ok(k) if !k.is_empty() => Ok(k),
        _ => Err(PyValueError::new_err(format!(
            "{label} API key not provided and not found in .env file"
        ))),
    }
}

impl LLMManager {
    pub fn build(
        py: Python<'_>,
        provider: &str,
        model: &str,
        api_key: Option<String>,
        cli_agent: bool,
        mode: &str,
        speed: &str,
        single_tab: bool,
    ) -> PyResult<Self> {
        load_dotenv_once(crate::web_dir(py)?);
        let provider = provider.to_lowercase();
        let runtime_api_key = api_key;
        let native_tools = mode != "text";
        let cli_fallback_model = if cli_agent {
            pick_cli_fallback(&provider, model)
        } else {
            None
        };
        let model_info = resolve_model_info(&provider, model);

        // Hand each provider its dialect's tool definitions — the driver gets
        // its action tools, thinking-less in fast mode, tab-less in
        // single-tab (parallel) mode; mode="text" gets none.
        let registry: &Vec<Value> = match (speed == "fast", single_tab) {
            (true, true) => main_tools_fast_single_tab(),
            (true, false) => main_tools_fast(),
            (false, true) => main_tools_quality_single_tab(),
            (false, false) => main_tools_quality(),
        };
        let native = native_tools;
        let provider_impl = match provider.as_str() {
            "openrouter" => ProviderImpl::OpenRouter(openrouter::service::OpenRouterProvider {
                api_key: key_from_env(&runtime_api_key, "OPENROUTER_API_KEY", "OpenRouter")?,
                cli_agent,
                tools: native.then(|| tools_openai(registry)),
            }),
            "groq" => ProviderImpl::Groq(groq::service::GroqProvider {
                api_key: key_from_env(&runtime_api_key, "GROQ_API_KEY", "Groq")?,
                cli_agent,
                tools: native.then(|| tools_openai(registry)),
            }),
            "openai" => ProviderImpl::OpenAI(openai::service::OpenAIProvider {
                api_key: key_from_env(&runtime_api_key, "OPENAI_API_KEY", "OpenAI")?,
                cli_agent,
                tools: native.then(|| tools_openai_strict(registry)),
            }),
            "anthropic" => ProviderImpl::Anthropic(anthropic::service::AnthropicProvider::new(
                key_from_env(&runtime_api_key, "ANTHROPIC_API_KEY", "Anthropic")?,
                cli_agent,
                native.then(|| tools_anthropic(registry)),
            )),
            "google" => {
                let model_meta = google::view::get_model_info(model);
                let is_vertex = model_meta.get("vertex").and_then(Value::as_bool).unwrap_or(false);
                if is_vertex {
                    // Vertex config from autouse_data/api_key/api_key.txt —
                    // the SAME file the Settings panel writes.
                    let (mut project, mut location) = (None, None);
                    let read = (|| -> PyResult<()> {
                        let key_file: String = py
                            .import("Auto_Use")?
                            .getattr("api_key_file")?
                            .call0()?
                            .str()?
                            .extract()?;
                        if let Ok(text) = std::fs::read_to_string(&key_file) {
                            for line in text.lines() {
                                let line = line.trim();
                                if let Some(v) = line.strip_prefix("VERTEX_PROJECT_ID=") {
                                    project = Some(v.to_string());
                                } else if let Some(v) = line.strip_prefix("VERTEX_LOCATION=") {
                                    location = Some(v.to_string());
                                }
                            }
                        }
                        Ok(())
                    })();
                    let _ = read; // best-effort, like the Python try/except
                    let project = project.or_else(|| std::env::var("VERTEX_PROJECT_ID").ok());
                    let location = location
                        .or_else(|| std::env::var("VERTEX_LOCATION").ok())
                        .or(Some("global".to_string()));
                    ProviderImpl::Google(google::service::GoogleProvider::new(
                        None,
                        cli_agent,
                        true,
                        project,
                        location,
                        native.then(|| tools_gemini(registry)),
                    ))
                } else {
                    ProviderImpl::Google(google::service::GoogleProvider::new(
                        Some(key_from_env(&runtime_api_key, "GOOGLE_API_KEY", "Google")?),
                        cli_agent,
                        false,
                        None,
                        None,
                        native.then(|| tools_gemini(registry)),
                    ))
                }
            }
            "perplexity" => ProviderImpl::Perplexity(perplexity::service::PerplexityProvider {
                api_key: key_from_env(&runtime_api_key, "PERPLEXITY_API_KEY", "Perplexity")?,
                cli_agent,
                tools: native.then(|| tools_perplexity(registry)),
            }),
            "together" => ProviderImpl::Together(together::service::TogetherProvider {
                api_key: key_from_env(&runtime_api_key, "TOGETHER_API_KEY", "Together")?,
                cli_agent,
                tools: native.then(|| tools_openai(registry)),
                tool_choice_auto: std::sync::atomic::AtomicBool::new(false),
            }),
            other => {
                return Err(PyValueError::new_err(format!("Unsupported provider: {other}")));
            }
        };

        Ok(LLMManager {
            provider,
            model_short_name: model.to_string(),
            runtime_api_key,
            cli_agent,
            mode: mode.to_string(),
            speed: speed.to_string(),
            native_tools,
            cli_fallback_model,
            primary_model_info: model_info.clone(),
            provider_impl,
            state: Mutex::new(CoreState {
                model: model_info
                    .get("api_name")
                    .and_then(Value::as_str)
                    .unwrap_or(model)
                    .to_string(),
                has_vision: model_info.get("vision").and_then(Value::as_bool).unwrap_or(true),
                display_name: model_info
                    .get("display_name")
                    .and_then(Value::as_str)
                    .unwrap_or(model)
                    .to_string(),
                model_info,
                last_usage: json!({}),
                last_call_seconds: 0.0,
            }),
        })
    }

    fn apply_model_info(&self, info: &Value) {
        let mut state = self.state.lock().unwrap();
        state.model = info
            .get("api_name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        state.has_vision = info.get("vision").and_then(Value::as_bool).unwrap_or(true);
        state.display_name = info
            .get("display_name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        state.model_info = info.clone();
    }

    /// Three idempotent tries against the model currently loaded. Errors
    /// carry the last provider message.
    fn attempt(&self, messages: &[Value], shot: Option<&str>) -> Result<SendOutcome, String> {
        let mut last_error = String::new();
        for attempt in 0..3 {
            // Deep-copy per attempt so provider mutations cannot compound.
            let mut attempt_messages: Vec<Value> = messages.to_vec();
            if !META_PROVIDERS.contains(&self.provider.as_str()) {
                for m in attempt_messages.iter_mut() {
                    if let Some(obj) = m.as_object_mut() {
                        obj.shift_remove(META_KEY);
                    }
                }
            }
            let (model, display_name) = {
                let state = self.state.lock().unwrap();
                (state.model.clone(), state.display_name.clone())
            };
            let t0 = Instant::now();
            match self.provider_impl.send(&attempt_messages, &model, shot) {
                Ok(response) => {
                    let elapsed = t0.elapsed().as_secs_f64();
                    let usage = normalize_usage(response.get("usage"));
                    {
                        let mut state = self.state.lock().unwrap();
                        state.last_call_seconds = elapsed;
                        state.last_usage = usage.clone();
                    }
                    if std::io::stdout().is_terminal() {
                        // \r first: overwrite any spinner residue. TTY-only —
                        // never leak into the UI subprocess pipe.
                        let raw_usage = response.get("usage").cloned().unwrap_or(json!({}));
                        let cached = match int_or_zero(raw_usage.get("cache_read_input_tokens")) {
                            0 => int_or_zero(
                                raw_usage
                                    .get("prompt_tokens_details")
                                    .and_then(|d| d.get("cached_tokens")),
                            ),
                            c => c,
                        };
                        println!(
                            "\r⏱ LLM call: {elapsed:.2}s ({display_name}) | in {} (cached {cached}) | out {}",
                            int_or_zero(usage.get("input_tokens")),
                            int_or_zero(usage.get("output_tokens")),
                        );
                    }
                    let message = response
                        .get("choices")
                        .and_then(Value::as_array)
                        .and_then(|c| c.first())
                        .and_then(|c| c.get("message"))
                        .cloned();
                    let Some(message) = message else {
                        last_error = "provider response carried no choices[0].message".to_string();
                        if attempt < 2 {
                            println!("⚠️ {display_name} request failed (attempt {}/3): {last_error}", attempt + 1);
                            println!("   Retrying in 1 second with a fresh message copy...");
                            std::thread::sleep(Duration::from_secs(1));
                            continue;
                        }
                        println!("❌ {display_name} request failed after 3 attempts: {last_error}");
                        break;
                    };
                    if self.native_tools {
                        let content = message.get("content").cloned().unwrap_or(Value::Null);
                        return Ok(SendOutcome::Native(json!({
                            "text": if truthy(&content) { content } else { json!("") },
                            "tool_calls": message.get("tool_calls").cloned().unwrap_or(json!([])),
                            META_KEY: message.get(META_KEY).cloned().unwrap_or(json!({})),
                        })));
                    }
                    let content = message.get("content").cloned().unwrap_or(Value::Null);
                    return Ok(SendOutcome::Text(match content {
                        Value::String(s) => s,
                        other => py_str_of(&other),
                    }));
                }
                Err(e) => {
                    last_error = e;
                    if attempt < 2 {
                        println!(
                            "⚠️ {display_name} request failed (attempt {}/3): {last_error}",
                            attempt + 1
                        );
                        println!("   Retrying in 1 second with a fresh message copy...");
                        std::thread::sleep(Duration::from_secs(1));
                        continue;
                    }
                    println!("❌ {display_name} request failed after 3 attempts: {last_error}");
                }
            }
        }
        Err(last_error)
    }

    /// Send with idempotent retries; sub-agents only get one fallback call.
    /// GIL-free — callers detach around this.
    pub fn send_rust(&self, messages: &[Value], shot: Option<&str>) -> Result<SendOutcome, String> {
        match self.attempt(messages, shot) {
            Ok(out) => return Ok(out),
            Err(e) => {
                if !(self.cli_agent && self.cli_fallback_model.is_some()) {
                    return Err(e);
                }
            }
        }
        let fallback_model = self.cli_fallback_model.clone().unwrap();
        let fallback_info = resolve_model_info(&self.provider, &fallback_model);
        let display = self.state.lock().unwrap().display_name.clone();
        println!(
            "⚠️ {display} failed 3 attempts — this step only, falling back to {}...",
            fallback_info.get("display_name").and_then(Value::as_str).unwrap_or(&fallback_model)
        );
        self.apply_model_info(&fallback_info);
        let result = self.attempt(messages, shot);
        // Always revert: the fallback covers this call, not the whole run.
        self.apply_model_info(&self.primary_model_info.clone());
        match result {
            Ok(out) => {
                println!(
                    "↩️ Back on {} for the next step.",
                    self.state.lock().unwrap().display_name
                );
                Ok(out)
            }
            Err(e) => {
                println!(
                    "❌ Fallback {} also failed 3 attempts — stopping.",
                    fallback_info.get("display_name").and_then(Value::as_str).unwrap_or(&fallback_model)
                );
                Err(e)
            }
        }
    }

    pub fn last_usage_value(&self) -> Value {
        self.state.lock().unwrap().last_usage.clone()
    }
}

#[pymethods]
impl LLMManager {
    #[new]
    #[pyo3(signature = (provider, model, api_key=None, cli_agent=false, mode=None, speed=None))]
    fn new(
        py: Python<'_>,
        provider: String,
        model: String,
        api_key: Option<Bound<'_, PyAny>>,
        cli_agent: bool,
        mode: Option<String>,
        speed: Option<String>,
    ) -> PyResult<Self> {
        let api_key = match &api_key {
            Some(v) if !v.is_none() => Some(v.str()?.extract::<String>()?),
            _ => None,
        };
        LLMManager::build(
            py,
            &provider,
            &model,
            api_key,
            cli_agent,
            mode.as_deref().unwrap_or("main"),
            speed.as_deref().unwrap_or("quality"),
            false,
        )
    }

    /// Send request to the selected provider with idempotent retries.
    #[pyo3(signature = (messages, annotated_screenshot_base64=None))]
    fn send_request<'py>(
        &self,
        py: Python<'py>,
        messages: Bound<'py, PyAny>,
        annotated_screenshot_base64: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let msgs: Vec<Value> = match pythonize::depythonize(&messages)? {
            Value::Array(a) => a,
            other => vec![other],
        };
        let outcome = py
            .detach(|| self.send_rust(&msgs, annotated_screenshot_base64.as_deref()))
            .map_err(PyException::new_err)?;
        match outcome {
            SendOutcome::Native(v) => pythonize::pythonize(py, &v).map_err(Into::into),
            SendOutcome::Text(s) => Ok(pyo3::types::PyString::new(py, &s).into_any()),
        }
    }

    /// Current model short name (preserves vertex suffix for routing).
    fn get_model_name(&self) -> &str {
        &self.model_short_name
    }

    fn get_provider_name(&self) -> &str {
        &self.provider
    }

    #[getter]
    fn provider(&self) -> &str {
        &self.provider
    }

    #[getter]
    fn model_short_name(&self) -> &str {
        &self.model_short_name
    }

    #[getter]
    fn runtime_api_key(&self) -> Option<&str> {
        self.runtime_api_key.as_deref()
    }

    #[getter]
    fn cli_agent(&self) -> bool {
        self.cli_agent
    }

    #[getter]
    fn mode(&self) -> &str {
        &self.mode
    }

    #[getter]
    fn speed(&self) -> &str {
        &self.speed
    }

    #[getter]
    fn native_tools(&self) -> bool {
        self.native_tools
    }

    #[getter]
    fn model(&self) -> String {
        self.state.lock().unwrap().model.clone()
    }

    #[getter]
    fn display_name(&self) -> String {
        self.state.lock().unwrap().display_name.clone()
    }

    #[getter]
    fn has_vision(&self) -> bool {
        self.state.lock().unwrap().has_vision
    }

    #[getter]
    fn model_info<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        pythonize::pythonize(py, &self.state.lock().unwrap().model_info).map_err(Into::into)
    }

    #[getter]
    fn last_usage<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        pythonize::pythonize(py, &self.last_usage_value()).map_err(Into::into)
    }

    #[getter]
    fn last_call_seconds(&self) -> f64 {
        self.state.lock().unwrap().last_call_seconds
    }
}

// ---------------------------------------------------------------------------
// Test hook — the converter exposed to Python so it can be differentially
// tested against the original (and reused by any future Python caller).
// ---------------------------------------------------------------------------

#[pyfunction]
#[pyo3(signature = (tool_calls, speed=None))]
pub fn _tool_calls_to_steps_debug<'py>(
    py: Python<'py>,
    tool_calls: Bound<'py, PyAny>,
    speed: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let calls_val: Value = if tool_calls.is_none() {
        Value::Null
    } else {
        pythonize::depythonize(&tool_calls)?
    };
    let track_params = if speed.as_deref() == Some("fast") {
        main_track_params_fast()
    } else {
        main_track_params()
    };
    let (actions, calls, rejects, track) = tool_calls_to_steps(&calls_val, track_params);
    let track_map: Map<String, Value> =
        track.into_iter().map(|(k, v)| (k, Value::String(v))).collect();
    pythonize::pythonize(
        py,
        &json!([actions, calls, rejects, Value::Object(track_map)]),
    )
    .map_err(Into::into)
}
