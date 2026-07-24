// Skills — two pieces, same fetch-inject pattern as the rest of the app:
//   1) the little scroll ICON, appended inside .agent-mode-wrap (right of the
//      Computer/Mobile use picker), mounted on 'agentmode:ready';
//   2) the STAGE (skills_stage.html), mounted INTO #zoneBigCenter — the full-grid
//      container that shows ONE thing at a time. Clicking the icon reveals the
//      stage (and hides Agent Notes so they never overlap); the × button makes
//      the stage vanish from the container again.
// Design-only: Save does nothing, nothing is persisted, no backend calls.
(function () {
    'use strict';

    function zoneEl()  { return document.getElementById('zoneBigCenter'); }
    function stageEl() { return document.getElementById('skillsStage'); }
    function notesEl() { return document.getElementById('agentNotes'); }

    function skillsOpen() {
        var s = stageEl();
        return !!(s && s.classList.contains('active'));
    }

    // Show Skills in the container — and hide the notes so it's the only thing.
    function openSkills() {
        var stage = stageEl(), zone = zoneEl();
        if (!stage || !zone) return;
        var notes = notesEl();
        if (notes) notes.style.display = 'none';       // one thing at a time
        stage.classList.add('active');
        zone.classList.add('active');
    }

    // Drop the Skills stage but leave the container to whoever else uses it
    // (Agent Notes) — used when the notes want to take the stage back.
    function hideSkillsOnly() {
        var stage = stageEl();
        if (stage) stage.classList.remove('active');
        var notes = notesEl();
        if (notes) notes.style.display = '';           // restore the notes' own visibility
    }

    // Close button: make the content vanish FROM the container (hide the zone too).
    function closeSkills() {
        hideSkillsOnly();
        var zone = zoneEl();
        if (zone) zone.classList.remove('active');
    }

    function wireStage() {
        var stage = stageEl();
        if (!stage) return;

        // Tabs — swap the active tab + its form.
        var tabs = stage.querySelectorAll('.skills-tab');
        var forms = stage.querySelectorAll('.skills-form');
        tabs.forEach(function (tab) {
            tab.addEventListener('click', function () {
                var which = tab.dataset.tab;
                tabs.forEach(function (t) {
                    var on = (t === tab);
                    t.classList.toggle('active', on);
                    t.setAttribute('aria-selected', on ? 'true' : 'false');
                });
                forms.forEach(function (f) {
                    var on = (f.dataset.form === which);
                    f.classList.toggle('active', on);
                    f.hidden = !on;
                });
            });
        });

        // Cancel (per form) makes the stage vanish from the container.
        stage.querySelectorAll('.skills-cancel').forEach(function (btn) {
            btn.addEventListener('click', closeSkills);
        });
    }

    function mountStage() {
        var zone = zoneEl();
        if (!zone || document.getElementById('skillsStage')) return;
        fetch('skills/skills_stage.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (document.getElementById('skillsStage')) return;   // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var stage = holder.querySelector('.skills-stage');
                if (!stage) return;
                zone.appendChild(stage);
                wireStage();
            })
            .catch(function () { /* non-fatal: the stage just won't render */ });
    }

    // Keep the "one thing at a time" rule: any path that shows the Agent Notes
    // first drops the Skills stage. showAgentNotes / showAgentHistory are defined
    // by notes_stage.js (loaded before this) — wrap them; the event-driven show
    // paths (reopen / stop-orb) get listeners.
    function wireCoordination() {
        ['showAgentNotes', 'showAgentHistory'].forEach(function (name) {
            var orig = window[name];
            if (typeof orig === 'function') {
                window[name] = function () { hideSkillsOnly(); return orig.apply(this, arguments); };
            }
        });
        document.addEventListener('chat:opened', hideSkillsOnly);
        document.addEventListener('chat:new', hideSkillsOnly);
        window.addEventListener('message', function (e) {
            if (e.data === 'pcbtn:clicked') hideSkillsOnly();
        });
    }

    function injectIcon() {
        var wrap = document.querySelector('.agent-mode-wrap');
        if (!wrap || document.getElementById('skillsBtn')) return;
        fetch('skills/skills.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (document.getElementById('skillsBtn')) return;     // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var btn = holder.querySelector('.skills-btn');
                if (!btn) return;
                wrap.appendChild(btn);                                // right of the trigger
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();                              // don't poke the agent-mode menu
                    if (skillsOpen()) closeSkills(); else openSkills();
                });
                document.dispatchEvent(new CustomEvent('skills:ready'));
            })
            .catch(function () { /* non-fatal: the icon just won't render */ });
    }

    wireCoordination();

    // Stage mounts once the big-center shell exists (big_center_container.js fires
    // 'bigcenter:ready'); the icon waits for the Computer/Mobile use picker.
    document.addEventListener('bigcenter:ready', mountStage);
    if (zoneEl()) mountStage();
    document.addEventListener('agentmode:ready', injectIcon);
    injectIcon();   // in case agentmode:ready already fired
})();
