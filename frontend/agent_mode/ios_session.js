// iOS session connector — attaches the proven connect logic to the Agent-mode
// menu. Selecting Mobile use → iOS activates the paired iPhone's WDA session
// (fresh session over the cable — no rebuild, no reinstall); switching to
// anything else (Computer use / Android) kills it instantly.
//
// While connecting, the chat box placeholder animates "Pairing." → "Pairing.."
// → "Pairing..." on loop; on success it shows "Device connected" for a moment
// and then returns to the normal "Type your task..." text. On failure the
// placeholder explains, and the menu quietly reverts to Computer use.
//
// Backend contract (all verified end-to-end):
//   POST /api/ios/activate        -> starts forward + XCUITest on the newest
//                                    device in paired_devices.json
//   GET  /api/ios/session-status  -> connecting / connected / error
//   POST /api/ios/deactivate      -> instant kill (phone-side /wda/shutdown
//                                    first, so the overlay drops in ~1s)
(function () {
    'use strict';

    // ---- session core ----
    var statusTimer = null;
    function stopPoll() { if (statusTimer) { clearInterval(statusTimer); statusTimer = null; } }

    function activate(cb) {
        cb = cb || {};
        stopPoll();
        function fail() {
            stopPoll();
            fetch('/api/ios/deactivate', { method: 'POST' }).catch(function () {});
            if (cb.onFail) cb.onFail();
        }
        fetch('/api/ios/activate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})            // backend picks the newest paired device
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (d.state === 'connected') { if (cb.onConnected) cb.onConnected(); return; }
            if (!d.ok) { fail(); return; }
            var deadline = Date.now() + 90000;
            statusTimer = setInterval(function () {
                if (Date.now() > deadline) { fail(); return; }
                fetch('/api/ios/session-status').then(function (r) { return r.json(); }).then(function (s) {
                    if (s.state === 'connected') { stopPoll(); if (cb.onConnected) cb.onConnected(); }
                    else if (s.state === 'error' || s.state === 'disconnected') { fail(); }
                }).catch(function () { /* transient */ });
            }, 2000);
        }).catch(function () { fail(); });
    }

    function deactivate() {
        stopPoll();
        fetch('/api/ios/deactivate', { method: 'POST' }).catch(function () {});
    }

    // ---- chat placeholder animation + breathing glow ----
    function chatInput() { return document.querySelector('.chat-input'); }
    function composer() { return document.querySelector('.input-area-wrapper'); }
    var dotsTimer = null, restoreTimer = null, savedPlaceholder = null;

    // Breathing glow via the Web Animations API. The rest frame EQUALS the
    // steady iOS glow (agent_mode.css), and stopping asks the browser to end
    // the animation exactly at the current cycle's boundary — so the settle
    // into the stable glow is frame-perfect, no JS-timing snap.
    var BREATHE_MS = 1800;
    var GLOW_REST = '0 2px 10px rgba(0,0,0,0.05), ' +
        'inset 0 14px 28px -16px rgba(191,90,242,0.32), ' +
        'inset 14px 0 28px -16px rgba(191,90,242,0.19), ' +
        'inset 0 -14px 28px -16px rgba(94,92,230,0.30), ' +
        'inset -14px 0 28px -16px rgba(94,92,230,0.19)';
    var GLOW_PEAK = '0 2px 10px rgba(0,0,0,0.05), ' +
        'inset 0 16px 32px -12px rgba(191,90,242,0.55), ' +
        'inset 16px 0 32px -12px rgba(191,90,242,0.32), ' +
        'inset 0 -16px 32px -12px rgba(94,92,230,0.50), ' +
        'inset -16px 0 32px -12px rgba(94,92,230,0.32)';
    var breatheAnim = null;

    function startBreathing() {
        var box = composer();
        if (!box || !box.animate || breatheAnim) return;
        breatheAnim = box.animate(
            [{ boxShadow: GLOW_REST }, { boxShadow: GLOW_PEAK }, { boxShadow: GLOW_REST }],
            { duration: BREATHE_MS, iterations: Infinity, easing: 'ease-in-out' }
        );
    }
    function stopBreathing(graceful) {
        if (!breatheAnim) return;
        var anim = breatheAnim;
        breatheAnim = null;
        if (!graceful) { anim.cancel(); return; }
        // Finish the in-flight cycle: cap iterations at the next whole cycle;
        // the browser ends it ON the boundary frame (== steady glow), then
        // cancel() drops the effect with zero visual delta.
        try {
            var cycles = Math.max(1, Math.ceil((anim.currentTime || 1) / BREATHE_MS));
            anim.effect.updateTiming({ iterations: cycles });
            anim.onfinish = function () { anim.cancel(); };
        } catch (e) {
            anim.cancel();
        }
    }

    function startPairingAnim() {
        startBreathing();                            // violet glow breathes while pairing
        var el = chatInput();
        if (!el) return;
        clearTimeout(restoreTimer); restoreTimer = null;
        if (savedPlaceholder === null) savedPlaceholder = el.placeholder;
        clearInterval(dotsTimer);
        var n = 1;
        el.placeholder = 'Pairing.';
        dotsTimer = setInterval(function () {
            n = (n % 3) + 1;                     // . .. ... . .. ...
            var e2 = chatInput();
            if (e2) e2.placeholder = 'Pairing' + '...'.slice(0, n);
        }, 400);
    }
    function endPairingAnim(message, holdMs, gracefulGlow) {
        stopBreathing(gracefulGlow);
        clearInterval(dotsTimer); dotsTimer = null;
        var el = chatInput();
        if (!el) { savedPlaceholder = null; return; }
        if (message) {
            el.placeholder = message;
            restoreTimer = setTimeout(restorePlaceholder, holdMs || 2000);
        } else {
            restorePlaceholder();
        }
    }
    function restorePlaceholder() {
        var el = chatInput();
        if (el && savedPlaceholder !== null) el.placeholder = savedPlaceholder;
        savedPlaceholder = null;
        restoreTimer = null;
    }

    // ---- wiring to the Agent-mode menu ----
    var iosActive = false;

    document.addEventListener('agentmode:changed', function (e) {
        var d = e.detail || {};
        var wantIOS = (d.mode === 'mobile' && d.sub === 'ios');

        if (wantIOS && !iosActive) {
            iosActive = true;
            startPairingAnim();
            activate({
                onConnected: function () {
                    // graceful: the current breath completes, THEN the glow settles
                    endPairingAnim('Device connected', 2200, true);
                },
                onFail: function () {
                    iosActive = false;
                    endPairingAnim('Pairing failed — check Settings → Connect Device', 3200);
                    // quietly put the menu back on Computer use (no re-dispatch)
                    document.dispatchEvent(new CustomEvent('agentmode:set', {
                        detail: { mode: 'computer' }
                    }));
                }
            });
        } else if (!wantIOS && iosActive) {
            iosActive = false;
            deactivate();                        // switched away -> instant kill
            endPairingAnim(null);                // stop any mid-pairing animation
        }
    });

    window.iosSession = { activate: activate, deactivate: deactivate };
})();
