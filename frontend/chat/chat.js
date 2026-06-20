// New Chat button component — injects the button at the TOP of the left bar
// body (below the logo/cog header), once the left bar exists. UI only for now;
// the click does nothing yet (integration comes later). Same self-contained
// pattern as settings/settings.js.
(function () {
    'use strict';

    function mount(bar) {
        if (!bar) return;
        var body = bar.querySelector('.left-bar-body');
        if (!body || body.querySelector('.new-chat-btn')) return; // already mounted
        fetch('chat/chat.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (body.querySelector('.new-chat-btn')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var btn = holder.querySelector('.new-chat-btn');
                if (!btn) return;
                body.insertBefore(btn, body.firstChild); // top of the body
                // TODO: wire the click to start a new chat (integration later).
            })
            .catch(function () { /* non-fatal: the button just won't render */ });
    }

    // Mount once the left bar exists: immediately if already injected, otherwise
    // when left_bar.js fires 'leftbar:ready'.
    var existing = document.getElementById('leftBar');
    if (existing) mount(existing);
    document.addEventListener('leftbar:ready', function (e) {
        mount((e && e.detail && e.detail.bar) || document.getElementById('leftBar'));
    });
})();
