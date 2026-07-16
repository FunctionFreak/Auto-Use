// Agent mode — injects the composer's bottom-left mode picker (transparent
// trigger showing the selected mode's logo + name, menu floating above with
// Computer use / Mobile use). Always exactly one mode selected (default
// Computer use); picking the other swaps trigger label + logo. Same
// fetch-inject pattern as the other components.
//
// DESIGN-ONLY for now: each change dispatches 'agentmode:changed'
// (detail.mode = 'computer' | 'mobile') and nothing listens yet. The proven
// iOS connect logic is parked in agent_mode/ios_session.js (window.iosSession)
// and gets attached once the design is finalized.
(function () {
    'use strict';

    function wire(wrap) {
        var btn = wrap.querySelector('#agentBtn');
        var menu = wrap.querySelector('#agentMenu');
        var chev = wrap.querySelector('#agentChev');
        var curLabel = wrap.querySelector('#agentCurLabel');
        var opts = wrap.querySelectorAll('.agent-opt');
        var subopts = wrap.querySelectorAll('.agent-subopt');

        // Two-level selection: the mode, plus each mode's chosen sub-option
        // (remembered per mode). Only Computer use has a default (This PC);
        // Mobile use starts with NO platform picked.
        var state = { mode: 'computer', subs: { computer: 'thispc', mobile: null } };
        var MODE_LABEL = { computer: 'Computer use', mobile: 'Mobile use' };

        function paint() {
            opts.forEach(function (o) {
                var on = (o.dataset.mode === state.mode);
                o.classList.toggle('selected', on);
                o.setAttribute('aria-checked', on ? 'true' : 'false');
            });
            subopts.forEach(function (s) {
                var mode = s.closest('.agent-opt-wrap').dataset.mode;
                var on = (state.subs[mode] === s.dataset.sub);
                s.classList.toggle('selected', on);
                s.setAttribute('aria-checked', on ? 'true' : 'false');
            });
            wrap.dataset.mode = state.mode;                       // mode logo (CSS)
            wrap.dataset.sub = state.subs[state.mode] || 'none';  // sub logo; This PC/none -> nothing
            curLabel.textContent = MODE_LABEL[state.mode];
        }

        function commit() {
            paint();
            // Announced for the future wiring (ios_session etc.); nothing
            // listens yet — connection stays unattached by design.
            document.dispatchEvent(new CustomEvent('agentmode:changed', {
                detail: { mode: state.mode, sub: state.subs[state.mode] }
            }));
            setTimeout(function () { setOpen(false); }, 180);
        }

        var open = false;
        function setOpen(v) {
            open = v;
            btn.setAttribute('aria-expanded', v ? 'true' : 'false');
            menu.classList.toggle('open', v);
            chev.style.transform = v ? 'rotate(180deg)' : 'rotate(0deg)';
        }

        btn.addEventListener('click', function () { setOpen(!open); });

        // Main row: select that mode — but ONLY if it has a sub-option picked.
        // Mobile use has no default platform, so clicking it before choosing
        // iOS/Android selects nothing: the list just closes and the previous
        // selection (Computer use) stands. Computer use always has This PC.
        opts.forEach(function (opt) {
            opt.addEventListener('click', function () {
                var mode = opt.dataset.mode;
                if (!state.subs[mode]) {
                    setTimeout(function () { setOpen(false); }, 120);
                    return;               // no platform picked -> no selection change
                }
                state.mode = mode;
                commit();
            });
        });

        // Sub row: select that sub-option AND its parent mode.
        subopts.forEach(function (s) {
            s.addEventListener('click', function (e) {
                e.stopPropagation();
                var mode = s.closest('.agent-opt-wrap').dataset.mode;
                state.mode = mode;
                state.subs[mode] = s.dataset.sub;
                commit();
            });
        });

        document.addEventListener('click', function (e) {
            if (open && !e.target.closest('.agent-mode-wrap')) setOpen(false);
        });

        paint();
    }

    function mount() {
        var wrapper = document.querySelector('.input-area-wrapper');
        if (!wrapper || document.getElementById('agentModeWrap')) return;
        fetch('agent_mode/agent_mode.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (document.getElementById('agentModeWrap')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var wrap = holder.querySelector('.agent-mode-wrap');
                if (!wrap) return;
                wrapper.appendChild(wrap);
                wire(wrap);
                document.dispatchEvent(new CustomEvent('agentmode:ready'));
            })
            .catch(function () { /* non-fatal: the control just won't render */ });
    }

    // chat_input.html is itself fetch-injected — mount on its ready event,
    // with a direct attempt in case that already fired.
    document.addEventListener('chatinput:ready', mount);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }
})();
