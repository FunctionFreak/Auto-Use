// Tool Response zone component — injects the full-width bottom cell into
// #mainGrid. UI only for now; tool-response text renders here later.
(function () {
    'use strict';

    function mount() {
        var grid = document.getElementById('mainGrid');
        if (!grid || grid.querySelector('.tool-response-zone')) return;
        fetch('tool_response/tool_response.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (grid.querySelector('.tool-response-zone')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var zone = holder.querySelector('.tool-response-zone');
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
