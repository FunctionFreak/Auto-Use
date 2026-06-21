// Bottom container loader — injects the full-width bottom cell into #mainGrid
// (row 2, both columns). Placeholder content for now.
(function () {
    'use strict';

    function mount() {
        var grid = document.getElementById('mainGrid');
        if (!grid || grid.querySelector('.bottom-zone')) return;
        fetch('container/bottom/bottom.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (grid.querySelector('.bottom-zone')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var zone = holder.querySelector('.bottom-zone');
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
