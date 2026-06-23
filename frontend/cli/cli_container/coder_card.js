// CoderCard — a reusable live card component for ONE CLI/coder agent.
//
// Small + self-contained so the orchestrator (cli/cli_stage.js) creates one per agent and
// can stack several later. Look ported from coder_animation.html + the proven native banner
// (banner.py COMPACT_HTML). Styles: cli/cli_container/coder_card.css (scoped .coder-card).
//
// API: create(desc) -> { el, setLine, setTodos, addMinion, setMinionLine, endMinion,
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

    function makeLineStreamer(target, initial) {
        var queue = [];
        var running = false;
        var timer = null;

        // Render text into a single static page (no animation) — used for the initial label so
        // the row never flashes empty; the first streamed line replaces it.
        function renderStatic(text) {
            var page = document.createElement('div');
            page.className = 'cc-line';
            page.textContent = text;
            target.replaceChildren(page);
        }
        if (initial != null) renderStatic(String(initial));

        function pump() {
            if (running || !queue.length) return;
            running = true;
            var text = queue.shift();
            // Array.from splits by code point so emoji/surrogate pairs stay intact.
            var chars = Array.from(text);
            if (!chars.length) { running = false; pump(); return; }

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

    // The most label-worthy field of an action, for the icon's caption.
    function argOf(a) {
        return a.pattern || a.path || a.command || a.value || a.query || '';
    }
    function actionList(obj) {
        var actions = (obj && obj.action) || [];
        return Array.isArray(actions) ? actions : [actions];
    }

    function create(desc) {
        var el = document.createElement('div');
        el.className = 'coder-card';
        // The terminal card holds ONLY the streamed output + todo + minion output. Nothing else
        // (the action chain and scratchpad live in their own zones beside the card).
        el.innerHTML =
            '<div class="cc-body">' +
                '<div class="cc-out"><span class="cc-p">&gt;</span> <span class="cc-out-text"></span></div>' +
                '<div class="cc-minions"><span class="cc-trunk"></span></div>' +
            '</div>' +
            '<div class="cc-progress"><span class="cc-fill"></span></div>';

        var outEl = el.querySelector('.cc-out');
        var outText = el.querySelector('.cc-out-text');
        var minionsEl = el.querySelector('.cc-minions');
        var trunk = el.querySelector('.cc-trunk');

        // The `>` line shows the coder's LIVE output only (not the task/user request).
        var outStream = makeLineStreamer(outText, 'starting…');
        var minionStreams = {};   // minion id -> its line streamer

        // LEFT zone — "Tool response: N tools used" + the vertical action-icon chain (extreme
        // left of the stage, mirroring the bottom-left card). The orchestrator places it.
        var chainEl = document.createElement('div');
        chainEl.className = 'cc-actions';
        chainEl.innerHTML =
            '<div class="cc-tools-label">Tool response: <span class="cc-tools-count">0</span> tools used</div>' +
            '<div class="cc-chain-flow"><div class="cc-chain"></div></div>';
        var toolsCountEl = chainEl.querySelector('.cc-tools-count');
        var toolCount = 0;
        function bumpToolCount() { toolCount += 1; if (toolsCountEl) toolsCountEl.textContent = String(toolCount); }

        // RIGHT zone — "tracking progress" (the coder's scratchpad), mirroring the top-right
        // container: NO logo; each scratchpad entry streams in char-by-char as a dot-bullet line.
        var trackEl = document.createElement('div');
        trackEl.className = 'cc-track';
        trackEl.innerHTML =
            '<div class="cc-track-label">tracking progress</div>' +
            '<div class="cc-track-flow"><div class="cc-track-tree"></div></div>';

        var ICONS = window.CliToolIcons;
        var actionChain = ICONS ? ICONS.createChain(chainEl.querySelector('.cc-chain'), { orientation: 'vertical' }) : null;
        var tracker = ICONS ? ICONS.createTracker(trackEl.querySelector('.cc-track-tree'), trackEl.querySelector('.cc-track-flow'), trackEl) : null;
        var minionChains = {};         // minion id -> its own horizontal chain
        var minionParsers = {};        // minion id -> its action parser

        function pushActions(chain, obj, isCoder) {
            actionList(obj).forEach(function (a) {
                if (!a || typeof a !== 'object' || !a.type) return;
                // scratchpad -> the right "tracking progress" stream (coder only; not minions)
                if (a.type === 'scratchpad') { if (isCoder && tracker) tracker.push(argOf(a)); return; }
                // every other real tool -> the icon chain; count it for the coder's label
                if (chain && chain.addAction({ name: a.type, arg: argOf(a) }) && isCoder) bumpToolCount();
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

        var coderActionParser = makeActionParser(function (obj) {
            markReceived();                       // this JSON IS the packet -> tick "thinking"
            pushActions(actionChain, obj, true);  // play this step's tools (+ scratchpad/count)
            if (!hasExit(obj)) startOpening();    // anticipate the next step (skip on the final one)
        });

        function setLine(text) {
            outStream.push(text);        // animated `>` display
            coderActionParser(text);     // read `action` -> opening phases + tools + scratchpad
        }

        startOpening();                  // step 1 begins: "communicating with llm service…"

        // Todo is intentionally NOT shown in the terminal anymore — kept as a no-op so the
        // cliTaskTodo event stays harmless.
        function setTodos(payload) {}

        // Anchor the single trunk so it runs from the `>` line's center down to the LAST
        // minion's center (matches coder_animation.html / the native banner).
        function layoutTrunk() {
            var rows = minionsEl.querySelectorAll('.cc-mrow');
            if (!rows.length) { trunk.style.height = '0'; return; }
            var mRect = minionsEl.getBoundingClientRect();
            var oRect = outEl.getBoundingClientRect();
            // anchor to the minion's HEAD center (the row also holds a sub-chain below it)
            var lastHead = rows[rows.length - 1].querySelector('.cc-mrow-head') || rows[rows.length - 1];
            var lRect = lastHead.getBoundingClientRect();
            var topY = (oRect.top + oRect.height / 2) - mRect.top;
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
                '<div class="cc-mchain"></div>';
            minionsEl.appendChild(row);
            // Each minion streams its live output (initial = query) AND grows its own
            // horizontal action sub-chain, parsed from the same per-minion JSON.
            minionStreams[id] = makeLineStreamer(row.querySelector('.mline'), query || 'minion');
            if (ICONS) minionChains[id] = ICONS.createChain(row.querySelector('.cc-mchain'), { orientation: 'horizontal' });
            minionParsers[id] = makeActionParser(function (obj) { pushActions(minionChains[id], obj, false); });
            if (window.requestAnimationFrame) requestAnimationFrame(layoutTrunk); else layoutTrunk();
        }

        function setMinionLine(id, line) {
            if (minionStreams[id]) minionStreams[id].push(line);
            if (minionParsers[id]) minionParsers[id](line);   // read `action` -> this minion's chain
        }

        function endMinion(id, status) {
            var row = findRow(id);
            if (!row) return;
            var loader = row.querySelector('.cc-loader');
            var bad = (status === 'error' || status === 'stopped');
            if (loader) loader.outerHTML = '<span class="cc-mark ' + (bad ? 'err' : 'done') + '"></span>';
        }

        // Web has its own globe icon in the tool chain now — no "searching the web…" hint in
        // the terminal. Kept as a no-op so the existing pill_web_loading events stay harmless.
        function setWeb(on) {}

        function setDone(status, summary) {
            clearTimeout(openTimer);                                  // stop any pending "thinking"
            if (thinkingStep) { thinkingStep.complete('packet received'); thinkingStep = null; }
            if (summary) setLine(summary);
            el.classList.add((status === 'error' || status === 'stopped') ? 'error' : 'done');
        }

        function dispose() {
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
            setTodos: setTodos,
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
