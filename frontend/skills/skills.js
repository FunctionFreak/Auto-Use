// Skills — two pieces, same fetch-inject pattern as the rest of the app:
//   1) the little scroll ICON, appended inside .agent-mode-wrap (right of the
//      Computer/Mobile use picker), mounted on 'agentmode:ready';
//   2) the STAGE (skills_stage.html), mounted INTO #zoneBigCenter — the full-grid
//      container that shows ONE thing at a time. Clicking the icon reveals the
//      stage (and hides Agent Notes so they never overlap); clicking it again
//      makes the stage vanish from the container.
// COMPUTER USE tab is live: default view lists the active platform's
// agent/skills/*.md through service.py (/api/skills — windows_use or macOS_use
// picked server-side), rows preview on click and delete via the bin; + swaps in
// the add form. Save is still design-only; MOBILE USE is design-only too.
(function () {
    'use strict';

    function zoneEl()  { return document.getElementById('zoneBigCenter'); }
    function stageEl() { return document.getElementById('skillsStage'); }
    function notesEl() { return document.getElementById('agentNotes'); }

    // Skills is "open" only when the stage AND the zone are active. The stage
    // class alone can go stale (a run start hides the zone without touching the
    // stage), and then the icon's first click would "close" an invisible panel.
    function skillsOpen() {
        var s = stageEl(), z = zoneEl();
        return !!(s && s.classList.contains('active') &&
                  z && z.classList.contains('active'));
    }

    // Whether Agent Notes was on screen when Skills opened — closing Skills
    // must return to that exact state (notes back, or the plain 4-zone grid).
    var notesWereShowing = false;

    function notesShowing() {
        var zone = zoneEl(), notes = notesEl();
        return !!(zone && zone.classList.contains('active') &&
                  notes && notes.style.display !== 'none');
    }

    // Icon clicked while the stage markup was still fetching — remembered so
    // mountStage can honour the click the moment the stage lands.
    var pendingOpen = false;

    // Show Skills in the container — and hide the notes so it's the only thing.
    // Always lands on the default view: Computer use tab, fresh file list.
    function openSkills() {
        var stage = stageEl(), zone = zoneEl();
        if (!stage || !zone) { pendingOpen = true; return; }
        pendingOpen = false;
        notesWereShowing = notesShowing();
        var notes = notesEl();
        if (notes) notes.style.display = 'none';       // one thing at a time
        stage.classList.add('active');
        zone.classList.add('active');
        resetStage();
    }

    // ── Computer-use tab: live skills list / preview / delete ──────────
    // The computer panel holds three sub-views (list | preview | add) that the
    // helpers below swap; the list and preview are fed by service.py, which
    // resolves the right skills folder for the OS (windows_use / macOS_use).

    var BIN_SVG =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/>' +
        '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>' +
        '<path d="M10 11v6"/><path d="M14 11v6"/></svg>';

    function computerPanel() {
        var stage = stageEl();
        return stage ? stage.querySelector('.skills-form[data-form="computer"]') : null;
    }

    // Every view change bumps viewToken so an in-flight preview fetch from a
    // PREVIOUS state (user reset the stage, went to add, clicked another file)
    // can tell it's stale and drop its response instead of yanking the view.
    var viewToken = 0;

    function showComputerView(which) {
        var panel = computerPanel();
        if (!panel) return;
        viewToken++;
        panel.querySelectorAll('.skills-view').forEach(function (v) {
            v.classList.toggle('active', v.dataset.view === which);
        });
    }

    function renderSkillsList(names) {
        var list = document.getElementById('skillsList');
        if (!list) return;
        list.innerHTML = '';
        if (!names.length) {
            var empty = document.createElement('div');
            empty.className = 'skills-empty';
            empty.textContent = 'No skills yet — click + to add one.';
            list.appendChild(empty);
            return;
        }
        names.forEach(function (name) {
            var row = document.createElement('div');
            row.className = 'skills-item';

            var open = document.createElement('button');
            open.type = 'button';
            open.className = 'skills-item-name';
            open.textContent = name;
            open.title = 'Preview ' + name;
            open.addEventListener('click', function () { openPreview(name); });

            var del = document.createElement('button');
            del.type = 'button';
            del.className = 'skills-item-del';
            del.setAttribute('aria-label', 'Delete ' + name);
            del.title = 'Delete';
            del.innerHTML = BIN_SVG;
            del.addEventListener('click', function () { deleteSkill(name); });

            row.appendChild(open);
            row.appendChild(del);
            list.appendChild(row);
        });
    }

    // Only the LATEST list request may render — two rapid deletes issue two
    // refreshes, and the first response can arrive last (stale listing).
    var listReqId = 0;
    function refreshSkillsList() {
        var id = ++listReqId;
        fetch('/api/skills')
            .then(function (r) { return r.json(); })
            .then(function (d) { if (id === listReqId) renderSkillsList((d && d.skills) || []); })
            .catch(function () { if (id === listReqId) renderSkillsList([]); });
    }

    function openPreview(name) {
        var token = ++viewToken;               // a newer click/navigation invalidates us
        function land(content) {
            if (token === viewToken) showPreview(name, content);
        }
        fetch('/api/skills/' + encodeURIComponent(name))
            .then(function (r) { return r.json(); })
            .then(function (d) {
                land((d && typeof d.content === 'string')
                    ? d.content : 'Couldn’t load this skill.');
            })
            .catch(function () { land('Couldn’t load this skill.'); });
    }

    var currentPreviewName = '';           // file shown in the preview / being edited

    function showPreview(name, content) {
        currentPreviewName = name;
        setEditMode(false);                // fresh preview always starts read-only
        var nameEl = document.getElementById('skillsPreviewName');
        var bodyEl = document.getElementById('skillsPreviewBody');
        if (nameEl) nameEl.textContent = name;
        if (bodyEl) bodyEl.textContent = content;
        showComputerView('preview');
    }

    // Edit mode: swap the read-only <pre> for its textarea twin (seeded with
    // the current text) and Edit for Save in the header. Off restores the pre.
    function setEditMode(on) {
        var pre = document.getElementById('skillsPreviewBody');
        var box = document.getElementById('skillsPreviewEdit');
        var editBtn = document.getElementById('skillsEditBtn');
        var saveBtn = document.getElementById('skillsSaveBtn');
        if (!pre || !box || !editBtn || !saveBtn) return;
        if (on) box.value = pre.textContent;
        pre.hidden = on;
        box.hidden = !on;
        editBtn.hidden = on;
        saveBtn.hidden = !on;
        saveBtn.textContent = 'Save';
        if (on) box.focus();
    }

    function saveEdit() {
        var box = document.getElementById('skillsPreviewEdit');
        var name = currentPreviewName;
        if (!box || !name) return;
        var content = box.value;
        var token = viewToken;             // if the user navigates away before the
                                           // PUT resolves, don't touch the new view
                                           // (the file itself still saved fine)
        fetch('/api/skills/' + encodeURIComponent(name), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: content })
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (token !== viewToken) return;
                if (d && d.status === 'saved') {
                    var pre = document.getElementById('skillsPreviewBody');
                    if (pre) pre.textContent = content;
                    setEditMode(false);
                } else {
                    flashSaveError();
                }
            })
            .catch(function () { if (token === viewToken) flashSaveError(); });
    }

    // Failed save: keep the edit (nothing is lost), tell the user on the button.
    function flashSaveError() {
        var btn = document.getElementById('skillsSaveBtn');
        if (!btn) return;
        btn.textContent = 'Couldn’t save';
        setTimeout(function () { btn.textContent = 'Save'; }, 1600);
    }

    // Idempotent server-side; refresh the list whether or not it succeeded.
    function deleteSkill(name) {
        fetch('/api/skills/' + encodeURIComponent(name), { method: 'DELETE' })
            .catch(function () {})
            .then(function () { refreshSkillsList(); });
    }

    // Default state every time the stage opens: Computer tab active, + visible,
    // list view showing a fresh fetch of the skill files.
    function resetStage() {
        var stage = stageEl();
        if (!stage) return;
        stage.querySelectorAll('.skills-tab').forEach(function (t) {
            var on = (t.dataset.tab === 'computer');
            t.classList.toggle('active', on);
            t.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        stage.querySelectorAll('.skills-form').forEach(function (f) {
            var on = (f.dataset.form === 'computer');
            f.classList.toggle('active', on);
            f.hidden = !on;
        });
        var add = stage.querySelector('.skills-add');
        if (add) add.hidden = false;
        showComputerView('list');
        refreshSkillsList();
    }

    // Drop the Skills stage but leave the container to whoever else uses it
    // (Agent Notes) — used when the notes want to take the stage back.
    function hideSkillsOnly() {
        var stage = stageEl();
        if (stage) stage.classList.remove('active');
        var notes = notesEl();
        if (notes) notes.style.display = '';           // restore the notes' own visibility
    }

    // Close button: put the screen back the way it was before Skills opened.
    // If Agent Notes was showing, leave the zone active (hideSkillsOnly already
    // restored the notes); otherwise hide the zone so the 4-zone grid returns.
    // Never ADDS active — if something else (new run, New chat) hid the zone
    // while Skills was open, closing must not resurrect stale notes.
    function closeSkills() {
        hideSkillsOnly();
        var zone = zoneEl();
        if (zone && !notesWereShowing) {
            zone.classList.remove('active');
            // Re-hide the (empty) notes so they don't ghost through the zone's
            // 0.35s fade-out; every show path runs hideSkillsOnly first, which
            // restores display before the notes are next revealed.
            var notes = notesEl();
            if (notes) notes.style.display = 'none';
        }
        notesWereShowing = false;
    }

    function wireStage() {
        var stage = stageEl();
        if (!stage) return;

        // Tabs — swap the active tab + its form. The + button only belongs to
        // the Computer tab (Mobile is just the form), so it hides with it.
        var tabs = stage.querySelectorAll('.skills-tab');
        var forms = stage.querySelectorAll('.skills-form');
        var addBtn = stage.querySelector('.skills-add');
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
                if (addBtn) addBtn.hidden = (which !== 'computer');
            });
        });

        // + shows the add form; ← in the preview goes back to the list
        // (dropping any unsaved edit — Save is the explicit keep).
        if (addBtn) addBtn.addEventListener('click', function () { showComputerView('add'); });
        stage.querySelectorAll('.skills-back').forEach(function (btn) {
            btn.addEventListener('click', function () {
                setEditMode(false);
                showComputerView('list');
            });
        });

        // Preview header: Edit swaps to the editable body, Save writes it back.
        var editBtn = stage.querySelector('#skillsEditBtn');
        var saveBtn = stage.querySelector('#skillsSaveBtn');
        if (editBtn) editBtn.addEventListener('click', function () { setEditMode(true); });
        if (saveBtn) saveBtn.addEventListener('click', saveEdit);

        // Cancel: the Computer form is a sub-view reached via +, so its Cancel
        // returns to the file list; Mobile's still closes the whole stage.
        stage.querySelectorAll('.skills-form[data-form="computer"] .skills-cancel').forEach(function (btn) {
            btn.addEventListener('click', function () { showComputerView('list'); });
        });
        stage.querySelectorAll('.skills-form[data-form="mobile"] .skills-cancel').forEach(function (btn) {
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
                if (pendingOpen) { pendingOpen = false; openSkills(); }
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
        // New chat: hideAgentNotes already dropped the zone (its fade-out is
        // running) before 'chat:new' fires, so restoring the notes' display here
        // would let the empty header ghost through the fade. Stage class checked
        // directly — skillsOpen() is false once the zone is off.
        document.addEventListener('chat:new', function () {
            var stage = stageEl();
            var wasOpen = !!(stage && stage.classList.contains('active'));
            hideSkillsOnly();
            if (!wasOpen) return;
            var zone = zoneEl(), notes = notesEl();
            if (notes && !(zone && zone.classList.contains('active'))) {
                notes.style.display = 'none';
            }
        });
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
