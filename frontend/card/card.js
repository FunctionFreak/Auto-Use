// Globe / Shell zone component — injects the top-right cell into #mainGrid.
// UI only for now; the globe (web search) + shell terminal render here later.
(function () {
    'use strict';

    function mount() {
        var grid = document.getElementById('mainGrid');
        if (!grid || grid.querySelector('.globe-shell-zone')) return;
        fetch('card/card.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (grid.querySelector('.globe-shell-zone')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var zone = holder.querySelector('.globe-shell-zone');
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
