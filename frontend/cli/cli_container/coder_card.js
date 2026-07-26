// CoderCard — a reusable live card component for ONE CLI/coder agent.
//
// Small + self-contained so the orchestrator (cli/cli_stage.js) creates one per agent and
// can stack several later. Look ported from coder_animation.html + the proven native banner
// (banner.py COMPACT_HTML). Styles: cli/cli_container/coder_card.css (scoped .coder-card).
//
// API: create(desc) -> { el, setLine, addMinion, setMinionLine, endMinion,
//                        setWeb, setDone, dispose }
(function () {
    'use strict';

    function orbLoaderHTML() {
        var s = '';
        for (var k = 0; k < 5; k++) s += '<i style="--index:' + k + '"></i>';
        return '<span class="cc-loader show">' + s + '</span>';
    }

    // NO-DROP, CONSTANT-SPEED line streamer. A faithful port of the proven CLI pill
    // (frontend/script.js _pumpCliRunner) and the native remote-connection banner
    // (remote_connection/banner.py pump): every incoming line is QUEUED and played one at a
    // time, each letter revealed left-to-right at a FIXED cadence — the pace NEVER depends on
    // line length or how much is queued. Long lines PAGINATE (overflow → hold → clear →
    // continue) so the COMPLETE content flows by, never ellipsis-clipped. We're explicitly OK
    // with the UI lagging real time: smoothness beats catching up. Shared by the `>` output
    // line and every minion row so they behave identically.
    var STAGGER   = 4;     // ms between ticks — fast & constant, never length-based
    var STEP      = 5;     // chars revealed per tick (timers clamp ~4ms, so batch for real speed)
    var CHAR_FADE = 30;    // ms opacity 0→1 fade per letter
    var PAGE_HOLD = 280;   // ms a full page lingers before clearing for the next page
    var LINE_HOLD = 110;   // ms between distinct lines

    // opts.multiline: true  -> a 5-line scrolling terminal (the coder `>` output). Each pumped
    //   line APPENDS a wrapping .cc-line into a translateY conveyor (.cc-scroll); the viewport
    //   (`target`) is clamped to N rows in CSS so older rows scroll up out of view, and the
    //   bottom is re-pinned as the active line wraps — like terminal scrollback.
    // opts (default)        -> the original single-line paginating ticker (minion rows): one
    //   .cc-line page, no-drop, overflow holds → clears → continues on a fresh page.
    function makeLineStreamer(target, initial, opts) {
        opts = opts || {};
        var multiline = !!opts.multiline;
        var queue = [];
        var running = false;
        var timer = null;
        // Per-streamer pace, defaulting to the module constants. The Shell-use terminal
        // overrides these to run faster: its viewport is only 3 lines and each packet
        // REPLACES it, so a step's lines have to land before the next packet arrives.
        var TICK_MS = opts.stagger || STAGGER;
        var TICK_CHARS = opts.step || STEP;
        var HOLD_MS = (opts.lineHold != null) ? opts.lineHold : LINE_HOLD;
        // Queue sentinel (identity-compared, never a string): "wipe the screen HERE".
        // Sits between one step's lines and the next's — see reset().
        var CLEAR = {};

        function hardClear() {
            if (multiline) {
                scroll.replaceChildren();
                scroll.style.transform = 'translateY(0)';
                target.classList.remove('cc-scrolled');
                activeLine = null;
            } else {
                target.replaceChildren();
            }
        }

        // Multiline: the append-only conveyor + the line currently shimmering. maxLines drives the
        // viewport height via a CSS var (CSS computes the px from line-height).
        var scroll = null, activeLine = null;
        if (multiline) {
            scroll = document.createElement('div');
            scroll.className = 'cc-scroll';
            target.style.setProperty('--cc-max-lines', String(opts.maxLines || 5));
            target.replaceChildren(scroll);
        }
        // Two scroll modes. Default: the conveyor is translateY'd so old lines slide up
        // out of a fixed window — smooth, but the user can't go back. Native mode (the
        // hand-typed terminal): real scrollTop, so old output stays reachable.
        var nativeScroll = false;
        function scrollToEnd() {
            if (!scroll) return;
            if (nativeScroll) {
                // Follow the tail ONLY if the user is already at the bottom. If they've
                // scrolled up to read something, yanking them back down would be hostile.
                var slack = target.scrollHeight - target.scrollTop - target.clientHeight;
                if (slack <= 24) target.scrollTop = target.scrollHeight;
                return;
            }
            var over = Math.max(0, scroll.scrollHeight - target.clientHeight);
            scroll.style.transform = over ? ('translateY(' + (-over) + 'px)') : 'translateY(0)';
            target.classList.toggle('cc-scrolled', over > 0);   // fade the top edge ONLY once it scrolls
        }

        // Render text into a single static page (no animation) — used for the initial label so
        // the row never flashes empty; the first streamed line replaces it (single-line) / appends
        // below it and scrolls away (multiline).
        function renderStatic(text) {
            var page = document.createElement('div');
            page.className = 'cc-line';
            page.textContent = text;
            if (multiline) { scroll.replaceChildren(page); activeLine = page; scrollToEnd(); }
            else { target.replaceChildren(page); }
        }
        if (initial != null) renderStatic(String(initial));

        function pump() {
            if (running || !queue.length) return;
            running = true;
            var item = queue.shift();
            // Step boundary reached: the previous step has finished drawing, so wipe now
            // and carry straight on with the next step's first line.
            if (item === CLEAR) { hardClear(); running = false; pump(); return; }
            // A queue entry is a bare string, or {text, cls} when the caller wants the
            // rendered line tagged (the terminal marks its prompt echoes this way).
            var plain = (typeof item === 'string');
            var text = plain ? item : item.text;
            var lineCls = plain ? '' : (' ' + item.cls);
            // Array.from splits by code point so emoji/surrogate pairs stay intact.
            var chars = Array.from(text);
            if (!chars.length) { running = false; pump(); return; }

            if (multiline) {
                // The previously finished line shimmered while waiting — stop it now that a new
                // line begins (matches the single-line path: dark text streams, THEN shimmers).
                if (activeLine) activeLine.classList.remove('cc-shimmer');
                var mline = document.createElement('div');
                mline.className = 'cc-line' + lineCls;   // pre-wrap (CSS) -> real multi-row wrapping
                scroll.appendChild(mline);
                activeLine = mline;
                while (scroll.children.length > 120) scroll.removeChild(scroll.firstChild);   // memory cap (off-screen rows)
                scrollToEnd();

                var mi = 0;
                (function mtick() {
                    for (var n = 0; n < TICK_CHARS; n++) {
                        if (mi >= chars.length) {
                            // Line fully streamed — flatten spans and add the loading shimmer
                            // (shines until the next line starts / the step ends).
                            mline.textContent = mline.textContent;
                            mline.classList.add('cc-shimmer');
                            timer = setTimeout(function () { running = false; pump(); }, HOLD_MS);
                            return;
                        }
                        var firstOnLine = mline.childElementCount === 0;
                        let span = document.createElement('span');   // let: own binding per char (fade closure)
                        span.className = 'cc-char';
                        span.textContent = chars[mi];
                        span.style.opacity = '0';
                        span.style.transition = 'opacity ' + CHAR_FADE + 'ms ease-out';
                        mline.appendChild(span);
                        // First char of a line shows instantly (no empty flash); the rest fade in.
                        if (firstOnLine) { span.style.opacity = '1'; }
                        else { requestAnimationFrame(function () { span.style.opacity = '1'; }); }
                        mi++;
                    }
                    scrollToEnd();                       // line may have wrapped onto a new row -> re-pin bottom
                    timer = setTimeout(mtick, TICK_MS);
                })();
                return;
            }

            // ---- single-line paginating path (minion rows) — unchanged ----
            var page = null;
            function startPage() {
                page = document.createElement('div');
                page.className = 'cc-line' + lineCls;
                target.replaceChildren(page);
            }
            startPage();

            var i = 0;
            (function tick() {
                for (var n = 0; n < TICK_CHARS; n++) {     // reveal a few chars per tick for speed
                    if (i >= chars.length) {
                        // Line fully streamed — flatten the char spans to plain text and add a
                        // loading shimmer (it shines until the next line replaces it / the step ends).
                        page.textContent = page.textContent;
                        page.classList.add('cc-shimmer');
                        timer = setTimeout(function () { running = false; pump(); }, HOLD_MS);
                        return;
                    }
                    var firstOnPage = page.childElementCount === 0;
                    let span = document.createElement('span');   // let: own binding per char (fade closure)
                    span.className = 'cc-char';
                    span.textContent = chars[i];
                    span.style.opacity = '0';
                    span.style.transition = 'opacity ' + CHAR_FADE + 'ms ease-out';
                    page.appendChild(span);
                    // Did this char push past the box's right edge? Measure on the page div (it has
                    // overflow:hidden so the overflow doesn't propagate). If it doesn't fit and isn't
                    // the only char on the page, retract it, hold the page, then restart it at the
                    // left edge of a fresh page.
                    if (page.scrollWidth > page.clientWidth + 1 && !firstOnPage) {
                        page.removeChild(span);
                        timer = setTimeout(function () {
                            startPage();
                            while (i < chars.length && /\s/.test(chars[i])) i++;   // drop leading space
                            tick();
                        }, PAGE_HOLD);
                        return;
                    }
                    // First char of a page shows instantly (no empty flash); the rest fade in.
                    if (firstOnPage) { span.style.opacity = '1'; }
                    else { requestAnimationFrame(function () { span.style.opacity = '1'; }); }
                    i++;
                }
                timer = setTimeout(tick, TICK_MS);
            })();
        }

        return {
            // push(text) — a plain line. push(text, cls) — the rendered .cc-line also gets
            // `cls`, so callers can mark specific lines (the terminal tags prompt echoes).
            push: function (text, cls) {
                if (text == null || !String(text).trim()) return;
                queue.push(cls ? { text: String(text), cls: String(cls) } : String(text));
                if (queue.length > 600) queue.splice(0, queue.length - 600);   // memory safety only
                if (!running) pump();
            },
            // Start a fresh screen for the step that's about to be pushed, so the terminal
            // only ever shows one step at a time.
            //
            // Idle → wipe now. Mid-flight → do NOT cut the current step off: queue the wipe
            // so it lands after the last of its lines and before the new step's first. Two
            // packets in quick succession therefore show step N in full, then step N+1 —
            // rather than truncating N the moment N+1 arrives.
            reset: function () {
                if (!running && !queue.length) { hardClear(); return; }
                queue.push(CLEAR);
            },
            // Swap the conveyor for real scrolling (and back). Native mode drops the
            // translateY so scrollTop is free to move, and the top-fade mask goes with
            // it — a mask over scrollback just hides the line you scrolled up to read.
            setNativeScroll: function (on) {
                nativeScroll = !!on;
                if (!scroll) return;
                if (nativeScroll) {
                    scroll.style.position = 'static';
                    scroll.style.transform = 'none';
                    target.classList.remove('cc-scrolled');
                    target.scrollTop = target.scrollHeight;
                } else {
                    scroll.style.position = '';
                    scroll.style.transform = 'translateY(0)';
                    target.scrollTop = 0;
                    scrollToEnd();
                }
            },
            dispose: function () { if (timer) { clearTimeout(timer); timer = null; } queue.length = 0; running = false; }
        };
    }

    // The agent's `action` array IS the list of tools used — and it already arrives in the
    // validated JSON we stream to the `>` line / minion rows. So we DON'T need any extra
    // backend events: we just reassemble that JSON from the streamed lines and read `action`.
    // A line buffer that JSON.parse()s after each line; calls onObject(obj) per complete
    // object, then resets. Non-JSON lines (when idle) are ignored; a runaway buffer is capped.
    function makeActionParser(onObject) {
        var buf = '';
        return function (line) {
            var s = String(line == null ? '' : line);
            if (buf === '' && s.replace(/^\s+/, '').charAt(0) !== '{') return;  // only start on a JSON object
            buf += s + '\n';
            try {
                var obj = JSON.parse(buf);
                buf = '';
                if (obj && typeof obj === 'object') onObject(obj);
            } catch (e) {
                if (buf.length > 200000) buf = '';   // safety: drop a malformed/runaway buffer
            }
        };
    }

    // Everything the agent REPLIED except `action` — thinking / memory / next_goal and
    // whatever the schema grows later, in the order the model wrote them. This is what the
    // Shell-use terminal streams: the agent's own words, not a synthesized per-action
    // narration. "null" thinking (the fast-mode skip convention) drops out.
    function proseLines(obj) {
        var out = [];
        if (!obj || typeof obj !== 'object') return out;
        Object.keys(obj).forEach(function (k) {
            if (k === 'action' || obj[k] == null) return;
            var v = obj[k];
            var text = String(typeof v === 'string' ? v : JSON.stringify(v)).trim();
            if (!text || text.toLowerCase() === 'null') return;
            out.push(text);
        });
        return out;
    }

    // The most label-worthy field of an action, for the icon's caption.
    function argOf(a) {
        return a.pattern || a.path || a.command || a.value || a.query || '';
    }
    function actionList(obj) {
        var actions = (obj && obj.action) || [];
        return Array.isArray(actions) ? actions : [actions];
    }

    // A clean, terminal-command-style narration line for ONE action — what the coder is
    // actually doing this step. We synthesize a readable line here instead of dumping the raw
    // JSON onto the `>` terminal.
    //
    // `all` = narrate EVERY action type. Without it, scratchpad/todo actions return '' and the
    // caller skips them, because the compact card surfaces those elsewhere (scratchpad → the
    // right tracker, todo → nowhere). The Shell-use terminal passes all=true: it's meant to be
    // the complete record of the step, so an action that produced no line there read as a
    // dropped packet.
    function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
    // How many "- [ ]" / "- [x]" rows a todo_list action wrote (its value is markdown).
    function todoCount(a) {
        var m = String(argOf(a)).match(/- \[[ xX]\]/g);
        return m ? m.length : 0;
    }
    function narrationFor(a, all) {
        if (!a || typeof a !== 'object' || !a.type) return '';
        switch (a.type) {
            case 'shell':   return '$ ' + clip(a.command || a.value || '', 240);
            case 'view':    return 'view ' + (a.path || '') + ((a.start || a.end) ? ('  :' + (a.start || 0) + '-' + (a.end || 0)) : '');
            case 'grep':    return 'grep "' + clip(a.pattern || '', 120) + '"' + (a.path ? (' in ' + a.path) : '') + (a.glob ? (' (' + a.glob + ')') : '');
            case 'glob':    return 'glob ' + (a.pattern || a.path || '');
            case 'write':   return 'write ' + (a.path || '');
            case 'replace': return 'edit ' + (a.path || '');
            case 'web':     return 'web "' + clip(a.value || a.query || '', 160) + '"';
            case 'wait':    return 'wait ' + (a.value || '1') + 's';
            case 'minion':  return 'minion: ' + clip(a.value || a.query || '', 200);
            case 'exit':    return 'done' + (a.value ? (' — ' + clip(a.value, 240)) : '');
            // Surfaced elsewhere on the compact card, so silent there — but narrated in full
            // on the Shell-use terminal. todo_list's value is the whole markdown list, so it's
            // summarised by item count rather than dumped into a 3-line window.
            case 'scratchpad':  return all ? ('note: ' + clip(argOf(a), 200)) : '';
            case 'todo_list':   return all ? ('todo list: ' + todoCount(a) + ' items') : '';
            case 'update_todo': return all ? ('todo #' + (a.value || '?') + ' complete') : '';
            default:        return a.type + (argOf(a) ? (' ' + clip(argOf(a), 200)) : '');
        }
    }

    // opts.header: <name> — the Shell-use card. Instead of leading with `>` it splits the
    //   step in two: a blinking dot + the name on top, then the agent head with the reply's
    //   PROSE (every JSON key except `action`) ticking beside it ONE line at a time, then
    //   the `>` terminal below carrying the ACTIONS — 10 lines deep, since Shell use gives
    //   the card the whole panel. Asked for by cli_stage.js only; dispatched coder cards in
    //   the other modes are unchanged: no header, actions on a 5-line `> AutoUse Code`.
    function create(desc, opts) {
        opts = opts || {};
        var header = opts.header || '';
        var el = document.createElement('div');
        el.className = 'coder-card' + (header ? ' has-head' : '');
        // The terminal card holds ONLY the streamed output + todo + minion output. Nothing else
        // (the action chain and scratchpad live in their own zones beside the card).
        // With a header the body is a ROW: .cc-main holds the terminal, with an (initially
        // empty) todo column to its RIGHT — the wrapper is what lets the two sit side by
        // side without the head/brain/out/minions each becoming their own column.
        var inner =
            (header
                ? '<div class="cc-head"><span class="cc-hdot"></span><span class="cc-hname"></span></div>' +
                  '<div class="cc-brain"><canvas class="cc-mascot"></canvas><span class="cc-think"></span></div>'
                : '') +
            // Shell use: the `>` prompt is a REAL input you can click and type into, with the
            // output view stacked below it — a plain terminal. Compact card: no input, and
            // .cc-out-name is a PINNED row (its "AutoUse Code" label used to live inside the
            // scrolling conveyor, so it scrolled away once the output overflowed).
            (header
                  // .cc-term wraps the prompt and the output so the two modes differ by
                  // flex-direction alone: ROW in AI mode (output beside the `>`, wrapped
                  // lines staying in that column and clear of the minion trunk below it),
                  // COLUMN while typing (your input on the `>` line, output underneath).
                ? '<div class="cc-term">' +
                      '<div class="cc-out"><span class="cc-p">&gt;</span>' +
                          // contenteditable, not <input>: the blinking `_` is a ::after on
                          // this element, so it lands immediately after the typed text with
                          // nothing to measure.
                          '<span class="cc-cmd" contenteditable="plaintext-only" role="textbox" ' +
                                'spellcheck="false" aria-label="terminal command"></span>' +
                      '</div>' +
                      '<div class="cc-out-text"><div class="cc-out-view"></div></div>' +
                  '</div>'
                : '<div class="cc-out"><span class="cc-p">&gt;</span> <span class="cc-out-text">' +
                      '<div class="cc-out-name">AutoUse Code</div>' +
                      '<div class="cc-out-view"></div>' +
                  '</span></div>') +
            '<div class="cc-minions"><span class="cc-trunk"></span></div>';
        el.innerHTML =
            '<div class="cc-body">' +
                (header ? '<div class="cc-main">' + inner + '</div><div class="cc-todo"></div>' : inner) +
            '</div>' +
            '<div class="cc-progress"><span class="cc-fill"></span></div>';
        if (header) el.querySelector('.cc-hname').textContent = header;

        var outEl = el.querySelector('.cc-out');
        var pEl = el.querySelector('.cc-p');           // the `>` prompt — trunk anchors just below it
        var outText = el.querySelector('.cc-out-view');   // the scrolling viewport, not the wrapper
        var minionsEl = el.querySelector('.cc-minions');
        var trunk = el.querySelector('.cc-trunk');

        // The `>` terminal shows a clean, multi-line scrolling log of the coder's ACTIONS
        // (synthesized narration, NOT the raw JSON), auto-scrolling once it overflows.
        // Shell use: a 3-line window, run FAST — each packet wipes it (see reset() below),
        // so a step of 6 lines has to show its first 3, scroll to the rest and be done
        // before the next packet lands. The stream starts EMPTY in both variants: the name
        // lives outside the conveyor now (header row / pinned .cc-out-name), so it can't be
        // scrolled away by the output.
        var outStream = makeLineStreamer(
            outText,
            null,
            header ? { multiline: true, maxLines: 3, step: 12, lineHold: 45 }
                   : { multiline: true, maxLines: 5 }
        );
        // Shell use only: the agent's own words, ONE line at a time beside the head. A
        // single-line paginating ticker (same streamer the minion rows use), so a long
        // field flows left-to-right, holds, clears and continues instead of stacking up.
        var thinkStream = header ? makeLineStreamer(el.querySelector('.cc-think'), null) : null;
        var minionStreams = {};   // minion id -> its line streamer

        // LEFT zone — "Tool response: N tools used" + the vertical action-icon chain (extreme
        // left of the stage, mirroring the bottom-left card). The orchestrator places it.
        var chainEl = document.createElement('div');
        chainEl.className = 'cc-actions';
        chainEl.innerHTML =
            '<div class="cc-tools-label">Tool response: <span class="cc-tools-count">0</span> tools used</div>' +
            '<div class="cc-chain-flow"><div class="cc-chain"></div></div>';
        var toolsCountEl = chainEl.querySelector('.cc-tools-count');
        // UNIVERSAL tool counter: the coder card continues the SAME running total as the main
        // agent (bottom_left's #toolUsedCount). Coder AND minion tools bump it in real time, so the
        // card shows main-agent + coder + minion tools, and because it's the same element, the
        // total rolls back up to the main agent when the cli stage closes. Falls back to a local
        // count when the main-agent counter isn't present (e.g. the standalone test harness).
        var localCount = 0;
        function gCountEl() { return document.getElementById('toolUsedCount'); }
        function readCount() { var g = gCountEl(); return g ? (parseInt(g.textContent, 10) || 0) : localCount; }
        function showCount() { if (toolsCountEl) toolsCountEl.textContent = String(readCount()); }
        function bumpToolCount() {
            var g = gCountEl();
            if (g) g.textContent = String((parseInt(g.textContent, 10) || 0) + 1);   // universal counter
            else localCount += 1;                                                     // harness fallback
            showCount();
        }
        showCount();   // seed the card from the main agent's current count — continue from there

        // RIGHT zone — "tracking progress" (the coder's scratchpad), mirroring the top-right
        // container: NO logo; each scratchpad entry streams in char-by-char as a dot-bullet line.
        var trackEl = document.createElement('div');
        trackEl.className = 'cc-track';
        trackEl.innerHTML =
            '<div class="cc-track-label">tracking progress</div>' +
            '<div class="cc-track-flow"><div class="cc-track-tree"></div></div>';

        var ICONS = window.CliToolIcons;
        // the header's blinking agent head (same mark as the chain's `agent` icon)
        var mascotEl = el.querySelector('.cc-mascot');
        var mascot = (mascotEl && ICONS && ICONS.createMascot) ? ICONS.createMascot(mascotEl, 20) : null;
        var actionChain = ICONS ? ICONS.createChain(chainEl.querySelector('.cc-chain'), { orientation: 'vertical' }) : null;
        var tracker = ICONS ? ICONS.createTracker(trackEl.querySelector('.cc-track-tree'), trackEl.querySelector('.cc-track-flow'), trackEl) : null;
        var minionChains = {};         // minion id -> its own horizontal chain
        var minionParsers = {};        // minion id -> its action parser

        function pushActions(chain, obj, isCoder) {
            actionList(obj).forEach(function (a) {
                if (!a || typeof a !== 'object' || !a.type) return;
                // coder -> stream a clean, terminal-style narration line on the `>` output
                // (narrationFor returns '' for scratchpad/todo, so those are skipped here).
                // Shell use narrates EVERY action (all=true) — its terminal is the step's
                // full record, so nothing may silently produce no line.
                if (isCoder) { var n = narrationFor(a, !!header); if (n) outStream.push(n); }
                // scratchpad -> the right "tracking progress" stream (coder only; not minions)
                if (a.type === 'scratchpad') { if (isCoder && tracker) tracker.push(argOf(a)); return; }
                // every other real tool -> the icon chain; count it on the UNIVERSAL counter
                // (coder tools AND minion tools both bump — minions run inside the coder's turn).
                if (chain && chain.addAction({ name: a.type, arg: argOf(a) })) bumpToolCount();
            });
        }

        // Opening-phase flow, EXACTLY like the main agent's tool-flow: "communicating with llm
        // service" -> "thinking" -> (on the packet) "packet received" -> this step's tools ->
        // repeat. Driven purely by JSON-arrival timing — no backend signal: each received packet
        // ticks "thinking", plays its tools, then anticipates the next step.
        var thinkingStep = null, openTimer = null, toolsRevealed = false;
        function revealTools() { if (!toolsRevealed) { toolsRevealed = true; chainEl.classList.add('cc-tools-active'); } }
        function startOpening() {
            if (!actionChain) return;
            revealTools();
            actionChain.addStep({ shape: 'server', label: 'communicating with llm service' });
            clearTimeout(openTimer);
            openTimer = setTimeout(function () {
                thinkingStep = actionChain.addStep({ shape: 'loader', label: 'thinking', tick: true });
            }, 850);
        }
        function markReceived() {
            clearTimeout(openTimer);
            if (!thinkingStep && actionChain) thinkingStep = actionChain.addStep({ shape: 'loader', label: 'thinking', tick: true });
            if (thinkingStep) { thinkingStep.complete('packet received'); thinkingStep = null; }
        }
        function hasExit(obj) { return actionList(obj).some(function (a) { return a && a.type === 'exit'; }); }
        function hasMinion(obj) { return actionList(obj).some(function (a) { return a && a.type === 'minion'; }); }

        // ---- hand-typed terminal (Shell use) ----------------------------------------
        // The `>` prompt is a real input: Enter runs the command through /api/shell-exec
        // (zsh on macOS, PowerShell on Windows) and its output lands in the same stream the
        // agent writes to. No LLM involved — this is the user driving the shell directly.
        // A hand-typed command is LIVE: the route returns as soon as it spawns and the
        // output arrives afterwards through window.shellTermLines. termBusy marks that
        // window — Enter is ignored (one prompt, one command) and .cc-busy dims the
        // prompt — until window.shellTermEnd reports the exit code.
        var termBusy = false;
        function setBusy(on) {
            termBusy = !!on;
            el.classList.toggle('cc-busy', termBusy);
        }

        var cmdEl = el.querySelector('.cc-cmd');

        // Prompt label. Card-scope (not inside the block below) so begin() can clear it
        // too — under 'use strict' a function declared in a block isn't visible outside.
        // The location stays up for the WHOLE of user mode, not just while focused: it's
        // the terminal's identity, and it stamps every executed line.
        var curPath = '';
        function showPath(short) { if (short) { curPath = short; pEl.textContent = short + ' >'; } }
        function clearPrompt() { curPath = ''; pEl.textContent = '>'; }
        function loadPath() {
            fetch('/api/shell-cwd')
                .then(function (r) { return r.json(); })
                .then(function (d) { if (!el.classList.contains('cc-running')) showPath(d.short); })
                .catch(function () { /* leave the plain `>` */ });
        }

        // Output exists → the prompt belongs on the LAST line (CSS flips the order).
        // Until then it stays on top, so clicking an empty terminal doesn't fling the
        // prompt to the bottom of a tall empty box.
        function markOut() { el.classList.add('cc-has-out'); }

        if (cmdEl) {
            // NOTE: the path is NOT loaded here. An untouched terminal shows a bare `>_`,
            // exactly like AI mode; the location appears when you click into it.
            // While the prompt is focused the `>` is replaced by the shell's actual
            // location, so `cd` is obvious; it goes back to `>` on blur to keep the
            // agent view clean.
            // WKWebView refuses to wheel-scroll an overflow area inside a transformed
            // subtree, and #cliContainer carries a transform — so the view would look
            // scrollable and simply not move. scrollTop DOES work, so drive it from the
            // wheel event ourselves (same fix as the model dropdown in script.js).
            if (outText) outText.addEventListener('wheel', function (e) {
                if (outText.scrollHeight <= outText.clientHeight) return;
                outText.scrollTop += (e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY);
                e.preventDefault();
            }, { passive: false });

            cmdEl.addEventListener('focus', function () {
                // typing gets the full height down to the bar; a sent task puts it back
                // body-level, not card-level: the hard cap is enforced on the grid row
                // that holds the card, which a class on the card itself can't reach.
                document.body.classList.add('cli-typing');
                outStream.setNativeScroll(true);   // old output stays reachable
                loadPath();
            });
            // NOTE: no blur handler — the path is not a focus affordance, it belongs to
            // user mode as a whole. Only begin() takes it away.

            // Click anywhere on the card and you're at the prompt, like a real terminal —
            // except over the todo column, where text should stay selectable, and except
            // while the agent owns the terminal.
            el.addEventListener('mousedown', function (e) {
                if (el.classList.contains('cc-running')) return;
                if (e.target === cmdEl || (e.target.closest && e.target.closest('.cc-todo'))) return;
                setTimeout(function () { cmdEl.focus(); }, 0);
            });

            cmdEl.addEventListener('keydown', function (e) {
                // Ctrl+C — interrupt whatever is running, exactly like a terminal. Works
                // whether or not something IS running, so it's never a dead key.
                if (e.key === 'c' && (e.ctrlKey || e.metaKey) && !window.getSelection().toString()) {
                    e.preventDefault();
                    outStream.push('^C');
                    fetch('/api/shell-kill', { method: 'POST' }).catch(function () {});
                    return;
                }
                if (e.key !== 'Enter') return;
                e.preventDefault();                  // never insert a newline
                if (termBusy) return;                // a command owns the prompt right now
                var cmd = (cmdEl.textContent || '').trim();
                if (!cmd) return;
                cmdEl.textContent = '';
                // Echo the command stamped with WHERE it ran, so the scrollback reads like
                // a terminal's — the executed line scrolls up, the live prompt stays last.
                // tagged so it stands out from output in the scrollback (cc-prompt-line)
                outStream.push((curPath ? curPath + ' > ' : '$ ') + cmd, 'cc-prompt-line');
                markOut();                           // prompt drops to the last line now
                setBusy(true);
                fetch('/api/shell-exec', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ command: cmd })
                })
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    if (document.activeElement === cmdEl) showPath(d.short);   // cd moved us
                    // `cd` and failures answer inline and are already finished; a live
                    // command answers 'started' and ends via window.shellTermEnd.
                    if (d.status === 'started') return;
                    var text = String(d.output || '').replace(/\s+$/, '');
                    if (text) text.split('\n').forEach(function (l) { outStream.push(l); });
                    else if (d.error) outStream.push(d.error);
                    setBusy(false);
                })
                .catch(function () { outStream.push('failed to reach the shell'); setBusy(false); });
            });
        }

        // Stream one reply's prose into a streamer, a field per line (Shell use only).
        function streamProse(stream, obj) {
            proseLines(obj).forEach(function (t) { stream.push(t); });
        }

        var coderActionParser = makeActionParser(function (obj) {
            markReceived();                       // this JSON IS the packet -> tick "thinking"
            if (thinkStream) streamProse(thinkStream, obj);   // Shell use: words beside the head
            // Shell use: each packet REPLACES the terminal. The `>` area shows the step that
            // just arrived and nothing else — no scrollback from the previous step, and no
            // backlog of its lines still trickling out of the char pump underneath it.
            if (header) outStream.reset();
            pushActions(actionChain, obj, true);  // actions -> the `>` terminal (+ chain/count)
            // Anticipate the next step — but NOT after a dispatched minion: the coder is
            // blocked until that minion returns, so the chain would sit on "communicating
            // with llm service / thinking" while nothing is being asked. It stops at
            // "dispatched minion" and endMinion() restarts the opening phase.
            if (!hasExit(obj) && !hasMinion(obj)) startOpening();
        });

        function setLine(text) {
            // Don't push the raw line to the terminal anymore — it's JSON. The parser reads the
            // `action` array and pushActions() streams a clean narration line per action instead.
            coderActionParser(text);     // read `action` -> narration + opening phases + tools + scratchpad
        }

        // Step 1 begins: "communicating with llm service…". Skipped for the Shell-use IDLE
        // card, which may sit for minutes before a task is sent — it would spend that whole
        // time accumulating chain steps for an LLM call that hasn't happened. cli_stage.js
        // calls begin() when the idle card is promoted to a live run.
        if (!opts.idle) startOpening();

        // Anchor the single trunk so it starts just below the `>` prompt (a tiny CONSTANT gap,
        // NOT the centre of the now-tall 5-line terminal) and runs down to the LAST minion's
        // head centre. TRUNK_GAP keeps the "almost touching the >" look constant.
        // Gap between the `>` and where the trunk starts. The Shell-use card's prompt is
        // bigger and sits alone atop a wide terminal, so the trunk is cut further back to
        // leave clean air under the glyph; the compact card keeps its almost-touching look.
        var TRUNK_GAP = header ? 12 : 3;
        function layoutTrunk() {
            var rows = minionsEl.querySelectorAll('.cc-mrow');
            if (!rows.length) { trunk.style.height = '0'; return; }
            var mRect = minionsEl.getBoundingClientRect();
            var pRect = pEl.getBoundingClientRect();   // the `>` glyph, top line of the terminal
            // anchor to the minion's HEAD center (the row also holds a sub-chain below it)
            var lastHead = rows[rows.length - 1].querySelector('.cc-mrow-head') || rows[rows.length - 1];
            var lRect = lastHead.getBoundingClientRect();
            var topY = (pRect.top + pRect.height / 2) - mRect.top + TRUNK_GAP;   // just below the `>`
            var botY = (lRect.top + lRect.height / 2) - mRect.top;
            trunk.style.top = topY + 'px';
            trunk.style.height = Math.max(0, botY - topY) + 'px';
        }

        function findRow(id) {
            var rows = minionsEl.querySelectorAll('.cc-mrow');
            for (var i = 0; i < rows.length; i++) if (rows[i].dataset.id === String(id)) return rows[i];
            return null;
        }

        function addMinion(id, query) {
            var row = document.createElement('div');
            row.className = 'cc-mrow';
            row.dataset.id = String(id);
            row.innerHTML =
                '<div class="cc-mrow-head">' + orbLoaderHTML() + '<span class="mline"></span></div>' +
                '<div class="cc-msub"><div class="cc-mchain"></div></div>';
            minionsEl.appendChild(row);
            // Each minion streams its live output (initial = query) AND grows its own
            // horizontal action sub-chain, parsed from the same per-minion JSON.
            minionStreams[id] = makeLineStreamer(row.querySelector('.mline'), query || 'minion');
            if (ICONS) minionChains[id] = ICONS.createChain(row.querySelector('.cc-mchain'), { orientation: 'horizontal' });
            minionParsers[id] = makeActionParser(function (obj) {
                // Shell use: same rule as the coder — the minion's row streams its OWN
                // reply (action excluded) instead of the raw JSON it printed.
                if (header && minionStreams[id]) streamProse(minionStreams[id], obj);
                pushActions(minionChains[id], obj, false);
                var mc = row.querySelector('.cc-mchain');
                if (mc && mc.children.length) row.classList.add('has-tools');   // reveal the loader→sub-chain L
                // a growing sub-chain makes this row taller, pushing later rows down — re-anchor the trunk.
                if (window.requestAnimationFrame) requestAnimationFrame(layoutTrunk); else layoutTrunk();
            });
            if (window.requestAnimationFrame) requestAnimationFrame(layoutTrunk); else layoutTrunk();
        }

        function setMinionLine(id, line) {
            // Default: the raw line IS the row's text. In Shell use the parser below
            // streams the parsed prose instead, so the raw JSON never lands in the row.
            if (!header && minionStreams[id]) minionStreams[id].push(line);
            if (minionParsers[id]) minionParsers[id](line);   // read `action` -> this minion's chain
        }

        // Todo updates — dropped everywhere else (the agent owns its list). In Shell use the
        // moment the agent writes a list the terminal gives up its right quarter to show it,
        // and every later update re-renders (todo.md is rewritten, not appended).
        // payload is the JSON string service.py sends: {objective, tasks:[{text, done}]}.
        //
        // Deliberately built with the MAIN agent's todo classes (.todo-list / .todo-item /
        // .todo-check / .todo-spinner / .todo-text from container/bottom_right/). Those are
        // unscoped and loaded globally, so this list is pixel-identical to the Computer-use
        // card — same animated tick colour, same spinner on the current task — with no style
        // duplicated here. Row states mirror bottom_right.js's renderTodo().
        var todoEl = el.querySelector('.cc-todo');
        function setTodo(payload) {
            if (!header || !todoEl) return;
            var data;
            try { data = (typeof payload === 'string') ? JSON.parse(payload) : payload; }
            catch (e) { return; }
            var tasks = (data && data.tasks) || [];
            if (!tasks.length) return;                // nothing written yet — stay collapsed

            var firstUndone = -1;
            for (var k = 0; k < tasks.length; k++) { if (!tasks[k].done) { firstUndone = k; break; } }

            var list = document.createElement('div');
            list.className = 'todo-list';
            tasks.forEach(function (t, i) {
                var item = document.createElement('div');
                item.className = 'todo-item';
                var mark = document.createElement('span');
                var label = document.createElement('span');
                label.className = 'todo-text';
                if (t.done) { mark.className = 'todo-check done'; label.classList.add('is-done'); }
                else if (i === firstUndone) { mark.className = 'todo-spinner'; }   // in progress
                else { mark.className = 'todo-check'; }                            // pending
                label.textContent = t.text || ('task ' + (i + 1));   // textContent: never parse as HTML
                item.appendChild(mark); item.appendChild(label);
                list.appendChild(item);
            });
            todoEl.replaceChildren(list);
            el.classList.add('has-todo');             // opens the right column (CSS)
        }

        // The minion is done — DON'T wait for its streaming or show a mark. Immediately fade the
        // whole row (line + L + tool sub-chain) and collapse its height to 0 so the rows below
        // slide up; then remove it and re-anchor the trunk.
        function endMinion(id, status) {
            var row = findRow(id);
            if (!row || row.classList.contains('leaving')) return;   // idempotent (cliMinionEnd can repeat)

            // stop streaming into a row that's leaving
            if (minionStreams[id]) { minionStreams[id].dispose(); delete minionStreams[id]; }

            // pin current height so max-height animates content-height -> 0 (no easing dead-zone)
            row.style.maxHeight = row.offsetHeight + 'px';   // read forces reflow
            if (window.requestAnimationFrame) {
                requestAnimationFrame(function () { row.classList.add('leaving'); row.style.maxHeight = '0px'; });
            } else { row.classList.add('leaving'); row.style.maxHeight = '0px'; }

            // The chain paused at "dispatched minion" while the coder waited. Once the
            // LAST live minion is back the coder resumes talking to the LLM, so pick the
            // opening phase up again. `row` is excluded explicitly rather than by the
            // .leaving class — that class is only added on the next animation frame, so
            // it isn't set yet here; .leaving rows are mid-collapse and don't count either.
            var stillRunning = Array.prototype.filter.call(
                minionsEl.querySelectorAll('.cc-mrow'),
                function (r) { return r !== row && !r.classList.contains('leaving'); }
            ).length;
            if (!stillRunning) startOpening();

            var done = false;
            function finish() {
                if (done) return; done = true;
                row.removeEventListener('transitionend', onEnd);
                if (minionChains[id]) { minionChains[id].dispose(); delete minionChains[id]; }
                delete minionParsers[id];
                if (row.parentNode) row.parentNode.removeChild(row);
                if (window.requestAnimationFrame) requestAnimationFrame(layoutTrunk); else layoutTrunk();
            }
            function onEnd(e) { if (e.target === row && e.propertyName === 'max-height') finish(); }
            row.addEventListener('transitionend', onEnd);
            setTimeout(finish, 420);   // safety if transitionend never fires (off-screen / unmounted)
        }

        // Web has its own globe icon in the tool chain now — no "searching the web…" hint in
        // the terminal. Kept as a no-op so the existing pill_web_loading events stay harmless.
        function setWeb(on) {}

        function setDone(status, summary) {
            clearTimeout(openTimer);                                  // stop any pending "thinking"
            if (thinkingStep) { thinkingStep.complete('packet received'); thinkingStep = null; }
            if (summary) outStream.push(summary);   // prose summary -> straight to the terminal (not JSON, so bypass the parser)
            el.classList.add((status === 'error' || status === 'stopped') ? 'error' : 'done');
        }

        function dispose() {
            if (mascot) mascot.dispose();
            if (thinkStream) thinkStream.dispose();
            outStream.dispose();
            for (var k in minionStreams) if (minionStreams[k]) minionStreams[k].dispose();
            if (actionChain) actionChain.dispose();
            if (tracker) tracker.dispose();
            for (var m in minionChains) if (minionChains[m]) minionChains[m].dispose();
        }

        return {
            el: el,
            chainEl: chainEl,
            trackEl: trackEl,
            setLine: setLine,
            // Kick off the opening phase for a card created with {idle:true}. Called when
            // cli_stage.js promotes the waiting terminal into a live run: the terminal
            // becomes the AGENT's, so the hand-typing layout drops (back to the 3-line
            // window) and the prompt goes read-only — the cursor moves to the agent's
            // output line, which is where text is actually appearing.
            begin: function () {
                document.body.classList.remove('cli-typing');
                el.classList.add('cc-running');
                outStream.setNativeScroll(false);   // back to the agent's sliding window
                if (cmdEl) {
                    cmdEl.setAttribute('contenteditable', 'false');
                    cmdEl.textContent = '';                 // drop anything half-typed
                    if (document.activeElement === cmdEl) cmdEl.blur();
                }
                clearPrompt();               // bare `>`, and no stale path on echoed lines
                el.classList.remove('cc-has-out');
                startOpening();
            },
            // raw text straight onto the terminal, bypassing the JSON parser — used for
            // stage-level messages (e.g. a run that failed before it produced any output)
            // and for the live output of a hand-typed command.
            note: function (t) { outStream.push(t); markOut(); },
            // a hand-typed command exited — hand the prompt back
            termEnd: function (code) {
                if (code) outStream.push('[exit ' + code + ']');
                setBusy(false);
            },
            setTodo: setTodo,
            addMinion: addMinion,
            setMinionLine: setMinionLine,
            endMinion: endMinion,
            setWeb: setWeb,
            setDone: setDone,
            dispose: dispose,
        };
    }

    window.CliCoderCard = { create: create };
})();
