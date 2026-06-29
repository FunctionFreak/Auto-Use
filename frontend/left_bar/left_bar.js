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
        var splash = document.getElementById('splashOverlay');
        if (!splash) {            // splash already finished/removed → show now
            reveal(bar);
            return;
        }
        window.addEventListener('message', function (e) {
            if (e && e.data === 'splashDone') reveal(bar);
        });
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
