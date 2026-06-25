// Memory bar component — injects the far-right vertical token gauge into <body>
// (fixed, background, pointer-transparent), and exposes the backend hooks:
//   window.updateMemoryBar(used, cap)  — set the fill height (% of cap, clamped);
//                                         the backend calls this each LLM call.
//   window.resetMemoryBar()            — back to 0 (a brand-new chat).
// Same self-contained fetch-inject pattern as left_bar.js / chat.js.
(function () {
    'use strict';

    var CAP = 500000;

    function inject() {
        if (document.getElementById('memoryBar')) return; // already mounted
        fetch('memory_bar/memory_bar.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (document.getElementById('memoryBar')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var bar = holder.querySelector('.memory-bar');
                if (bar) document.body.appendChild(bar);
            })
            .catch(function () { /* non-fatal: the bar just won't render */ });
    }

    // Set the fill to used/cap (clamped 0..100%); turn red at/over the cap.
    window.updateMemoryBar = function (used, cap) {
        cap = cap || CAP;
        var pct = Math.max(0, Math.min(100, (Number(used) || 0) / cap * 100));
        var fill = document.querySelector('.memory-bar .memory-bar-fill');
        if (fill) fill.style.height = pct + '%';
        var bar = document.getElementById('memoryBar');
        if (bar) bar.classList.toggle('full', pct >= 100);
    };

    // Brand-new chat: empty the bar.
    window.resetMemoryBar = function () { window.updateMemoryBar(0, CAP); };

    // Visibility — the bar is hidden by default and only shown while the agent
    // is running (shown on send, hidden when the run ends).
    window.showMemoryBar = function () {
        var bar = document.getElementById('memoryBar');
        if (bar) bar.classList.add('visible');
    };
    window.hideMemoryBar = function () {
        var bar = document.getElementById('memoryBar');
        if (bar) bar.classList.remove('visible');
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', inject);
    } else {
        inject();
    }
})();
