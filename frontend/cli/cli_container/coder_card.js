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

        // Multiline: the append-only conveyor + the line currently shimmering. maxLines drives the
        // viewport height via a CSS var (CSS computes the px from line-height).
        var scroll = null, activeLine = null;
        if (multiline) {
            scroll = document.createElement('div');
            scroll.className = 'cc-scroll';
            target.style.setProperty('--cc-max-lines', String(opts.maxLines || 5));
            target.replaceChildren(scroll);
        }
        function scrollToEnd() {
            if (!scroll) return;
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
            var text = queue.shift();
            // Array.from splits by code point so emoji/surrogate pairs stay intact.
            var chars = Array.from(text);
            if (!chars.length) { running = false; pump(); return; }

            if (multiline) {
                // The previously finished line shimmered while waiting — stop it now that a new
                // line begins (matches the single-line path: dark text streams, THEN shimmers).
                if (activeLine) activeLine.classList.remove('cc-shimmer');
                var mline = document.createElement('div');
                mline.className = 'cc-line';   // pre-wrap (CSS) -> real multi-row wrapping
                scroll.appendChild(mline);
                activeLine = mline;
                while (scroll.children.length > 120) scroll.removeChild(scroll.firstChild);   // memory cap (off-screen rows)
                scrollToEnd();

                var mi = 0;
                (function mtick() {
                    for (var n = 0; n < STEP; n++) {
                        if (mi >= chars.length) {
                            // Line fully streamed — flatten spans and add the loading shimmer
                            // (shines until the next line starts / the step ends).
                            mline.textContent = mline.textContent;
                            mline.classList.add('cc-shimmer');
                            timer = setTimeout(function () { running = false; pump(); }, LINE_HOLD);
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
                    timer = setTimeout(mtick, STAGGER);
                })();
                return;
            }

            // ---- single-line paginating path (minion rows) — unchanged ----
            var page = null;
            function startPage() {
                page = document.createElement('div');
                page.className = 'cc-line';
                target.replaceChildren(page);
            }
            startPage();

            var i = 0;
            (function tick() {
                for (var n = 0; n < STEP; n++) {     // reveal a few chars per tick for speed
                    if (i >= chars.length) {
                        // Line fully streamed — flatten the char spans to plain text and add a
                        // loading shimmer (it shines until the next line replaces it / the step ends).
                        page.textContent = page.textContent;
                        page.classList.add('cc-shimmer');
                        timer = setTimeout(function () { running = false; pump(); }, LINE_HOLD);
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
                timer = setTimeout(tick, STAGGER);
            })();
        }

        return {
            push: function (text) {
                if (text == null || !String(text).trim()) return;
                queue.push(String(text));
                if (queue.length > 600) queue.splice(0, queue.length - 600);   // memory safety only
                if (!running) pump();
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
    // actually doing this step. The backend only streams the `action` array (not the prose
    // fields), so we synthesize a readable line here instead of dumping the raw JSON onto the
    // `>` terminal. Returns '' for actions surfaced elsewhere (scratchpad → right tracker;
    // todo → not shown), so the caller skips them.
    function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
    function narrationFor(a) {
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
            case 'scratchpad': case 'todo_list': case 'update_todo': return '';   // shown elsewhere / not shown
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
        el.innerHTML =
            '<div class="cc-body">' +
                (header
                    ? '<div class="cc-head"><span class="cc-hdot"></span><span class="cc-hname"></span></div>' +
                      '<div class="cc-brain"><canvas class="cc-mascot"></canvas><span class="cc-think"></span></div>'
                    : '') +
                '<div class="cc-out"><span class="cc-p">&gt;</span> <span class="cc-out-text"></span></div>' +
                '<div class="cc-minions"><span class="cc-trunk"></span></div>' +
            '</div>' +
            '<div class="cc-progress"><span class="cc-fill"></span></div>';
        if (header) el.querySelector('.cc-hname').textContent = header;

        var outEl = el.querySelector('.cc-out');
        var pEl = el.querySelector('.cc-p');           // the `>` prompt — trunk anchors just below it
        var outText = el.querySelector('.cc-out-text');
        var minionsEl = el.querySelector('.cc-minions');
        var trunk = el.querySelector('.cc-trunk');

        // The `>` terminal shows a clean, multi-line scrolling log of the coder's ACTIONS
        // (synthesized narration, NOT the raw JSON). Up to 5 lines visible, then it auto-scrolls.
        // With a header the name is already on top, so the stream starts empty instead of
        // repeating it as a first line that would just scroll away.
        var outStream = makeLineStreamer(outText, header ? null : 'AutoUse Code',
                                         { multiline: true, maxLines: 5 });
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
                if (isCoder) { var n = narrationFor(a); if (n) outStream.push(n); }
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

        // Stream one reply's prose into a streamer, a field per line (Shell use only).
        function streamProse(stream, obj) {
            proseLines(obj).forEach(function (t) { stream.push(t); });
        }

        var coderActionParser = makeActionParser(function (obj) {
            markReceived();                       // this JSON IS the packet -> tick "thinking"
            if (thinkStream) streamProse(thinkStream, obj);   // Shell use: words beside the head
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

        startOpening();                  // step 1 begins: "communicating with llm service…"

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

        // Todo updates — dropped everywhere else (the agent owns its list), but in Shell
        // use the terminal is the full transcript, so the list streams there too.
        // payload is the JSON string service.py sends: {objective, tasks:[{text, done}]}.
        function setTodo(payload) {
            if (!header) return;
            var data;
            try { data = (typeof payload === 'string') ? JSON.parse(payload) : payload; }
            catch (e) { return; }
            if (!data) return;
            if (data.objective) outStream.push('todo: ' + data.objective);
            (data.tasks || []).forEach(function (t) {
                if (t && t.text) outStream.push((t.done ? '[x] ' : '[ ] ') + t.text);
            });
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
            // raw text straight onto the terminal, bypassing the JSON parser — used for
            // stage-level messages (e.g. a run that failed before it produced any output)
            note: function (t) { outStream.push(t); },
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
