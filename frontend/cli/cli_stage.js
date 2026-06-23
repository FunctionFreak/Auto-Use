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

    var MAX_CARDS = 1;                 // raise + add container slots to support more agents
    var cards = new Map();             // task_id -> card (from window.CliCoderCard.create)
    var minionParent = new Map();      // minion_id -> parent task_id

    function getContainer() { return document.getElementById('cliContainer'); }

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
        if (c) c.innerHTML = '';
        cards.clear();
        minionParent.clear();
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

        // A coder agent starts → create its card. Bare "[minion] " task_starts are ignored.
        window.cliTaskStart = function (taskId, desc) {
            if (typeof desc === 'string' && desc.indexOf('[minion] ') === 0) return;
            if (cards.has(taskId) || cards.size >= MAX_CARDS) return;
            var c = getContainer();
            if (!c || !window.CliCoderCard) return;
            var card = window.CliCoderCard.create(desc);
            c.innerHTML = '';            // (single-card slot for now)
            if (card.chainEl) c.appendChild(card.chainEl);   // "Tool response" zone — extreme LEFT
            var wrap = document.createElement('div');
            wrap.className = 'cc-card-wrap';
            wrap.appendChild(card.el);                        // terminal card — centered
            c.appendChild(wrap);
            if (card.trackEl) c.appendChild(card.trackEl);    // "tracking progress" zone — RIGHT
            cards.set(taskId, card);
        };
        window.cliTaskLine = function (taskId, line, stream) {
            var card = cards.get(taskId);
            if (card) card.setLine(line);
        };
        window.cliTaskTodo = function (taskId, payload) {
            var card = cards.get(taskId);
            if (card) card.setTodos(payload);
        };
        window.cliTaskEnd = function (taskId, status, summary) {
            var card = cards.get(taskId);
            if (card) card.setDone(status, summary);
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
