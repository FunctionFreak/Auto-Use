// Mode dial component — injects the Computer/Mobile dial (mode_dial.html → an
// iframe hosting mode_dial/dial.html) into the composer's bottom-left, then keeps
// it in sync with the run state: visible while idle, pop-vanish on send, spring
// back when the composer returns to idle. Same self-contained fetch-inject
// pattern as chat_input/chat_input.js and the container/* zones.
//
// The dial's Apple logo is the WDA session toggle: selecting it activates the
// paired iPhone (fresh session over the cable — no reinstall; the pairing
// itself happens once in Settings → Connect Device), unselecting deactivates.
// If activation fails (no paired device / cable out / trust missing) the logo
// reverts to unselected. 'modedial:mode:*' stays display-only for now. Run
// state is read passively off the .chat-input's 'agent-active' class (a
// MutationObserver), so chat_input.js needs no knowledge of this component.
(function () {
    'use strict';

    function postToDial(msg) {
        var frame = document.getElementById('modeDialFrame');
        if (frame && frame.contentWindow) frame.contentWindow.postMessage(msg, '*');
    }

    // ---- Apple-logo session toggle ----
    var statusTimer = null;
    function stopStatusPoll() { if (statusTimer) { clearInterval(statusTimer); statusTimer = null; } }

    function activateIOS() {
        stopStatusPoll();
        fetch('/api/ios/activate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})            // backend picks the newest paired device
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (d.state === 'connected') return;                 // already live
            if (!d.ok) { postToDial('modedial:platform:none'); return; }
            // connecting -> poll until the phone really answers (or fails)
            var deadline = Date.now() + 90000;
            statusTimer = setInterval(function () {
                if (Date.now() > deadline) { fail(); return; }
                fetch('/api/ios/session-status').then(function (r) { return r.json(); }).then(function (s) {
                    if (s.state === 'connected') { stopStatusPoll(); }
                    else if (s.state === 'error' || s.state === 'disconnected') { fail(); }
                }).catch(function () { /* transient */ });
            }, 2000);
            function fail() {                                    // clean up + revert the logo
                stopStatusPoll();
                fetch('/api/ios/deactivate', { method: 'POST' }).catch(function () {});
                postToDial('modedial:platform:none');
            }
        }).catch(function () { postToDial('modedial:platform:none'); });
    }

    function deactivateIOS() {
        stopStatusPoll();
        fetch('/api/ios/deactivate', { method: 'POST' }).catch(function () {});
    }

    window.addEventListener('message', function (e) {
        if (e.data === 'modedial:platform:ios') activateIOS();
        else if (e.data === 'modedial:platform:none') deactivateIOS();
        else if (e.data === 'modedial:platform:android') deactivateIOS();   // switching platform ends the iOS session
    });

    // Mirror the composer's run state onto the dial. agent-active appears the
    // moment a task is sent (chat_input.js startAgent) and leaves on restoreIdle
    // — exactly the show/vanish moments the dial wants.
    function wireRunState() {
        var chatInput = document.querySelector('.chat-input');
        if (!chatInput) return;

        var running = chatInput.classList.contains('agent-active');
        var observer = new MutationObserver(function () {
            var now = chatInput.classList.contains('agent-active');
            if (now === running) return;   // class changed for some other reason
            running = now;
            postToDial(now ? 'modedial:vanish' : 'modedial:show');
        });
        observer.observe(chatInput, { attributes: true, attributeFilter: ['class'] });
    }

    function mount() {
        var wrapper = document.querySelector('.input-area-wrapper');
        if (!wrapper || document.getElementById('modeDialSlot')) return;
        fetch('mode_dial/mode_dial.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (document.getElementById('modeDialSlot')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var slot = holder.querySelector('.mode-dial-slot');
                if (!slot) return;
                wrapper.appendChild(slot);
                wireRunState();
                // Announce for the future agent wiring (mirrors 'chatinput:ready').
                document.dispatchEvent(new CustomEvent('modedial:ready'));
            })
            .catch(function () { /* non-fatal: the dial just won't render */ });
    }

    // chat_input.html is itself fetch-injected, so the wrapper usually doesn't
    // exist yet when this script runs — mount on chat_input's ready event, with
    // a direct attempt in case that already fired.
    document.addEventListener('chatinput:ready', mount);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }
})();
