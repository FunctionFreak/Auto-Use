// New Chat button + chat-history list — injected at the TOP of the left bar
// body (below the logo/cog header), once the left bar exists. Owns the UI for
// permanent chat memory:
//   • New chat  -> drop the active session (window.currentSessionId = null),
//     reset the screen, ready for a fresh start.
//   • History   -> GET /api/chats; click a row to reopen that session (adopts
//     its id so the next send CONTINUES it, and shows its last "done" message
//     on the full-grid notes stage via window.showAgentNotes).
//   • Delete    -> DELETE /api/chats/<id>.
// All persistence lives in the backend (Auto_Use/agent_conversation); this file
// is pure UI. Same self-contained fetch-inject pattern as settings/settings.js.
(function () {
    'use strict';

    function escapeAttr(s) { return String(s == null ? '' : s); }

    // Fetch the session list and (re)render the history rows.
    function loadChats(list) {
        if (!list) return;
        fetch('/api/chats')
            .then(function (r) { return r.json(); })
            .then(function (rows) { renderChats(list, rows || []); })
            .catch(function () { /* non-fatal: list just stays as-is */ });
    }

    function renderChats(list, rows) {
        list.innerHTML = '';
        rows.forEach(function (row) {
            var item = document.createElement('div');
            item.className = 'chat-history-item';
            if (row.id === window.currentSessionId) item.classList.add('active');
            item.dataset.id = row.id;
            item.title = escapeAttr(row.name);

            var label = document.createElement('span');
            label.className = 'chat-history-name';
            label.textContent = row.name || 'New chat';
            item.appendChild(label);

            var more = document.createElement('button');
            more.type = 'button';
            more.className = 'chat-history-more';
            more.setAttribute('aria-label', 'Chat options');
            more.textContent = '⋯';   // ⋯ horizontal ellipsis
            more.addEventListener('click', function (e) {
                e.stopPropagation();       // open the menu, not the chat
                openItemMenu(row.id, more, list);
            });
            item.appendChild(more);

            item.addEventListener('click', function () { openChat(row.id, list); });
            list.appendChild(item);
        });
    }

    // Reopen a saved session: adopt its id (so the next send continues it) and
    // show ONLY its last done message in the top-left container (the reopen view).
    function openChat(id, list) {
        window.currentSessionId = id;
        Array.prototype.forEach.call(list.children, function (el) {
            el.classList.toggle('active', el.dataset.id === id);
        });
        if (window.resetChatUi) window.resetChatUi();   // clear leftover live view
        if (window.hideWelcomeHero) window.hideWelcomeHero();   // reopening != empty state
        // Announce the reopen (the notes stage shows itself on this).
        document.dispatchEvent(new CustomEvent('chat:opened', { detail: { id: id } }));
        fetch('/api/chats/' + encodeURIComponent(id))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || data.error) return;
                // Full life of the chat: numbered request/outcome pairs
                // (exchanges.json). Legacy sessions saved before exchanges
                // existed fall back to the old single last-done-message note.
                var exchanges = Array.isArray(data.exchanges) ? data.exchanges : [];
                if (exchanges.length && window.showAgentHistory) {
                    window.showAgentHistory(exchanges);
                } else if (window.showAgentNotes) {
                    var msg = data.last_done_message || '';
                    // showAgentNotes expects a JSON string of an array of strings; a
                    // single-element array shows the last done message as note "1.".
                    window.showAgentNotes(JSON.stringify(msg ? [msg] : []));
                }
                // Restore the memory bar to this chat's last context size + cap and
                // show it (the bar hides only for a brand-new chat).
                if (window.updateMemoryBar) window.updateMemoryBar(data.context_tokens || 0, data.context_cap || 300000);
                if (window.showMemoryBar) window.showMemoryBar();
                // Per-chat mode lock: the chat follows the agent that ran it.
                // ios_use -> Mobile use with the device tick CLEARED (the phone
                // session is gone — the user re-picks iOS/Android to re-pair);
                // any desktop pkg -> Computer use; untagged (never ran) -> free.
                var pkg = data.run_pkg || '';
                var lockMode = pkg === 'ios_use' ? 'mobile' : (pkg ? 'computer' : null);
                document.dispatchEvent(new CustomEvent('agentmode:lock', { detail: { mode: lockMode } }));
                if (lockMode) {
                    document.dispatchEvent(new CustomEvent('agentmode:set', {
                        detail: lockMode === 'mobile' ? { mode: 'mobile', sub: null } : { mode: 'computer' }
                    }));
                }
            })
            .catch(function () { /* non-fatal */ });
    }

    function deleteChat(id, list) {
        fetch('/api/chats/' + encodeURIComponent(id), { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function () {
                // If we deleted the open chat, fall back to a fresh-start state.
                if (window.currentSessionId === id) {
                    window.currentSessionId = null;
                    if (window.resetChatUi) window.resetChatUi();
                }
                loadChats(list);
            })
            .catch(function () { /* non-fatal */ });
    }

    // Download a session's saved conversation (the agent's exact optimized
    // memory) to the user's Downloads folder — a debug aid to inspect what's
    // actually stored. The backend writes the file; we toast the saved path.
    function downloadChat(id) {
        fetch('/api/chats/' + encodeURIComponent(id) + '/download', { method: 'POST' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.path) {
                    if (window.showToast) window.showToast('Saved in Downloads');
                } else if (window.showToast) {
                    window.showToast((data && data.error) || 'Nothing saved for this chat yet');
                }
            })
            .catch(function () { if (window.showToast) window.showToast('Download failed'); });
    }

    // Per-chat "⋯" menu (Download / Delete). Appended to <body> and fixed-
    // positioned next to the kebab so the left bar's overflow can't clip it.
    var openMenuEl = null;
    function closeItemMenu() {
        if (!openMenuEl) return;
        openMenuEl.remove();
        openMenuEl = null;
        document.removeEventListener('click', closeItemMenu, true);
        document.removeEventListener('keydown', onMenuKey, true);
    }
    function onMenuKey(e) { if (e.key === 'Escape') closeItemMenu(); }
    function openItemMenu(id, anchor, list) {
        closeItemMenu();
        var menu = document.createElement('div');
        menu.className = 'chat-item-menu';

        var dl = document.createElement('button');
        dl.type = 'button';
        dl.className = 'chat-item-menu-action';
        dl.textContent = 'Download conversation';
        dl.addEventListener('click', function (e) {
            e.stopPropagation(); closeItemMenu(); downloadChat(id);
        });

        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'chat-item-menu-action danger';
        del.textContent = 'Delete';
        del.addEventListener('click', function (e) {
            e.stopPropagation(); closeItemMenu(); deleteChat(id, list);
        });

        menu.appendChild(dl);
        menu.appendChild(del);
        document.body.appendChild(menu);

        var r = anchor.getBoundingClientRect();
        var w = menu.offsetWidth || 190;
        menu.style.top = (r.bottom + 4) + 'px';
        menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';

        openMenuEl = menu;
        // Defer so the click that opened the menu doesn't immediately close it.
        setTimeout(function () {
            document.addEventListener('click', closeItemMenu, true);
            document.addEventListener('keydown', onMenuKey, true);
        }, 0);
    }

    function startNewChat() {
        window.currentSessionId = null;          // fresh start: no memory loaded
        // A fresh chat has no mode history — unlock the mode picker.
        document.dispatchEvent(new CustomEvent('agentmode:lock', { detail: { mode: null } }));
        if (window.resetChatUi) window.resetChatUi();
        if (window.resetMemoryBar) window.resetMemoryBar();     // empty the memory bar
        if (window.hideMemoryBar) window.hideMemoryBar();       // hide it (only new chat hides)
        if (window.showWelcomeHero) window.showWelcomeHero();   // bring back the hero
        var input = document.querySelector('.chat-input');
        if (input) { input.disabled = false; input.focus(); }
        // Announce the fresh start (the notes stage hides itself on this).
        document.dispatchEvent(new CustomEvent('chat:new'));
        document.dispatchEvent(new CustomEvent('chats:refresh'));   // clears highlight
    }

    function mount(bar) {
        if (!bar) return;
        var body = bar.querySelector('.left-bar-body');
        if (!body || body.querySelector('.new-chat-btn')) return; // already mounted
        fetch('chat/chat.html')
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (body.querySelector('.new-chat-btn')) return; // guard race
                var holder = document.createElement('div');
                holder.innerHTML = html.trim();
                var btn = holder.querySelector('.new-chat-btn');
                if (!btn) return;
                body.insertBefore(btn, body.firstChild); // top of the body
                btn.addEventListener('click', startNewChat);

                // History list lives directly under the New-chat button.
                var list = body.querySelector('.chat-history-list');
                if (!list) {
                    list = document.createElement('div');
                    list.className = 'chat-history-list';
                    body.insertBefore(list, btn.nextSibling);
                }
                loadChats(list);
            })
            .catch(function () { /* non-fatal: the button just won't render */ });
    }

    // Reload the list whenever a run ends or a new chat is started.
    document.addEventListener('chats:refresh', function () {
        var list = document.querySelector('.chat-history-list');
        if (list) loadChats(list);
    });

    // Mount once the left bar exists: immediately if already injected, otherwise
    // when left_bar.js fires 'leftbar:ready'.
    var existing = document.getElementById('leftBar');
    if (existing) mount(existing);
    document.addEventListener('leftbar:ready', function (e) {
        mount((e && e.detail && e.detail.bar) || document.getElementById('leftBar'));
    });
})();
