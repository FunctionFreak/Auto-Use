// CLI stage orchestrator.
//
// Owns the #cliContainer panel (slides up on cli_await; cli/cli_stage.css) and a registry
// of live coder cards keyed by task_id (the card component is cli/cli_container/coder_card.js).
// It just ROUTES the window.cli* event stream (the same events the Telegram banner consumes,
// pushed by app.py send_cli_event_to_frontend) to the right card. Split this way so adding
// more agents later is easy — give each its own card + container slot.
//
// For now: ONE card, centered in #cliContainer. (Multiple agents → multiple stacked
// containers is the next step; the registry below already supports N cards.)
//
// NOTE: script.js assigns its window.cli* inside a DOMContentLoaded handler, so we install
// ours on the macrotask AFTER DOMContentLoaded to win the override.
(function () {
    'use strict';

    var MAX_VISIBLE = 3;               // at most 3 coder cards stacked ("piped") at once
    var cards = new Map();             // task_id -> card (VISIBLE cards only)
    var queue = [];                    // FIFO of { taskId, desc } waiting for a free slot
    var minionParent = new Map();      // minion_id -> parent task_id

    function getContainer() { return document.getElementById('cliContainer'); }
    // 1 card -> full-size; 2 -> halves; 3 -> thirds (drives the height transition in CSS).
    function syncLayout() {
        var c = getContainer(); if (!c) return;
        c.classList.toggle('n2', cards.size === 2);
        c.classList.toggle('n3', cards.size === 3);
    }

    function injectPanel() {
        var grid = document.getElementById('mainGrid');
        if (!grid) return false;
        if (!document.getElementById('cliContainer')) {
            var panel = document.createElement('div');
            panel.id = 'cliContainer';
            panel.setAttribute('aria-hidden', 'true');
            grid.appendChild(panel);
        }
        return true;
    }

    function clearAll() {
        var c = getContainer();
        cards.forEach(function (card) { if (card && card.dispose) card.dispose(); });
        if (c) { c.innerHTML = ''; c.classList.remove('n2', 'n3'); }
        cards.clear();
        queue.length = 0;
        minionParent.clear();
    }

    // Mount one coder agent as a `.cc-agent` row (left tool-chain | center terminal | right
    // scratchpad) appended BELOW any existing card, then fade/slide it in. Oldest stays on top.
    function mountAgent(taskId, desc) {
        var c = getContainer();
        if (!c || !window.CliCoderCard || cards.has(taskId)) return;
        var card = window.CliCoderCard.create(desc);
        var unit = document.createElement('div');
        unit.className = 'cc-agent entering';
        if (card.chainEl) unit.appendChild(card.chainEl);     // "Tool response" — LEFT
        var wrap = document.createElement('div');
        wrap.className = 'cc-card-wrap';
        wrap.appendChild(card.el);                            // terminal — CENTER
        unit.appendChild(wrap);
        if (card.trackEl) unit.appendChild(card.trackEl);     // "tracking progress" — RIGHT
        card._unit = unit;
        c.appendChild(unit);
        cards.set(taskId, card);
        syncLayout();
        // strip .entering next frame so it transitions in (and after the .two height change lands)
        if (window.requestAnimationFrame) requestAnimationFrame(function () { unit.classList.remove('entering'); });
        else unit.classList.remove('entering');
    }

    // Remove a visible agent instantly (its task ended), then pull the next queued one (if any).
    function unmountAgent(taskId) {
        var card = cards.get(taskId);
        if (!card) return;
        if (card.dispose) card.dispose();
        if (card._unit && card._unit.parentNode) card._unit.parentNode.removeChild(card._unit);
        cards.delete(taskId);
        syncLayout();
        if (queue.length) { var next = queue.shift(); mountAgent(next.taskId, next.desc); }
    }

    function installHooks() {
        injectPanel();

        // Slide the panel in / out. The chat box is untouched (no .cli-mode).
        window.cliAwaitStart = function (reason) {
            injectPanel();
            document.body.classList.add('cli-stage');
        };
        window.cliAwaitEnd = function () {
            document.body.classList.remove('cli-stage');
            setTimeout(clearAll, 600);   // clear cards after the slide-down finishes
        };

        // A coder agent starts → mount it if a slot is free, else queue it. Up to 2 show at once;
        // the rest wait FIFO. Bare "[minion] " task_starts are ignored.
        window.cliTaskStart = function (taskId, desc) {
            if (typeof desc === 'string' && desc.indexOf('[minion] ') === 0) return;
            if (cards.has(taskId)) return;                                   // already visible
            for (var i = 0; i < queue.length; i++) if (queue[i].taskId === taskId) return;  // already queued
            if (cards.size < MAX_VISIBLE) mountAgent(taskId, desc);
            else queue.push({ taskId: taskId, desc: desc });
        };
        window.cliTaskLine = function (taskId, line, stream) {
            var card = cards.get(taskId);   // queued (unmounted) agents' lines are dropped until shown
            if (card) card.setLine(line);
        };
        // Todos are handled internally by the agent and intentionally NOT rendered in the UI.
        // The backend still emits todo_update events, so keep a no-op stub to swallow them.
        window.cliTaskTodo = function () {};
        window.cliTaskEnd = function (taskId, status, summary) {
            if (cards.has(taskId)) { unmountAgent(taskId); return; }   // instant vanish + pull next queued
            // finished before it was ever shown → just drop it from the queue
            for (var i = 0; i < queue.length; i++) if (queue[i].taskId === taskId) { queue.splice(i, 1); break; }
        };

        window.cliMinionStart = function (parentId, taskId, query) {
            var card = cards.get(parentId);
            if (!card) return;
            minionParent.set(taskId, parentId);
            card.addMinion(taskId, query);
        };
        window.cliMinionLine = function (taskId, line, stream) {
            var parent = minionParent.get(taskId);
            var card = (parent != null) ? cards.get(parent) : null;
            if (card) card.setMinionLine(taskId, line);
        };
        window.cliMinionEnd = function (taskId, status, summary) {
            var parent = minionParent.get(taskId);
            var card = (parent != null) ? cards.get(parent) : null;
            if (card) card.endMinion(taskId, status);
        };

        window.cliPillWebLoadingStart = function (taskId) { var c = cards.get(taskId); if (c) c.setWeb(true); };
        window.cliPillWebLoadingEnd = function (taskId) { var c = cards.get(taskId); if (c) c.setWeb(false); };
    }

    function schedule() { setTimeout(installHooks, 0); }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', schedule);
    } else {
        schedule();
    }
})();
