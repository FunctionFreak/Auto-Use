// Mode dial component — injects the Computer/Mobile dial (mode_dial.html → an
// iframe hosting mode_dial/dial.html) into the composer's bottom-left, then keeps
// it in sync with the run state: visible while idle, pop-vanish on send, spring
// back when the composer returns to idle. Same self-contained fetch-inject
// pattern as chat_input/chat_input.js and the container/* zones.
//
// No agent wiring yet: the dial's 'modedial:mode:*' messages are emitted but not
// consumed — integration comes later. Run state is read passively off the
// .chat-input's 'agent-active' class (a MutationObserver), so chat_input.js
// needs no knowledge of this component.
(function () {
    'use strict';

    function postToDial(msg) {
        var frame = document.getElementById('modeDialFrame');
        if (frame && frame.contentWindow) frame.contentWindow.postMessage(msg, '*');
    }

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
