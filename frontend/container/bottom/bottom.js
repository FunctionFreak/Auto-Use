// =====================================================================
// Tool-response flow — CONFIG-DRIVEN so the backend can map its events to
// steps later (future-proof). Edit STEPS (order / labels / logos) and the
// SHAPES registry; the DOM + animation are generated from them.
//
// Backend integration (when ready):
//     window.toolFlow.stopTest();              // turn the demo loop off
//     window.toolFlow.reset();                 // clear all steps
//     window.toolFlow.setActive('screenshot'); // a step starts (logo shows, works)
//     window.toolFlow.setDone('screenshot');   // a step finishes (-> next; ticks if step.tick)
//   ...map your real tool events to these by each step's `key`.
//
// Until then a built-in test runner plays the steps one after another (loop).
// =====================================================================
(function () {
    'use strict';

    /* ---------------- "N tools used" counter hooks ---------------- */
    window.setToolCount = function (n) {
        var el = document.getElementById('toolUsedCount');
        if (el) el.textContent = String(Math.max(0, n | 0));
    };
    window.bumpToolCount = function () {
        var el = document.getElementById('toolUsedCount');
        if (!el) return;
        el.textContent = String((parseInt(el.textContent, 10) || 0) + 1);
    };
    window.resetToolCount = function () {
        var el = document.getElementById('toolUsedCount');
        if (el) el.textContent = '0';
    };

    /* ---------------- icon geometry ---------------- */
    var ICON = 16;                              // icon size (css px); keep == CSS .tf-icon
    var COLOR = 'rgba(85, 90, 100, 0.92)';
    var VSCALE = (ICON / 2) / 44;              // source coords (~±42) -> icon
    var TYPE_MS = 12;                          // blazing-fast per-char typing
    var TICK_MS = 400;                         // shape -> tick crossfade duration

    /* ---------------- shape draws (each returns a painter(ctx,cx,cy,now,alpha)) ----
       To add a new logo: write a draw, add it to SHAPES, reference it from a STEP. */
    function rrect(ctx, x, y, w, h, r) { ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(x, y, w, h, r); else ctx.rect(x, y, w, h); }

    // vector shapes are authored in source coords (~±35) and drawn via vecPaint,
    // which centres + scales them into the icon.
    function vecScreen(ctx) {
        ctx.lineWidth = 5; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        rrect(ctx, -35, -25, 70, 45, 6); ctx.stroke();
        rrect(ctx, -20, 30, 40, 6, 3); ctx.fill();
        ctx.beginPath(); ctx.rect(-6, 20, 12, 10); ctx.fill();
    }
    var TREE_NODES = [{ x: 0, y: -25 }, { x: -20, y: 0 }, { x: 20, y: 0 }, { x: -35, y: 25 }, { x: -20, y: 25 }, { x: -5, y: 25 }, { x: 5, y: 25 }, { x: 20, y: 25 }, { x: 35, y: 25 }];
    var TREE_EDGES = [[0, 1], [0, 2], [1, 3], [1, 4], [1, 5], [2, 6], [2, 7], [2, 8]];
    function vecTree(ctx) {
        ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.beginPath();
        TREE_EDGES.forEach(function (e) { ctx.moveTo(TREE_NODES[e[0]].x, TREE_NODES[e[0]].y); ctx.lineTo(TREE_NODES[e[1]].x, TREE_NODES[e[1]].y); });
        ctx.stroke();
        TREE_NODES.forEach(function (nd) { ctx.beginPath(); ctx.arc(nd.x, nd.y, 4.5, 0, Math.PI * 2); ctx.fill(); });
    }
    function vecServer(ctx) {
        ctx.lineWidth = 4; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        rrect(ctx, -25, -35, 50, 70, 4); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(-25, -12); ctx.lineTo(25, -12); ctx.moveTo(-25, 12); ctx.lineTo(25, 12); ctx.stroke();
        [-23.5, 0, 23.5].forEach(function (yc) {
            ctx.beginPath(); ctx.arc(-15, yc, 2.5, 0, Math.PI * 2); ctx.fill();
            ctx.beginPath(); ctx.moveTo(-5, yc); ctx.lineTo(15, yc); ctx.stroke();
        });
    }
    function vecPaint(ctx, cx, cy, alpha, fn) {
        ctx.save();
        ctx.translate(cx, cy); ctx.scale(VSCALE, VSCALE);
        ctx.globalAlpha = alpha * 0.92; ctx.strokeStyle = COLOR; ctx.fillStyle = COLOR;
        fn(ctx); ctx.restore();
    }
    function vecPainter(fn) { return function (ctx, cx, cy, now, alpha) { vecPaint(ctx, cx, cy, alpha, fn); }; }

    // loader — 12 outer + 8 inner counter-rotating spokes (matches the source)
    var L_OUTER = 12, L_INNER = 8, L_PER = 14;
    function genLoader() {
        var d = [], i, j, t;
        for (i = 0; i < L_OUTER; i++) { var a = (i / L_OUTER) * Math.PI * 2; for (j = 0; j < L_PER; j++) { t = j / (L_PER - 1); d.push({ type: 'o', angle: a, r: 32 + t * 10 }); } }
        for (i = 0; i < L_INNER; i++) { var a2 = (i / L_INNER) * Math.PI * 2; for (j = 0; j < L_PER; j++) { t = j / (L_PER - 1); d.push({ type: 'i', angle: a2, r: 16 + t * 6 }); } }
        return d;
    }
    function loaderPainter() {
        var loader = genLoader();
        return function (ctx, cx, cy, now, alpha) {
            ctx.fillStyle = COLOR; ctx.globalAlpha = alpha * 0.9;
            for (var i = 0; i < loader.length; i++) {
                var l = loader[i];
                var a = l.angle + (l.type === 'o' ? now * 0.0015 : -now * 0.0025);
                ctx.beginPath();
                ctx.arc(cx + Math.cos(a) * l.r * VSCALE, cy + Math.sin(a) * l.r * VSCALE, 0.6, 0, Math.PI * 2);
                ctx.fill();
            }
        };
    }

    // globe — clean rotating wireframe (outline + parallels + meridians)
    function globePainter() {
        var R = ICON / 2 - 1.5, TILT = 0.34;
        return function (ctx, cx, cy, now, alpha) {
            ctx.strokeStyle = COLOR; ctx.globalAlpha = alpha * 0.9; ctx.lineWidth = 0.8;
            ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
            ctx.beginPath(); ctx.ellipse(cx, cy, R, R * TILT, 0, 0, Math.PI * 2); ctx.stroke();
            ctx.beginPath(); ctx.ellipse(cx, cy - R * 0.52, R * 0.85, R * 0.85 * TILT, 0, 0, Math.PI * 2); ctx.stroke();
            ctx.beginPath(); ctx.ellipse(cx, cy + R * 0.52, R * 0.85, R * 0.85 * TILT, 0, 0, Math.PI * 2); ctx.stroke();
            var spin = now * 0.0012;
            for (var k = 0; k < 3; k++) {
                var rx = Math.abs(Math.cos(spin + k * (Math.PI / 3))) * R;
                ctx.beginPath(); ctx.ellipse(cx, cy, rx, R, 0, 0, Math.PI * 2); ctx.stroke();
            }
        };
    }

    // "done" tick badge — filled circle + white check
    function drawTick(ctx, cx, cy, alpha) {
        var R = ICON / 2 - 1.5;
        ctx.save(); ctx.translate(cx, cy); ctx.globalAlpha = alpha;
        ctx.fillStyle = COLOR; ctx.beginPath(); ctx.arc(0, 0, R, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.95)';
        ctx.lineWidth = 1.6; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.beginPath(); ctx.moveTo(-R * 0.42, 0); ctx.lineTo(-R * 0.12, R * 0.32); ctx.lineTo(R * 0.46, -R * 0.32); ctx.stroke();
        ctx.restore();
    }

    // shape name -> painter factory (call to get a painter instance)
    var SHAPES = {
        screen: function () { return vecPainter(vecScreen); },
        tree:   function () { return vecPainter(vecTree); },
        server: function () { return vecPainter(vecServer); },
        loader: loaderPainter,
        globe:  globePainter,
    };

    /* =====================================================================
       STEP CONFIG — the single source of truth. Reorder / rename / re-shape
       here; everything else follows. Each step:
         key       backend id you'll map your tool event to
         shape     which logo (a key in SHAPES above)
         label     text shown while the step runs
         doneLabel (optional) text shown once the step is done
         tick      (optional) morph the logo into a ✓ when done
       ===================================================================== */
    var STEPS = [
        { key: 'screenshot',  shape: 'screen', label: 'screenshot taken' },
        { key: 'mapping',     shape: 'tree',   label: 'mapping pixel and element' },
        { key: 'communicate', shape: 'server', label: 'communicating to llm service' },
        { key: 'thinking',    shape: 'loader', label: 'thinking', doneLabel: 'packet received', tick: true },
        { key: 'web',         shape: 'globe',  label: 'searching the web' },
    ];

    /* ---------------- per-icon canvas + render ---------------- */
    function setupCanvas(canvas) {
        var ctx = canvas.getContext('2d');
        var dpr = window.devicePixelRatio || 1;
        canvas.width = ICON * dpr; canvas.height = ICON * dpr;
        canvas.style.width = ICON + 'px'; canvas.style.height = ICON + 'px';
        ctx.scale(dpr, dpr);
        return ctx;
    }
    var rafId = null;
    function frame(now) {
        rafId = null;
        for (var i = 0; i < STEPS.length; i++) renderIcon(STEPS[i], now);
        rafId = requestAnimationFrame(frame);
    }
    function startEngine() { if (!rafId) rafId = requestAnimationFrame(frame); }

    function renderIcon(step, now) {
        var ctx = step._ctx;
        if (!ctx) return;
        var cx = ICON / 2, cy = ICON / 2;
        ctx.clearRect(0, 0, ICON, ICON);
        if (step.state === 'pending') return;                 // nothing drawn (also hidden via CSS)
        if (step.tick && step.state === 'done') {
            var k = Math.min(1, (now - step._doneAt) / TICK_MS);
            if (k < 1) { step._painter(ctx, cx, cy, now, 1 - k); drawTick(ctx, cx, cy, k); }
            else drawTick(ctx, cx, cy, 1);
        } else {
            step._painter(ctx, cx, cy, now, 1);               // active, or done-without-tick
        }
        ctx.globalAlpha = 1;
    }

    /* ---------------- blazing-fast typewriter ---------------- */
    function typeText(el, text) {
        if (!el) return;
        clearTimeout(el._typeTimer);
        el.textContent = '';
        var i = 0;
        (function tick() {
            if (i > text.length) return;
            el.textContent = text.slice(0, i);
            i += 1;
            el._typeTimer = setTimeout(tick, TYPE_MS);
        })();
    }

    /* ---------------- orchestration (state machine) ---------------- */
    function activate(step) {
        if (!step || !step._item) return;
        step.state = 'active';
        step._doneAt = 0;
        step._item.classList.add('line-in');                  // CSS draws the line segment
        clearTimeout(step._revealTimer);
        step._revealTimer = setTimeout(function () {
            step._item.classList.add('icon-in');              // reveal the logo
            typeText(step._word, step.label);
        }, 320);                                              // logo/text appear as the line lands
    }
    function complete(step) {
        if (!step || !step._item) return;
        step.state = 'done';
        step._doneAt = performance.now();                     // drives the tick crossfade
        // Make sure the step is visible even if setDone() is called without a
        // prior setActive() (direct backend driving).
        clearTimeout(step._revealTimer);
        step._item.classList.add('line-in', 'icon-in');
        if (step.doneLabel) {
            clearTimeout(step._doneTimer);
            step._doneTimer = setTimeout(function () { typeText(step._word, step.doneLabel); }, TICK_MS * 0.6);
        } else if (step._word && !step._word.textContent) {
            typeText(step._word, step.label);                 // no label yet -> show it now
        }
        window.bumpToolCount();
    }
    function resetFlow() {
        for (var i = 0; i < STEPS.length; i++) {
            var s = STEPS[i];
            s.state = 'pending'; s._doneAt = 0;
            clearTimeout(s._revealTimer); clearTimeout(s._doneTimer);
            if (s._word) { clearTimeout(s._word._typeTimer); s._word.textContent = ''; }
            if (s._item) s._item.classList.remove('line-in', 'icon-in');
        }
        window.resetToolCount();
    }
    function findStep(key) {
        for (var i = 0; i < STEPS.length; i++) if (STEPS[i].key === key) return STEPS[i];
        return null;
    }

    /* ---------------- test runner (auto sequential loop) ---------------- */
    var WORK_MS = 1500, GAP_MS = 700, HOLD_MS = 2200, FADE_MS = 450;
    var testOn = true, testTimer = null, flowEl = null;

    function runTest() {
        if (!testOn) return;
        resetFlow();
        if (flowEl) flowEl.style.opacity = '1';
        var i = 0;
        (function nextStep() {
            if (!testOn) return;
            if (i >= STEPS.length) {                          // all done -> hold, fade, loop
                testTimer = setTimeout(function () {
                    if (flowEl) flowEl.style.opacity = '0';
                    testTimer = setTimeout(runTest, FADE_MS);
                }, HOLD_MS);
                return;
            }
            var step = STEPS[i];
            activate(step);
            testTimer = setTimeout(function () {
                complete(step);
                i += 1;
                testTimer = setTimeout(nextStep, GAP_MS);
            }, WORK_MS);
        })();
    }

    /* ---------------- backend hooks (map your tool events to these) ---------------- */
    window.toolFlow = {
        steps: STEPS,
        setActive: function (key) { activate(findStep(key)); },
        setDone: function (key) { complete(findStep(key)); },
        reset: function () { resetFlow(); },
        stopTest: function () {
            testOn = false;
            clearTimeout(testTimer);
            // also kill any per-step timers in flight, so a stale reveal/type
            // can't fire after the backend takes over.
            for (var i = 0; i < STEPS.length; i++) {
                var s = STEPS[i];
                clearTimeout(s._revealTimer);
                clearTimeout(s._doneTimer);
                if (s._word) clearTimeout(s._word._typeTimer);
            }
        },
        startTest: function () { if (!testOn) { testOn = true; runTest(); } },
    };

    /* ---------------- build + mount ---------------- */
    function buildFlow(tree) {
        for (var i = 0; i < STEPS.length; i++) {
            var step = STEPS[i];
            var item = document.createElement('div');
            item.className = 'tool-flow-item';
            var canvas = document.createElement('canvas');
            canvas.className = 'tf-icon';
            var word = document.createElement('span');
            word.className = 'tool-flow-word';
            item.appendChild(canvas);
            item.appendChild(word);
            tree.appendChild(item);
            step._item = item;
            step._word = word;
            step._ctx = setupCanvas(canvas);
            step._painter = (SHAPES[step.shape] || SHAPES.loader)();
            step.state = 'pending';
        }
        startEngine();
    }

    function mount() {
        var grid = document.getElementById('mainGrid');
        if (!grid || grid.querySelector('.bottom-zone')) return;
        fetch('container/bottom/bottom.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (grid.querySelector('.bottom-zone')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var zone = holder.querySelector('.bottom-zone');
                if (!zone) return;
                grid.appendChild(zone);
                flowEl = zone.querySelector('.tool-flow');
                var tree = zone.querySelector('.tool-flow-tree');
                if (tree) { buildFlow(tree); runTest(); }
            })
            .catch(function () { /* non-fatal */ });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }
})();
