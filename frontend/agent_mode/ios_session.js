// iOS session toggle — THE SAFE, PROVEN CONNECT LOGIC, kept separate while the
// composer UI design is finalized. Nothing calls this right now; when the new
// design is attached, wire its iOS on/off control to:
//
//     window.iosSession.activate(onFail)   // Apple/iOS switched ON
//     window.iosSession.deactivate()       // switched OFF (instant kill)
//
// What it does (all verified end-to-end against the real phone):
//   activate  -> POST /api/ios/activate (backend picks the newest device in
//                paired_devices.json, starts the fresh WDA session over the
//                cable — no rebuild, no reinstall), then polls
//                /api/ios/session-status until 'connected' (up to 90s).
//                On any failure it cleans up and calls onFail() so the UI can
//                revert its control.
//   deactivate-> POST /api/ios/deactivate (kills phone-side runner via WDA's
//                /wda/shutdown first — the "Automation Running" overlay drops
//                within ~1s — then the local processes).
(function () {
    'use strict';

    var statusTimer = null;
    function stopPoll() { if (statusTimer) { clearInterval(statusTimer); statusTimer = null; } }

    function activate(onFail) {
        stopPoll();
        function fail() {
            stopPoll();
            fetch('/api/ios/deactivate', { method: 'POST' }).catch(function () {});
            if (onFail) onFail();
        }
        fetch('/api/ios/activate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})            // backend picks the newest paired device
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (d.state === 'connected') return;                 // already live
            if (!d.ok) { fail(); return; }
            var deadline = Date.now() + 90000;
            statusTimer = setInterval(function () {
                if (Date.now() > deadline) { fail(); return; }
                fetch('/api/ios/session-status').then(function (r) { return r.json(); }).then(function (s) {
                    if (s.state === 'connected') { stopPoll(); }
                    else if (s.state === 'error' || s.state === 'disconnected') { fail(); }
                }).catch(function () { /* transient */ });
            }, 2000);
        }).catch(function () { fail(); });
    }

    function deactivate() {
        stopPoll();
        fetch('/api/ios/deactivate', { method: 'POST' }).catch(function () {});
    }

    window.iosSession = { activate: activate, deactivate: deactivate };
})();
