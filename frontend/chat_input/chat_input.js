// Chat input component — injects the bottom text box (the output / cli /
// agent-activated strip areas + the input bar with its textarea and the stop-agent
// orb) into #chatWrapper, then wires its behaviour: auto-resize, Enter-to-send and
// the stop button. Same self-contained fetch-inject pattern as chat/chat.js and the
// container/* zones.
//
// Shared app state stays owned by script.js and is read through a tiny window
// interface so this file never holds a second copy:
//   • window.getModelSelection() -> { provider, model }  (live selection)
//   • window.stopStreaming()                              (cancel agent-text stream)
//   • window.setWorkState(state)                          (reset the work-state cell)
// Every one of those is only called at user-event time (Enter / stop click), long
// after script.js has defined them — so injection order doesn't matter.
(function () {
    'use strict';

    // Wire the just-injected text box: auto-resize, Enter-to-send, stop button.
    function wireChatInput(root) {
        const chatInput = (root || document).querySelector('.chat-input');
        if (!chatInput) return;

        // Auto-grow the textarea up to the CSS max-height.
        const adjustHeight = () => {
            chatInput.style.height = 'auto';
            const newHeight = Math.min(chatInput.scrollHeight, 150); // 150px matches CSS max-height
            chatInput.style.height = `${Math.max(newHeight, 44)}px`; // 44px matches CSS min-height base
        };
        chatInput.addEventListener('input', adjustHeight);
        adjustHeight();

        // Enter (without Shift) starts the agent; Shift+Enter inserts a newline.
        chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault(); // Prevent default newline

                const message = chatInput.value.trim();
                // Live (provider, model) selection lives in script.js (Settings writes it).
                const sel = window.getModelSelection ? window.getModelSelection() : {};
                if (message && sel.provider && sel.model) {
                    // Show Agent Response Strip
                    const agentStrip = document.getElementById('agentResponseStrip');
                    const agentText = document.getElementById('agentText');
                    // Stop-agent orb is embedded from pc_button.html (iframe)
                    const stopBtnFrame = document.getElementById('stopBtnFrame');

                    if (agentStrip) {
                        agentStrip.classList.add('active');
                        // Disable input
                        chatInput.disabled = true;
                        chatInput.classList.add('agent-active');
                        agentText.textContent = 'Starting agent...';

                        // Show Stop Button (tell the embedded orb to appear)
                        if (stopBtnFrame) {
                            stopBtnFrame.classList.add('active');
                            stopBtnFrame.contentWindow.postMessage('pcbtn:show', '*');
                        }

                        // Layout note: the screenshot/web/shell now live in the
                        // persistent T-grid (container/ components), so there's no
                        // floating panel to reveal and no split-layout to switch to —
                        // the chat box stays centered at the bottom.

                        // Hide eyes only, keep glow
                        const welcomeEl = document.getElementById('welcomeOverlay');
                        if (welcomeEl) welcomeEl.classList.add('eyes-hidden');

                        // Start the live tracking-progress stream (top-right): reset
                        // any prior run's entries and fade the content in.
                        if (window.trackingProgress) window.trackingProgress.start();
                        const milestoneStream = document.getElementById('milestoneStream');
                        if (milestoneStream) {
                            milestoneStream.innerHTML = '';
                        }

                        // Reset the live todo card (bottom-right) and make sure it's
                        // the visible state — a new run starts with no plan until the
                        // agent writes one (backend also clears todo.md).
                        if (window.updateTodoList) window.updateTodoList({ objective: '', tasks: [] });
                        if (window.setWorkState) window.setWorkState('todo');

                        // Drop the previous run's "Agent Notes" so the screenshot
                        // area is live again for this run (notes reappear on finish).
                        if (window.hideAgentNotes) window.hideAgentNotes();
                    }

                    // Send request to start agent
                    fetch('/api/start-agent', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            provider: sel.provider,
                            model: sel.model,
                            task: message
                        })
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.status === 'started') {
                            agentText.textContent = 'Agent running...';
                            // Clear input after successful start
                            chatInput.value = '';
                            adjustHeight();
                        } else if (data.error) {
                            agentText.textContent = `Error: ${data.error}`;
                            // Re-enable input on error
                            chatInput.disabled = false;
                            if (stopBtnFrame) {
                                stopBtnFrame.classList.remove('active');
                                stopBtnFrame.contentWindow.postMessage('pcbtn:hide', '*');
                            }
                        }
                    })
                    .catch(err => {
                        console.error('Failed to start agent:', err);
                        agentText.textContent = 'Failed to start agent';
                        // Re-enable input on error
                        chatInput.disabled = false;
                        if (stopBtnFrame) {
                            stopBtnFrame.classList.remove('active');
                            stopBtnFrame.contentWindow.postMessage('pcbtn:hide', '*');
                        }
                    });
                }
            }
        });

        // Stop Button click — the orb lives in the pc_button.html iframe and posts
        // 'pcbtn:clicked' back to us (and plays its own pop-vanish animation).
        const stopBtnFrame = document.getElementById('stopBtnFrame');
        window.addEventListener('message', (e) => {
            if (e.data !== 'pcbtn:clicked') return;

            // Stop the agent-text stream immediately (timer is owned by script.js).
            if (window.stopStreaming) window.stopStreaming();

            const agentText = document.getElementById('agentText');
            if (agentText) agentText.textContent = 'Stopping agent...';

            // The orb iframe already plays the pop-vanish; just drop its pointer-events.
            if (stopBtnFrame) stopBtnFrame.classList.remove('active');

            // Force-close any active tool animations immediately
            if (window.webSearchEnd) window.webSearchEnd();
            if (window.shellEnd) window.shellEnd();
            if (window.trackingProgress) window.trackingProgress.end();   // fade the tracking-progress stream out

            // Interrupted by the user: freeze the todo and mark any still-pending
            // tasks with a ✕ (so a spinner doesn't rotate forever). Stop-only —
            // normal completion leaves the ticks alone.
            if (window.markTodoInterrupted) window.markTodoInterrupted();

            fetch('/api/stop-agent', { method: 'POST' })
                .then(res => res.json())
                .then(data => {
                    console.log('Agent stop requested:', data);
                    const agentStrip = document.getElementById('agentResponseStrip');
                    if (agentStrip) agentStrip.classList.remove('active');

                    chatInput.disabled = false;
                    chatInput.classList.remove('agent-active');
                    chatInput.focus();
                })
                .catch(err => console.error('Error stopping agent:', err));
        });
    }

    function mount() {
        var wrap = document.getElementById('chatWrapper');
        if (!wrap || wrap.querySelector('.chat-container-content')) return;
        fetch('chat_input/chat_input.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (wrap.querySelector('.chat-container-content')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var content = holder.querySelector('.chat-container-content');
                if (!content) return;
                wrap.appendChild(content);
                // The markup is now live — wire it (runs once, post-injection).
                wireChatInput(content);
                // Announce for any future listeners (script.js no longer needs this).
                document.dispatchEvent(new CustomEvent('chatinput:ready'));
            })
            .catch(function () { /* non-fatal: the text box just won't render */ });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }
})();
