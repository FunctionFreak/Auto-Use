// Left Bar loader — fetches the panel markup, injects it into the top document
// once, and reveals it when the splash animation finishes (or immediately if
// the splash is already gone). Kept out of the monolithic script.js so the
// component stays self-contained in its own folder.
(function () {
    'use strict';

    function reveal(bar) {
        // rAF so the initial hidden state is committed before the transition.
        requestAnimationFrame(function () { bar.classList.add('revealed'); });
    }

    function revealWhenReady(bar) {
        // Reveal as soon as the bar is injected — the splash overlay still covers
        // it until splashDone, so there is no extra fade lag after the animation.
        reveal(bar);
    }

    // GitHub star pill: live stargazer count (refreshed every 5 minutes).
    // TEMP: the original repo is suspended, so the click-through to GitHub is
    // paused — clicking shows a small notice card instead (see showNote below).
    // Restore by swapping the click handler back to POST /api/open-github.
    // The count refresh is left running: it fails silently while the repo is
    // down and self-heals the moment the repo is back.
    var GH_REPO = 'FunctionFreak/Auto-Use';
    function wireGithubPill(bar) {
        var pill = bar.querySelector('#ghStarPill');
        if (!pill) return;
        var countEl = pill.querySelector('#ghStarCount');
        function fmt(n) {
            if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k';
            return String(n);
        }
        function refresh() {
            fetch('https://api.github.com/repos/' + GH_REPO)
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    if (d && typeof d.stargazers_count === 'number' && countEl) {
                        countEl.textContent = fmt(d.stargazers_count);
                    }
                })
                .catch(function () { /* keep last value */ });
        }
        // TEMP notice card, appended to <body> (the bar clips overflow) and
        // anchored under the pill. Toggles on pill click, closes on outside
        // click / Escape, and auto-dismisses after a few seconds.
        var note = null, hideTimer = null;
        function noteVisible() { return !!(note && note.classList.contains('visible')); }
        function hideNote() {
            if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
            if (note) note.classList.remove('visible');
        }
        function showNote() {
            if (!note) {
                note = document.createElement('div');
                note.className = 'gh-suspend-note';
                note.setAttribute('role', 'status');
                note.innerHTML =
                    '<div class="gh-suspend-title">GitHub link paused</div>' +
                    '<div class="gh-suspend-text">The original Auto-Use repository has been ' +
                    'suspended and we’re working on getting it restored. This is a fork ' +
                    'build — everything here keeps working as normal.</div>';
                document.body.appendChild(note);
                document.addEventListener('click', function (e) {
                    if (noteVisible() && !note.contains(e.target) && !pill.contains(e.target)) {
                        hideNote();
                    }
                });
                document.addEventListener('keydown', function (e) {
                    if (e.key === 'Escape') hideNote();
                });
            }
            var r = pill.getBoundingClientRect();
            note.style.top = (r.bottom + 10) + 'px';
            note.style.left = Math.max(10, Math.min(r.left, window.innerWidth - note.offsetWidth - 10)) + 'px';
            // rAF so the first paint commits the hidden state and the fade runs.
            requestAnimationFrame(function () { note.classList.add('visible'); });
            if (hideTimer) clearTimeout(hideTimer);
            hideTimer = setTimeout(hideNote, 6000);
        }
        pill.addEventListener('click', function () {
            if (noteVisible()) hideNote(); else showNote();
        });
        refresh();
        setInterval(refresh, 5 * 60 * 1000);
    }

    function inject() {
        fetch('left_bar/left_bar.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (document.getElementById('leftBar')) return; // guard double-inject
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var bar = holder.querySelector('.left-bar');
                if (!bar) return;
                document.body.appendChild(bar);
                // Let dependent components (e.g. the settings button) mount into
                // the bar now that it exists in the DOM.
                document.dispatchEvent(new CustomEvent('leftbar:ready', { detail: { bar: bar } }));
                wireGithubPill(bar);
                revealWhenReady(bar);
            })
            .catch(function () { /* non-fatal: the bar simply won't render */ });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', inject);
    } else {
        inject();
    }
})();
