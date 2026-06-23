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
    var STAGGER   = 8;     // ms between letters — constant, never length-based
    var CHAR_FADE = 60;    // ms opacity 0→1 fade per letter
    var PAGE_HOLD = 550;   // ms a full page lingers before clearing for the next page
    var LINE_HOLD = 260;   // ms between distinct lines

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
                if (i >= chars.length) {
                    // Final page rendered — hold, then pull the next queued line.
                    timer = setTimeout(function () { running = false; pump(); }, LINE_HOLD);
                    return;
                }
                var firstOnPage = page.childElementCount === 0;
                var span = document.createElement('span');
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

    function create(desc) {
        var el = document.createElement('div');
        el.className = 'coder-card';
        el.innerHTML =
            '<div class="cc-body">' +
                '<div class="cc-out"><span class="cc-p">&gt;</span> <span class="cc-out-text"></span></div>' +
                '<div class="cc-web">searching the web…</div>' +
                '<div class="cc-todos hidden"></div>' +
                '<div class="cc-minions"><span class="cc-trunk"></span></div>' +
            '</div>' +
            '<div class="cc-progress"><span class="cc-fill"></span></div>';

        var outEl = el.querySelector('.cc-out');
        var outText = el.querySelector('.cc-out-text');
        var todosEl = el.querySelector('.cc-todos');
        var minionsEl = el.querySelector('.cc-minions');
        var trunk = el.querySelector('.cc-trunk');

        // The `>` line shows the coder's LIVE output only (not the task/user request).
        var outStream = makeLineStreamer(outText, 'starting…');
        var minionStreams = {};   // minion id -> its line streamer

        function setLine(text) { outStream.push(text); }

        function setTodos(payload) {
            var data;
            try { data = (typeof payload === 'string') ? JSON.parse(payload) : payload; }
            catch (e) { return; }
            var tasks = (data && data.tasks) || [];
            if (!tasks.length) { todosEl.classList.add('hidden'); todosEl.innerHTML = ''; return; }
            todosEl.classList.remove('hidden');
            todosEl.innerHTML = '';
            var spinnerUsed = false;
            tasks.forEach(function (t) {
                var row = document.createElement('div');
                row.className = 'cc-item';
                var mark;
                if (t.done) {
                    mark = '<span class="cc-chk done"></span>';
                } else if (!spinnerUsed) {
                    mark = '<span class="cc-loading"></span>';
                    spinnerUsed = true;
                } else {
                    mark = '<span class="cc-chk"></span>';
                }
                row.innerHTML = mark + '<span class="lbl"></span>';
                row.querySelector('.lbl').textContent = t.text;
                todosEl.appendChild(row);
            });
        }

        // Anchor the single trunk so it runs from the `>` line's center down to the LAST
        // minion's center (matches coder_animation.html / the native banner).
        function layoutTrunk() {
            var rows = minionsEl.querySelectorAll('.cc-mrow');
            if (!rows.length) { trunk.style.height = '0'; return; }
            var mRect = minionsEl.getBoundingClientRect();
            var oRect = outEl.getBoundingClientRect();
            var lRect = rows[rows.length - 1].getBoundingClientRect();
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
            row.innerHTML = orbLoaderHTML() + '<span class="mline"></span>';
            minionsEl.appendChild(row);
            // Each minion streams its live output through its own paced streamer (initial = query).
            minionStreams[id] = makeLineStreamer(row.querySelector('.mline'), query || 'minion');
            if (window.requestAnimationFrame) requestAnimationFrame(layoutTrunk); else layoutTrunk();
        }

        function setMinionLine(id, line) {
            if (minionStreams[id]) minionStreams[id].push(line);
        }

        function endMinion(id, status) {
            var row = findRow(id);
            if (!row) return;
            var loader = row.querySelector('.cc-loader');
            var bad = (status === 'error' || status === 'stopped');
            if (loader) loader.outerHTML = '<span class="cc-mark ' + (bad ? 'err' : 'done') + '"></span>';
        }

        function setWeb(on) { el.classList.toggle('web-loading', !!on); }

        function setDone(status, summary) {
            if (summary) setLine(summary);
            el.classList.remove('web-loading');
            el.classList.add((status === 'error' || status === 'stopped') ? 'error' : 'done');
        }

        function dispose() {
            outStream.dispose();
            for (var k in minionStreams) if (minionStreams[k]) minionStreams[k].dispose();
        }

        return {
            el: el,
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
