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
    var idleCard = null;               // the waiting terminal shown in Shell use

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
        idleCard = null;                  // innerHTML wiped it too
        if (shellPinned) mountIdle();     // Shell use never shows a bare panel
    }

    // Shell use gets the terminal header — blinking dot + name, the agent head
    // under it, then the `>` prompt everything streams from. The other modes keep
    // the plain `> AutoUse Code` card.
    function cardOpts() { return shellPinned ? { header: 'AutoUse Code' } : {}; }

    // One agent row: left tool-chain | center terminal | right scratchpad.
    // Shared by the live cards and the idle placeholder so they're the same shape.
    function buildUnit(card) {
        var unit = document.createElement('div');
        unit.className = 'cc-agent';
        if (card.chainEl) unit.appendChild(card.chainEl);     // "Tool response" — LEFT
        var wrap = document.createElement('div');
        wrap.className = 'cc-card-wrap';
        wrap.appendChild(card.el);                            // terminal — CENTER
        unit.appendChild(wrap);
        if (card.trackEl) unit.appendChild(card.trackEl);     // "tracking progress" — RIGHT
        return unit;
    }

    // Mount one coder agent appended BELOW any existing card, then fade/slide it
    // in. Oldest stays on top.
    function mountAgent(taskId, desc) {
        var c = getContainer();
        if (!c || !window.CliCoderCard || cards.has(taskId)) return;

        // Shell use: PROMOTE the waiting terminal rather than swapping it out. It is already
        // the same card, in the same place, with the same live mascot canvas — tearing it
        // down and sliding an identical replacement up from 16px is exactly the jitter you
        // see on send. Promotion just attaches the two side zones and starts the run, so the
        // terminal simply carries on. Nothing to animate, nothing to repaint.
        if (idleCard && !cards.size) {
            var promoted = idleCard;
            var pUnit = promoted._unit;
            idleCard = null;                                   // it's a live card now
            pUnit.classList.remove('cc-idle');
            if (promoted.chainEl) pUnit.insertBefore(promoted.chainEl, pUnit.firstChild);
            if (promoted.trackEl) pUnit.appendChild(promoted.trackEl);
            cards.set(taskId, promoted);
            syncLayout();
            if (promoted.begin) promoted.begin();              // opening phase starts NOW
            return;
        }

        unmountIdle();                    // (other modes / extra agents) idle makes way
        var card = window.CliCoderCard.create(desc, cardOpts());
        var unit = buildUnit(card);
        unit.classList.add('entering');
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
        else if (shellPinned && !cards.size) mountIdle();   // back to the waiting terminal
    }

    // Shell use PINS the stage open: the terminal is that mode's home screen, so
    // the panel slides up the moment the mode is picked and stays after a run
    // ends (empty, waiting for the next task) instead of sliding away. Every
    // other mode keeps the original behaviour — the stage exists only for the
    // duration of a cli_await.
    var shellPinned = false;    // Shell use selected?
    var awaitActive = false;    // a cli_await is in flight?

    // The waiting terminal: a normal coder card that's simply never fed any
    // events, so it sits there as an empty `>` prompt with its blinking cursor.
    // It keeps the mode looking like a terminal instead of a blank panel; a real
    // run swaps it out, and it comes back when the run ends.
    //
    // TERMINAL ONLY — the card's chainEl / trackEl are deliberately NOT passed to
    // buildUnit, so "Tool response" and "tracking progress" don't exist until a
    // real run mounts its own card and has something to put in them.
    function mountIdle() {
        var c = getContainer();
        if (!c || idleCard || cards.size || !window.CliCoderCard) return;
        // idle:true — don't start the opening phase; mountAgent's promotion calls begin().
        var opts = cardOpts();
        opts.idle = true;
        idleCard = window.CliCoderCard.create('', opts);
        var unit = buildUnit({ el: idleCard.el });
        unit.classList.add('cc-idle');
        idleCard._unit = unit;
        c.appendChild(unit);
    }

    function unmountIdle() {
        if (!idleCard) return;
        if (idleCard.dispose) idleCard.dispose();
        if (idleCard._unit && idleCard._unit.parentNode) {
            idleCard._unit.parentNode.removeChild(idleCard._unit);
        }
        idleCard = null;
    }

    function syncShellStage() {
        var wrap = document.getElementById('agentModeWrap');
        shellPinned = !!wrap && wrap.dataset.mode === 'shell';
        // Shell use re-lays-out the card: full-width terminal on top, the two
        // side zones moved BELOW it (coder_card.css). Dispatched-coder runs in
        // the other modes keep the left | center | right columns.
        document.body.classList.toggle('cli-shell', shellPinned);
        if (shellPinned) {
            injectPanel();
            document.body.classList.add('cli-stage');
            mountIdle();
        } else {
            unmountIdle();
            // left Shell use — but never yank the stage out from under a live run
            if (!awaitActive) document.body.classList.remove('cli-stage');
        }
    }

    function installHooks() {
        injectPanel();

        // Slide the panel in / out. The chat box is untouched (no .cli-mode).
        window.cliAwaitStart = function (reason) {
            injectPanel();
            awaitActive = true;
            document.body.classList.add('cli-stage');
        };
        window.cliAwaitEnd = function () {
            awaitActive = false;
            if (!shellPinned) document.body.classList.remove('cli-stage');
            setTimeout(clearAll, 600);   // clear cards after the slide-down finishes
        };

        // agent_mode.js paints the picker's data-mode BEFORE dispatching either
        // event, so reading it back here is always current.
        document.addEventListener('agentmode:ready', syncShellStage);
        document.addEventListener('agentmode:changed', syncShellStage);
        syncShellStage();   // in case the picker mounted first

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
        // Todos are handled internally by the agent and NOT rendered as a checklist. The
        // card swallows them in every mode except Shell use, where the terminal is the
        // full transcript and setTodo() streams the list into it.
        window.cliTaskTodo = function (taskId, payload) {
            var card = cards.get(taskId);
            if (card && card.setTodo) card.setTodo(payload);
        };
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

        // Stage-level message straight onto the terminal. In Shell use the four zones
        // that normally carry errors (milestone stream et al.) are hidden, so a run that
        // fails before producing any output would otherwise read as "nothing happened".
        window.cliShellNote = function (text) {
            var card = idleCard;
            if (!card) { var first = cards.values().next(); card = first && first.value; }
            if (card && card.note) card.note(text);
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
