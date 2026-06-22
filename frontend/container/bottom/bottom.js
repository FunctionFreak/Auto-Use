// =====================================================================
// Tool-response flow — EVENT-DRIVEN. The chain is built dynamically as the
// agent runs; each logo is a painter in SHAPES, each step maps a backend
// tool/phase to {shape, label} (see OPEN + TOOL_MAP).
//
// Backend drives it from app.py via pywebview — one event per turn from the main driver:
//     window.toolFlow.onFlow('run_start')
//     window.toolFlow.onFlow('turn', '{"hasImage":true}')   // screenshot+mapping+communicate+thinking
//     window.toolFlow.onFlow('received', '{"tools":[{"name":"left_click","clicks":2},{"name":"input"}]}')
//     window.toolFlow.onFlow('run_end')
// `received` ticks "packet received" then plays this turn's tools (read from the action
// block). Image-less turns (web / sub-agent) pass hasImage:false to skip screenshot+mapping.
//
// The chain stays EMPTY on app start and only animates while an agent run is driving it.
// A built-in demo exists but is opt-in only (window.toolFlow.startTest()).
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

    // AI mascot head — rounded square + blinking white eyes (ported from
    // tool_animation.html shape 'a'). Two-tone + animated, so it needs its own
    // painter rather than the single-colour vecPainter wrapper.
    function agentPainter() {
        return function (ctx, cx, cy, now, alpha) {
            ctx.save();
            ctx.translate(cx, cy); ctx.scale(VSCALE, VSCALE);
            // head
            ctx.globalAlpha = alpha * 0.92; ctx.fillStyle = COLOR;
            ctx.beginPath();
            if (ctx.roundRect) ctx.roundRect(-30, -30, 60, 60, 13); else ctx.rect(-30, -30, 60, 60);
            ctx.fill();
            // blink (1.8s loop) — eyes squash near the end of each cycle
            var bt = now % 1800, sy = 1;
            if (bt > 1620) sy = bt < 1710 ? 1 - ((bt - 1620) / 90) * 0.92 : 0.08 + ((bt - 1710) / 90) * 0.92;
            var eh = 14 * sy, ey = -5 - eh / 2, er = Math.min(4, eh / 2);
            ctx.globalAlpha = alpha; ctx.fillStyle = '#ffffff';
            ctx.beginPath();
            if (ctx.roundRect) { ctx.roundRect(-12, ey, 8, eh, er); ctx.roundRect(4, ey, 8, eh, er); }
            else { ctx.rect(-12, ey, 8, eh); ctx.rect(4, ey, 8, eh); }
            ctx.fill();
            ctx.restore();
        };
    }

    // Apple terminal — window + ">" prompt + Apple logo (shape 'sh'). Build the
    // glyph path once; single-colour so it rides the vecPainter wrapper.
    var APPLE_PATH = (typeof Path2D !== 'undefined')
        ? new Path2D("M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701z")
        : null;
    function vecApple(ctx) {
        ctx.lineWidth = 3.5; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(-35, -25, 70, 50, 4); else ctx.rect(-35, -25, 70, 50);
        ctx.stroke();
        ctx.beginPath(); ctx.moveTo(-35, -15); ctx.lineTo(35, -15); ctx.stroke();   // top bar
        ctx.lineWidth = 3;
        ctx.beginPath(); ctx.moveTo(-26, -9); ctx.lineTo(-21, -5); ctx.lineTo(-26, -1); ctx.stroke(); // ">"
        if (APPLE_PATH) { ctx.save(); ctx.translate(0, 4); ctx.scale(1.3, 1.3); ctx.translate(-12, -12); ctx.fill(APPLE_PATH); ctx.restore(); }
    }

    // Terminal shell — window + ">" prompt + three output lines (shape 'cl').
    function vecShell(ctx) {
        ctx.lineWidth = 3.5; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(-35, -25, 70, 50, 4); else ctx.rect(-35, -25, 70, 50);
        ctx.stroke();
        ctx.beginPath(); ctx.moveTo(-35, -15); ctx.lineTo(35, -15); ctx.stroke();   // top bar
        ctx.lineWidth = 3;
        ctx.beginPath(); ctx.moveTo(-26, -9); ctx.lineTo(-21, -5); ctx.lineTo(-26, -1); ctx.stroke(); // ">"
        [[-16, 14, -5], [-26, 20, 5], [-26, 4, 15]].forEach(function (L) {
            ctx.beginPath(); ctx.moveTo(L[0], L[2]); ctx.lineTo(L[1], L[2]); ctx.stroke();
        });
    }

    // Clock — face ring + 12 ticks + hands at 10:10 + hub (shape 'ck'). Static.
    var _hourA = (10 + 10 / 60) * 30 * Math.PI / 180, _minA = 10 * 6 * Math.PI / 180;
    var CLOCK_HOUR_END = { x: Math.sin(_hourA) * 16, y: -Math.cos(_hourA) * 16 };
    var CLOCK_MIN_END  = { x: Math.sin(_minA) * 25,  y: -Math.cos(_minA) * 25 };
    function vecClock(ctx) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.lineWidth = 3.5; ctx.beginPath(); ctx.arc(0, 0, 32, 0, Math.PI * 2); ctx.stroke();
        ctx.lineWidth = 2.5;
        for (var i = 0; i < 12; i++) {
            var a = i * 30 * Math.PI / 180, dx = Math.sin(a), dy = -Math.cos(a);
            ctx.beginPath(); ctx.moveTo(dx * 28, dy * 28); ctx.lineTo(dx * 31.5, dy * 31.5); ctx.stroke();
        }
        ctx.lineWidth = 4; ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(CLOCK_HOUR_END.x, CLOCK_HOUR_END.y); ctx.stroke();
        ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(CLOCK_MIN_END.x, CLOCK_MIN_END.y); ctx.stroke();
        ctx.beginPath(); ctx.arc(0, 0, 2.6, 0, Math.PI * 2); ctx.fill();
    }

    // Keyboard — body + two rows of keys + spacebar (shape 'kb'). Static.
    var KEY_XS = [-27.5, -16.5, -5.5, 5.5, 16.5, 27.5], KEY_YS = [-11, -1];
    function vecKeyboard(ctx) {
        ctx.lineWidth = 3.5; ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(-38, -20, 76, 40, 5); else ctx.rect(-38, -20, 76, 40);
        ctx.stroke();
        KEY_YS.forEach(function (ky) {
            KEY_XS.forEach(function (kx) {
                ctx.beginPath();
                if (ctx.roundRect) ctx.roundRect(kx - 3.5, ky - 3.5, 7, 7, 1.5); else ctx.rect(kx - 3.5, ky - 3.5, 7, 7);
                ctx.fill();
            });
        });
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(-18, 7, 36, 6, 2); else ctx.rect(-18, 7, 36, 6);
        ctx.fill();
    }

    // Mouse with scrolling wheel — body + wheel pill + button split + animated
    // scroll tick (shape 'mo'). The tick oscillates so it reads as "scrolling".
    var MOUSE_SCROLL_CENTER = -15.5;
    function mousePainter() {
        return function (ctx, cx, cy, now, alpha) {
            var scrollY = MOUSE_SCROLL_CENTER + 3.0 * Math.sin(now / 200);
            ctx.save();
            ctx.translate(cx, cy); ctx.scale(VSCALE, VSCALE);
            ctx.globalAlpha = alpha * 0.92; ctx.strokeStyle = COLOR; ctx.fillStyle = COLOR;
            ctx.lineCap = 'round'; ctx.lineJoin = 'round';
            ctx.lineWidth = 3.5; ctx.beginPath();
            if (ctx.roundRect) ctx.roundRect(-15, -26, 30, 48, [15, 15, 12, 12]); else ctx.rect(-15, -26, 30, 48);
            ctx.stroke();
            ctx.lineWidth = 2.5; ctx.beginPath();
            if (ctx.roundRect) ctx.roundRect(-3, -20, 6, 9, 3); else ctx.rect(-3, -20, 6, 9);
            ctx.stroke();
            ctx.beginPath(); ctx.moveTo(0, -9); ctx.lineTo(0, 4); ctx.stroke();
            ctx.lineWidth = 2.2; ctx.beginPath(); ctx.moveTo(-1.8, scrollY); ctx.lineTo(1.8, scrollY); ctx.stroke();
            ctx.restore();
        };
    }

    // Cursor (left-pointing arrow) with a "breathing" click pulse — 1/2/3 presses
    // per 2s loop (shapes 'cu*'). Animated + pulses about its own pivot.
    var CURSOR_PTS = [[-11, -27], [-11, 16], [2, 6], [20, 4]], CURSOR_PIVOT = { x: -1, y: -4 };
    function clickScale(now, clicks) {
        var local = now % 2000, dip;
        if (clicks === 3) dip = Math.min(1, Math.exp(-Math.pow((local - 700) / 80, 2)) + Math.exp(-Math.pow((local - 960) / 80, 2)) + Math.exp(-Math.pow((local - 1220) / 80, 2)));
        else if (clicks === 2) dip = Math.min(1, Math.exp(-Math.pow((local - 850) / 85, 2)) + Math.exp(-Math.pow((local - 1150) / 85, 2)));
        else dip = Math.exp(-Math.pow((local - 1000) / 160, 2));
        return 1.0 - 0.15 * dip;
    }
    function cursorPainter(clicks, flip) {
        return function (ctx, cx, cy, now, alpha) {
            var s = clickScale(now, clicks);
            ctx.save();
            ctx.translate(cx, cy); ctx.scale(VSCALE, VSCALE);
            if (flip) ctx.scale(-1, 1);                       // mirror -> points up-right (right click)
            ctx.globalAlpha = alpha * 0.92; ctx.fillStyle = COLOR; ctx.strokeStyle = COLOR;
            ctx.translate(CURSOR_PIVOT.x, CURSOR_PIVOT.y); ctx.scale(s, s); ctx.translate(-CURSOR_PIVOT.x, -CURSOR_PIVOT.y);
            ctx.lineWidth = 6; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
            ctx.beginPath(); ctx.moveTo(CURSOR_PTS[0][0], CURSOR_PTS[0][1]);
            for (var i = 1; i < CURSOR_PTS.length; i++) ctx.lineTo(CURSOR_PTS[i][0], CURSOR_PTS[i][1]);
            ctx.closePath(); ctx.fill(); ctx.stroke();
            ctx.restore();
        };
    }

    // Pen taking notes — tilted pen + nib + three note lines; the top line's left
    // end sweeps in/out so it reads as "writing" (shape 'pn'). Animated.
    var PEN_LINE_REST = -26;
    function drawPenShape(ctx, lineLeftX) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.lineWidth = 6.5; ctx.beginPath(); ctx.moveTo(22, -20); ctx.lineTo(0, 2); ctx.stroke();
        ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(0, 2); ctx.lineTo(-5, 7); ctx.stroke();
        ctx.lineWidth = 3;
        ctx.beginPath(); ctx.moveTo(lineLeftX, 10); ctx.lineTo(-7, 10); ctx.stroke();   // writing line
        ctx.beginPath(); ctx.moveTo(-27, 17); ctx.lineTo(3, 17); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(-27, 24); ctx.lineTo(-11, 24); ctx.stroke();
    }
    function penPainter() {
        return function (ctx, cx, cy, now, alpha) {
            ctx.save();
            ctx.translate(cx, cy); ctx.scale(VSCALE, VSCALE);
            ctx.globalAlpha = alpha * 0.92; ctx.strokeStyle = COLOR; ctx.fillStyle = COLOR;
            drawPenShape(ctx, PEN_LINE_REST + 8 * Math.sin(now / 230));
            ctx.restore();
        };
    }

    // Camera — body + viewfinder bump + lens (two rings) + flash dot (shape 'cm'). Static.
    function vecCamera(ctx) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.lineWidth = 3.5;
        ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(-35, -16, 70, 38, 6); else ctx.rect(-35, -16, 70, 38); ctx.stroke();
        ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(-11, -23, 22, 8, 3); else ctx.rect(-11, -23, 22, 8); ctx.stroke();
        ctx.beginPath(); ctx.arc(0, 3, 13, 0, Math.PI * 2); ctx.stroke();
        ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(0, 3, 7.5, 0, Math.PI * 2); ctx.stroke();
        ctx.beginPath(); ctx.arc(23, -9, 2.5, 0, Math.PI * 2); ctx.fill();
    }

    // Drag-and-drop — dashed drop-zone + a box that glides into it (shape 'dd'). Animated.
    var DRAG_START = { x: -18, y: -14 }, DRAG_TARGET = { x: 17, y: 12 };
    function drawDragDropShape(ctx, bx, by) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.lineWidth = 2.5; ctx.setLineDash([4, 4]);
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(2, -2, 30, 28, 4); else ctx.rect(2, -2, 30, 28);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.lineWidth = 3; ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(bx - 9, by - 9, 18, 18, 3); else ctx.rect(bx - 9, by - 9, 18, 18);
        ctx.stroke();
    }
    function dragDropPainter() {
        return function (ctx, cx, cy, now, alpha) {
            var theta = (now % 2400) / 2400 * Math.PI * 2, e = (1 - Math.cos(theta)) / 2;
            var bx = DRAG_START.x + (DRAG_TARGET.x - DRAG_START.x) * e;
            var by = DRAG_START.y + (DRAG_TARGET.y - DRAG_START.y) * e;
            ctx.save();
            ctx.translate(cx, cy); ctx.scale(VSCALE, VSCALE);
            ctx.globalAlpha = alpha * 0.92; ctx.strokeStyle = COLOR; ctx.fillStyle = COLOR;
            drawDragDropShape(ctx, bx, by);
            ctx.restore();
        };
    }

    // Todo list — 3 rows (checkbox + task line). alphas[i]>0 draws a tick + strike on
    // row i. 'todo' shows it empty (created); 'todoUpd' ticks the rows off (updated).
    var TODO_ROWS = [-16, 0, 16], TODO_ENDS = [22, 26, 14];
    function drawTodoShape(ctx, alphas) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        for (var i = 0; i < TODO_ROWS.length; i++) {
            var ry = TODO_ROWS[i];
            ctx.lineWidth = 2.5; ctx.beginPath();
            if (ctx.roundRect) ctx.roundRect(-31, ry - 4.5, 9, 9, 2); else ctx.rect(-31, ry - 4.5, 9, 9);
            ctx.stroke();
            ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(-18, ry); ctx.lineTo(TODO_ENDS[i], ry); ctx.stroke();
        }
        for (var j = 0; j < TODO_ROWS.length; j++) {
            var a = alphas[j];
            if (a > 0.01) {
                var ry2 = TODO_ROWS[j];
                ctx.save(); ctx.globalAlpha *= a;
                ctx.lineWidth = 2.2; ctx.beginPath(); ctx.moveTo(-29.5, ry2); ctx.lineTo(-27, ry2 + 2.5); ctx.lineTo(-23.5, ry2 - 3); ctx.stroke();
                ctx.lineWidth = 2.5; ctx.beginPath(); ctx.moveTo(-18, ry2); ctx.lineTo(TODO_ENDS[j], ry2); ctx.stroke();
                ctx.restore();
            }
        }
    }
    function vecTodo(ctx) { drawTodoShape(ctx, [0, 0, 0]); }
    function todoUpdateAlphas(now) {
        var t = now % 1800;
        var tickIn = function (s) { if (t < s) return 0; if (t < s + 130) return (t - s) / 130; return 1; };
        var fade = 1;
        if (t > 1300 && t < 1600) fade = 1 - (t - 1300) / 300; else if (t >= 1600) fade = 0;
        return [tickIn(0) * fade, tickIn(120) * fade, tickIn(240) * fade];
    }
    function todoUpdatePainter() {
        return function (ctx, cx, cy, now, alpha) {
            ctx.save();
            ctx.translate(cx, cy); ctx.scale(VSCALE, VSCALE);
            ctx.globalAlpha = alpha * 0.92; ctx.strokeStyle = COLOR; ctx.fillStyle = COLOR;
            drawTodoShape(ctx, todoUpdateAlphas(now));
            ctx.restore();
        };
    }

    // Open book (scratchpad) in perspective — both pages + spine + text, with a right
    // leaf that lifts and turns around the spine (shape 'bk'). Animated.
    var BOOK_LINES = [[5, -11, 19, -10], [5, -4, 26, -3], [6, 3, 31, 4], [6, 10, 35, 11]];
    function drawBookPage(ctx, m) {
        ctx.beginPath();
        ctx.moveTo(0, -19);
        ctx.quadraticCurveTo(m * 10, -22, m * 20, -15);
        ctx.lineTo(m * 38, 16);
        ctx.quadraticCurveTo(m * 18, 21, 0, 19);
        ctx.stroke();
    }
    function drawBookShape(ctx) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.lineWidth = 3;
        drawBookPage(ctx, -1); drawBookPage(ctx, 1);
        ctx.lineWidth = 2.5; ctx.beginPath(); ctx.moveTo(0, -19); ctx.lineTo(0, 19); ctx.stroke();
        ctx.lineWidth = 2;
        BOOK_LINES.forEach(function (L) { ctx.beginPath(); ctx.moveTo(L[0], L[1]); ctx.lineTo(L[2], L[3]); ctx.stroke(); });
        BOOK_LINES.forEach(function (L) { ctx.beginPath(); ctx.moveTo(-L[0], L[1]); ctx.lineTo(-L[2], L[3]); ctx.stroke(); });
    }
    function drawBookLeaf(ctx) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.lineWidth = 3;
        drawBookPage(ctx, 1);
        ctx.lineWidth = 2;
        BOOK_LINES.forEach(function (L) { ctx.beginPath(); ctx.moveTo(L[0], L[1]); ctx.lineTo(L[2], L[3]); ctx.stroke(); });
    }
    function bookPainter() {
        return function (ctx, cx, cy, now, alpha) {
            ctx.save();
            ctx.translate(cx, cy); ctx.scale(VSCALE, VSCALE);
            ctx.globalAlpha = alpha * 0.92; ctx.strokeStyle = COLOR; ctx.fillStyle = COLOR;
            drawBookShape(ctx);
            var ang = (now % 1500) / 1500 * Math.PI;       // one page turn per 1.5s
            if (ang > 0.001) {
                ctx.save();
                ctx.transform(Math.cos(ang), -Math.sin(ang) * 0.75, 0, 1, 0, 0);  // foreshorten + lift
                drawBookLeaf(ctx);
                ctx.restore();
            }
            ctx.restore();
        };
    }

    // Open app box — isometric crate with splayed lid flaps (shape 'ap'). Static.
    // (the file's rising "apps" word is dropped — illegible at 16px.)
    function vecAppBox(ctx) {
        ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.moveTo(0, -14); ctx.lineTo(24, -6); ctx.lineTo(0, 2); ctx.lineTo(-24, -6); ctx.closePath(); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(-24, -6); ctx.lineTo(-24, 14); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(0, 2); ctx.lineTo(0, 22); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(24, -6); ctx.lineTo(24, 14); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(-24, 14); ctx.lineTo(0, 22); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(24, 14); ctx.lineTo(0, 22); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(0, -14); ctx.lineTo(-24, -6); ctx.lineTo(-39, -16); ctx.lineTo(-15, -24); ctx.closePath(); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(0, -14); ctx.lineTo(24, -6); ctx.lineTo(39, -16); ctx.lineTo(15, -24); ctx.closePath(); ctx.stroke();
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

    // Warning triangle with a "!" — shown when a run is interrupted or fails
    // (soft red so it stands out against the grey chain).
    function drawBang(ctx, cx, cy, alpha) {
        var R = ICON / 2 - 1.5;
        ctx.save(); ctx.translate(cx + 0.5, cy); ctx.globalAlpha = alpha;  // +0.5: sit the tip on the connector line
        var red = 'rgba(198, 92, 80, 0.95)';
        ctx.fillStyle = red; ctx.strokeStyle = red;
        ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.lineWidth = 2.2;
        // rounded triangle pointing up (round-join stroke softens the corners)
        ctx.beginPath();
        ctx.moveTo(0, -R);
        ctx.lineTo(R * 0.96, R * 0.72);
        ctx.lineTo(-R * 0.96, R * 0.72);
        ctx.closePath();
        ctx.fill(); ctx.stroke();
        // white "!" tucked into the triangle's lower body
        ctx.fillStyle = 'rgba(255, 255, 255, 0.97)';
        var ey = 0.6;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(-0.85, ey - R * 0.42, 1.7, R * 0.55, 0.85); else ctx.rect(-0.85, ey - R * 0.42, 1.7, R * 0.55);
        ctx.fill();
        ctx.beginPath(); ctx.arc(0, ey + R * 0.42, 1.05, 0, Math.PI * 2); ctx.fill();
        ctx.restore();
    }
    function bangPainter() { return function (ctx, cx, cy, now, alpha) { drawBang(ctx, cx, cy, alpha); }; }

    // shape name -> painter factory (call to get a painter instance)
    var SHAPES = {
        screen: function () { return vecPainter(vecScreen); },
        tree:   function () { return vecPainter(vecTree); },
        server: function () { return vecPainter(vecServer); },
        loader: loaderPainter,
        globe:  globePainter,
        agent:  agentPainter,                       // mascot head (blinking)
        apple:  function () { return vecPainter(vecApple); },
        shell:  function () { return vecPainter(vecShell); },
        clock:    function () { return vecPainter(vecClock); },
        keyboard: function () { return vecPainter(vecKeyboard); },
        mouse:    mousePainter,                      // mouse w/ scrolling wheel
        click1:   function () { return cursorPainter(1); },        // single left click
        click2:   function () { return cursorPainter(2); },        // double click
        click3:   function () { return cursorPainter(3); },        // triple click
        clickR:   function () { return cursorPainter(1, true); },  // right click (points right)
        pen:      penPainter,                       // pen taking notes (writing)
        camera:   function () { return vecPainter(vecCamera); },
        todo:     function () { return vecPainter(vecTodo); },  // empty todo list
        todoUpd:  todoUpdatePainter,                // todo ticking off
        dragdrop: dragDropPainter,                  // box glides into drop-zone
        book:     bookPainter,                      // open book / scratchpad (page turns)
        appbox:   function () { return vecPainter(vecAppBox); },  // open app crate
        error:    bangPainter,                      // "!" badge — interrupted / failed
    };

    /* =====================================================================
       CONFIG. STEPS is the LIVE chain, built dynamically as events arrive
       (idle demo, or real agent events via window.toolFlow.onFlow). Each step:
         shape     which logo (a key in SHAPES above)
         label     text shown while the step runs
         doneLabel (optional) text shown once the step is done
         tick      (optional) morph the logo into a ✓ when done
         phase     (optional) opening phase, not counted in "N tools used"
       ===================================================================== */
    var STEPS = [];                                  // the live on-screen chain

    // Idle demo — plays + loops when no agent run is active.
    var DEMO = [
        { shape: 'screen',  label: 'screenshot taken', phase: true },
        { shape: 'tree',    label: 'mapping pixel and element', phase: true },
        { shape: 'server',  label: 'communicating with llm service', phase: true },
        { shape: 'loader',  label: 'thinking', doneLabel: 'packet received', tick: true, phase: true },
        { shape: 'click1',  label: 'select' },
        { shape: 'pen',     label: 'typing text' },
        { shape: 'click2',  label: 'double left click' },
        { shape: 'mouse',   label: 'scrolling the screen' },
        { shape: 'keyboard',label: 'pressing hotkeys' },
        { shape: 'globe',   label: 'searching the web' },
        { shape: 'agent',   label: 'sub agent trigger' },
        { shape: 'apple',   label: 'executing applescript' },
        { shape: 'shell',   label: 'executing shell' },
        { shape: 'appbox',  label: 'opening app' },
        { shape: 'camera',  label: 'taking a snapshot' },
        { shape: 'todo',    label: 'created todo' },
        { shape: 'todoUpd', label: 'updated todo' },
        { shape: 'dragdrop',label: 'dragging and dropping files' },
        { shape: 'book',    label: 'saving to agent memory' },
    ];

    // Per-turn opening phases (real run).
    var OPEN = {
        screenshot:  { shape: 'screen', label: 'screenshot taken', phase: true },
        mapping:     { shape: 'tree',   label: 'mapping pixel and element', phase: true },
        communicate: { shape: 'server', label: 'communicating with llm service', phase: true },
        thinking:    { shape: 'loader', label: 'thinking', doneLabel: 'packet received', tick: true, phase: true },
    };

    // Backend action/tool name -> { shape, label }. left_click is split by click count.
    var TOOL_MAP = {
        left_click_1: { shape: 'click1',   label: 'select' },
        left_click_2: { shape: 'click2',   label: 'double left click' },
        left_click_3: { shape: 'click3',   label: 'triple left click' },
        right_click:  { shape: 'clickR',   label: 'right click' },
        input:        { shape: 'pen',      label: 'typing text' },
        typewrite:    { shape: 'pen',      label: 'typing text' },
        hotkey:       { shape: 'keyboard', label: 'pressing hotkeys' },
        scroll:       { shape: 'mouse',    label: 'scrolling the screen' },
        drag_drop:    { shape: 'dragdrop', label: 'dragging and dropping files' },
        screenshot:   { shape: 'camera',   label: 'taking a snapshot' },
        open_app:     { shape: 'appbox',   label: 'opening app' },
        wait:         { shape: 'clock',    label: 'waiting for a while' },
        web:          { shape: 'globe',    label: 'searching the web' },
        cli_agent:    { shape: 'agent',    label: 'sub agent trigger' },
        minion:       { shape: 'agent',    label: 'sub agent trigger' },
        cli_await:    { shape: 'clock',    label: 'waiting for sub-agents' },
        shell:        { shape: 'shell',    label: 'executing shell' },
        applescript:  { shape: 'apple',    label: 'executing applescript' },
        todo_list:    { shape: 'todo',     label: 'created todo' },
        update_todo:  { shape: 'todoUpd',  label: 'updated todo' },
        scratchpad:   { shape: 'book',     label: 'saving to agent memory' },
        done:         { shape: 'loader',   label: 'done', tick: true },  // reuse the packet tick
    };

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
        // Only the visible tail is re-rendered each frame, so long runs stay cheap
        // (off-screen items are masked out and don't need repainting).
        var start = STEPS.length > RENDER_TAIL ? STEPS.length - RENDER_TAIL : 0;
        for (var i = start; i < STEPS.length; i++) renderIcon(STEPS[i], now);
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
        frontier = STEPS.indexOf(step);
        scrollToFrontier(frontier);                           // slide the chain up if it overflows
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
        // prior setActive() (direct backend driving) — also scroll to it.
        clearTimeout(step._revealTimer);
        frontier = Math.max(frontier, STEPS.indexOf(step));
        scrollToFrontier(frontier);
        step._item.classList.add('line-in', 'icon-in');
        if (step.doneLabel) {
            clearTimeout(step._doneTimer);
            step._doneTimer = setTimeout(function () { typeText(step._word, step.doneLabel); }, TICK_MS * 0.6);
        } else if (step._word && !step._word.textContent) {
            typeText(step._word, step.label);                 // no label yet -> show it now
        }
        // NB: the "N tools used" counter is bumped by the driver (per real tool),
        // not here — opening phases / thinking complete without counting.
    }

    /* ---------------- conveyor scroll ----------------
       The chain grows downward as steps activate. Once it fills the visible box,
       each new step slides the whole tree up one "pitch" (logo + gap) so the top
       logo rises past the top edge and dissolves under the mask. maxVisible is
       MEASURED from the box height, so a taller window simply shows more. */
    function getGap() {
        if (!treeEl) return 12;
        var cs = getComputedStyle(treeEl);
        return parseFloat(cs.rowGap || cs.gap) || 12;     // resolve the clamp() to px
    }
    function scrollToFrontier(index) {
        if (!treeEl || !flowEl) return;
        var gap = getGap();
        var pitch = ICON + gap;                            // ICON === item height
        var maxVisible = Math.max(1, Math.floor((flowEl.clientHeight - gap) / pitch));
        var overflow = Math.max(0, index - (maxVisible - 1));
        treeEl.style.transform = 'translateY(' + (-overflow * pitch) + 'px)';
    }

    /* ---------------- timing ---------------- */
    var GAP_MS = 700, HOLD_MS = 2200, FADE_MS = 450, WORK_MS = 1400;  // idle demo
    var STEP_GAP_MS = 950;     // ~1s between opening phases (real run)
    var TOOL_GAP_MS = 340;     // fast cadence between tools (real run)
    var RENDER_TAIL = 12;      // only the last N items are re-rendered each frame

    /* ---------------- driver state ---------------- */
    var flowEl = null, treeEl = null, zoneEl = null, frontier = -1, uid = 0;
    var shimmerWord = null;                              // the newest word (loading shine)
    // The chain stays EMPTY until a real agent run drives it. The demo is opt-in
    // only (window.toolFlow.startTest()) — it never auto-runs on app start.
    var demoOn = false, demoTimer = null;
    var thinkingStep = null, receivedEarly = false, openingDone = true;
    var openTimers = [], toolQ = [], toolTimer = null;

    function clearArr(a) { for (var i = 0; i < a.length; i++) clearTimeout(a[i]); a.length = 0; }

    // The ONE way a step enters the chain: append + build DOM + play its reveal.
    function addStep(spec) {
        var step = { key: 's' + (uid++), shape: spec.shape, label: spec.label, doneLabel: spec.doneLabel, tick: spec.tick };
        STEPS.push(step);
        buildItem(step, treeEl);
        activate(step);
        // the newest item is the "current" one — move the loading shine onto it.
        if (shimmerWord) shimmerWord.classList.remove('shimmer');
        step._word.classList.add('shimmer');
        shimmerWord = step._word;
        return step;
    }

    function clearChain() {
        clearArr(openTimers); clearTimeout(toolTimer); toolTimer = null; toolQ.length = 0;
        clearTimeout(demoTimer);
        for (var i = 0; i < STEPS.length; i++) {
            var s = STEPS[i];
            clearTimeout(s._revealTimer); clearTimeout(s._doneTimer);
            if (s._word) clearTimeout(s._word._typeTimer);
            if (s._item && s._item.parentNode) s._item.parentNode.removeChild(s._item);
        }
        STEPS.length = 0; frontier = -1; thinkingStep = null; receivedEarly = false; openingDone = true; shimmerWord = null;
        if (treeEl) {                                        // snap the conveyor back to top, no animation
            treeEl.style.transition = 'none';
            treeEl.style.transform = 'translateY(0)';
            void treeEl.offsetHeight;
            treeEl.style.transition = '';
        }
        window.resetToolCount();
    }

    /* ---------------- idle demo (loops when no run is active) ---------------- */
    function runDemo() {
        if (!demoOn) return;
        clearChain();
        if (flowEl) flowEl.style.opacity = '1';
        var i = 0;
        (function next() {
            if (!demoOn) return;
            if (i >= DEMO.length) {                          // all shown -> hold, fade, loop
                demoTimer = setTimeout(function () {
                    if (flowEl) flowEl.style.opacity = '0';
                    demoTimer = setTimeout(runDemo, FADE_MS);
                }, HOLD_MS);
                return;
            }
            var sp = DEMO[i];
            var st = addStep(sp);
            if (!sp.phase) window.bumpToolCount();
            if (sp.tick) setTimeout(function () { complete(st); }, WORK_MS * 0.55);
            i++;
            demoTimer = setTimeout(next, GAP_MS);
        })();
    }
    function stopDemo() { demoOn = false; clearTimeout(demoTimer); }

    /* ---------------- real-run driver (fed by window.toolFlow.onFlow) ----------------
       Opening phases auto-advance ~1s apart; "thinking" holds until the real `received`
       event ticks it ("packet received"); tools queue behind the opening, then play fast. */
    function startOpening(hasImage) {
        clearArr(openTimers);
        openingDone = false; thinkingStep = null; receivedEarly = false;
        var specs = [];
        if (hasImage) { specs.push(OPEN.screenshot); specs.push(OPEN.mapping); }
        specs.push(OPEN.communicate);
        specs.push(OPEN.thinking);
        var i = 0;
        (function play() {
            var sp = specs[i];
            if (sp === OPEN.thinking) {
                thinkingStep = addStep(sp);                  // stays active (loader spins) until received
                openingDone = true;
                if (receivedEarly) { receivedEarly = false; complete(thinkingStep); thinkingStep = null; }
                playTools();
            } else {
                addStep(sp);
            }
            i++;
            if (i < specs.length) openTimers.push(setTimeout(play, STEP_GAP_MS));
        })();
    }
    // received{tools}: the packet arrived — tick "packet received", then play this
    // turn's tools (read from the action block) one-by-one, fast.
    function markReceived(tools) {
        toolQ = (tools || []).slice();
        if (thinkingStep) { complete(thinkingStep); thinkingStep = null; playTools(); }
        else { receivedEarly = true; }                       // fast LLM: handled when thinking appears
    }
    function playTools() {
        if (toolTimer || !openingDone || !toolQ.length) return;
        var raw = toolQ.shift();
        var sp = toolSpec(raw);
        var st = addStep(sp);
        window.bumpToolCount();                               // every schema tool counts (incl. done)
        if (sp.tick) setTimeout(function () { complete(st); }, 300);
        if (toolQ.length) toolTimer = setTimeout(function () { toolTimer = null; playTools(); }, TOOL_GAP_MS);
    }
    function toolSpec(p) {
        var name = p.name || '';
        if (name === 'left_click') { var c = p.clicks || 1; name = c >= 3 ? 'left_click_3' : (c === 2 ? 'left_click_2' : 'left_click_1'); }
        return TOOL_MAP[name] || { shape: 'loader', label: (p.name || 'tool') };
    }
    // A terminal "!" drop — agent interrupted (stop) or a backend/LLM error. Not a tool,
    // so it isn't counted; it just caps the chain with a short explanation.
    function showError(text) {
        clearArr(openTimers); clearTimeout(toolTimer); toolTimer = null; toolQ.length = 0;
        thinkingStep = null; receivedEarly = false; openingDone = true;
        addStep({ shape: 'error', label: text || 'something went wrong' });
    }

    /* ---------------- backend bridge ---------------- */
    window.toolFlow = {
        steps: STEPS,
        // backend (app.py) calls: window.toolFlow.onFlow('<event>', '<json payload>')
        onFlow: function (event, payload) {
            if (!treeEl) return;
            var p = {};
            if (payload) { try { p = JSON.parse(payload); } catch (e) { p = {}; } }
            switch (event) {
                case 'run_start': stopDemo(); clearChain(); if (zoneEl) zoneEl.classList.add('flow-started'); break;
                case 'turn':      startOpening(!!p.hasImage); break;
                case 'received':  markReceived(p.tools); break;   // tick + play this turn's tools
                case 'error':     showError(p.text); break;       // "!" drop (stop / backend error)
                case 'run_end':   // run finished: stop the shine on the final item; keep it visible
                    if (shimmerWord) { shimmerWord.classList.remove('shimmer'); shimmerWord = null; }
                    break;
            }
        },
        reset: clearChain,
        stopTest: stopDemo,                                  // back-compat aliases
        startTest: function () { if (!demoOn) { demoOn = true; runDemo(); } },
    };

    /* ---------------- build + mount ---------------- */
    function buildItem(step, tree) {
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
        return item;
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
                zoneEl = zone;                                // .bottom-zone (heading reveal toggles on it)
                flowEl = zone.querySelector('.tool-flow');
                var tree = zone.querySelector('.tool-flow-tree');
                if (tree) {
                    treeEl = tree;
                    startEngine();                            // rAF render loop (renders the live tail)
                    // keep "fill the box, then scroll" correct when the window resizes
                    window.addEventListener('resize', function () { scrollToFrontier(frontier); });
                    // NOTE: no demo here — the chain stays empty until the agent run
                    // drives it via window.toolFlow.onFlow(...).
                }
            })
            .catch(function () { /* non-fatal */ });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }
})();
