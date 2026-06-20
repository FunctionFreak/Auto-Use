// Screenshot zone component — injects the top-left glass cell into #mainGrid.
// UI only for now; the agent screenshot will render here later. Same
// fetch+inject pattern as the other components (target is the static #mainGrid,
// so it mounts on DOMContentLoaded — no leftbar:ready needed).
(function () {
    'use strict';

    function mount() {
        var grid = document.getElementById('mainGrid');
        if (!grid || grid.querySelector('.screen-shot-zone')) return;
        fetch('screen_shot/screen_shot.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (grid.querySelector('.screen-shot-zone')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var zone = holder.querySelector('.screen-shot-zone');
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
