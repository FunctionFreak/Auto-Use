// Top-left container loader — injects the screenshot cell into #mainGrid
// (column 1, row 1). Also owns the "Agent Notes" hook (window.showAgentNotes),
// fed by app.py when a run ends — the image is hidden and the scratchpad shows.
(function () {
    'use strict';

    // Show the agent's scratchpad notes (numbered) in place of the image. Defined
    // up front (lazy DOM lookup) so it exists before the backend calls it, even
    // though the markup is fetch-injected below. payload = JSON array of strings.
    window.showAgentNotes = function (payload) {
        var entries = payload;
        if (typeof payload === 'string') {
            try { entries = JSON.parse(payload); } catch (e) { entries = []; }
        }
        if (!Array.isArray(entries)) entries = [];
        var zone = document.getElementById('zoneTopLeft');
        var listEl = document.getElementById('agentNotesList');
        if (!zone || !listEl) return;

        listEl.innerHTML = '';
        if (!entries.length) {
            // Empty memory — still replace the screenshot with the notes view.
            var empty = document.createElement('div');
            empty.className = 'agent-note-empty';
            empty.textContent = 'No notes';
            listEl.appendChild(empty);
        } else {
            entries.forEach(function (t, i) {
                var row = document.createElement('div');
                row.className = 'agent-note';
                var num = document.createElement('span');
                num.className = 'agent-note-num';
                num.textContent = (i + 1) + '.';
                var txt = document.createElement('span');
                txt.className = 'agent-note-text';
                txt.textContent = String(t);
                row.appendChild(num);
                row.appendChild(txt);
                listEl.appendChild(row);
            });
        }
        zone.classList.add('notes-active');   // ALWAYS hide the image, show the notes
    };

    // Hide the notes and bring the image area back (called on a new run start).
    window.hideAgentNotes = function () {
        var zone = document.getElementById('zoneTopLeft');
        if (zone) zone.classList.remove('notes-active');
    };

    function mount() {
        var grid = document.getElementById('mainGrid');
        if (!grid || grid.querySelector('.screenshot-zone')) return;
        fetch('container/top_left/top_left.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (grid.querySelector('.screenshot-zone')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var zone = holder.querySelector('.screenshot-zone');
                if (zone) grid.appendChild(zone);
            })
            .catch(function () { /* non-fatal */ });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }
})();
