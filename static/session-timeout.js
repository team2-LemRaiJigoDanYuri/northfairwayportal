(() => {
    'use strict';

    const modal = document.getElementById('sessionTimeoutModal');
    if (!modal) return;

    const timeoutMs = Number(modal.dataset.timeoutSeconds || 1800) * 1000;
    const warningMs = Number(modal.dataset.warningSeconds || 300) * 1000;
    const userKey = modal.dataset.userKey || 'authenticated';
    const storageKey = `nfh:session:lastActivity:${userKey}`;
    const syncKey = `nfh:session:sync:${userKey}`;
    const countdown = document.getElementById('sessionTimeoutCountdown');
    const stayBtn = document.getElementById('sessionStaySignedIn');
    const signOutBtn = document.getElementById('sessionSignOutNow');
    const originalFetch = window.fetch.bind(window);

    let lastActivity = Date.now();
    let lastBackendSync = Date.now(); // page navigation itself already refreshed server activity
    let lastLocalWrite = 0;
    let warningVisible = false;
    let warningTrigger = null;
    let expiring = false;

    const safeStoredActivity = () => {
        const value = Number(localStorage.getItem(storageKey));
        return Number.isFinite(value) && value > 0 ? value : 0;
    };

    const writeActivity = (timestamp) => {
        lastActivity = timestamp;
        // Throttle localStorage churn from mousemove/scroll while still making
        // multi-tab activity sync responsive.
        if (timestamp - lastLocalWrite >= 1000) {
            lastLocalWrite = timestamp;
            try {
                localStorage.setItem(storageKey, String(timestamp));
                localStorage.setItem(syncKey, `${timestamp}:${Math.random()}`);
            } catch (_) {}
        }
    };

    const formatRemaining = (milliseconds) => {
        const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
    };

    const hideWarning = (restoreFocus = true) => {
        if (!warningVisible) return;
        warningVisible = false;
        modal.classList.remove('is-visible');
        modal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('session-timeout-open');
        if (restoreFocus && warningTrigger && typeof warningTrigger.focus === 'function') {
            setTimeout(() => warningTrigger.focus(), 0);
        }
    };

    const showWarning = () => {
        if (warningVisible || expiring) return;
        warningVisible = true;
        warningTrigger = document.activeElement;
        modal.classList.add('is-visible');
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('session-timeout-open');
        setTimeout(() => stayBtn?.focus(), 0);
    };

    const redirectExpired = () => {
        window.location.replace('/register?session=expired');
    };

    const forceSessionExpired = async () => {
        if (expiring) return;
        expiring = true;
        hideWarning(false);
        try { await originalFetch('/api/logout', { method: 'POST', headers: { 'Accept': 'application/json' }, cache: 'no-store' }); } catch (_) {}
        try { localStorage.removeItem(storageKey); localStorage.removeItem(syncKey); } catch (_) {}
        redirectExpired();
    };

    const syncBackend = async (force = false) => {
        const now = Date.now();
        if (!force && now - lastBackendSync < 60000) return true;
        lastBackendSync = now;
        try {
            const response = await originalFetch('/api/session/keepalive', {
                method: 'POST',
                headers: { 'Accept': 'application/json', 'X-NFH-Real-Activity': '1' },
                cache: 'no-store'
            });
            if (response.status === 401 || response.headers.get('X-Session-Expired') === '1') {
                forceSessionExpired();
                return false;
            }
            return response.ok;
        } catch (_) {
            // A transient network error should not falsely log the user out;
            // the server remains authoritative on the next successful request.
            return false;
        }
    };

    const registerActivity = () => {
        if (warningVisible || expiring) return;
        const now = Date.now();
        writeActivity(now);
        syncBackend(false);
    };

    // Capture genuine browser interaction. Passive listeners avoid impacting
    // scroll/touch performance, and backend keepalive is throttled to 1/minute.
    ['pointerdown', 'mousemove', 'keydown', 'touchstart', 'scroll'].forEach((eventName) => {
        window.addEventListener(eventName, registerActivity, { passive: true, capture: true });
    });

    stayBtn?.addEventListener('click', async () => {
        if (expiring) return;
        stayBtn.disabled = true;
        const ok = await syncBackend(true);
        if (ok) {
            const now = Date.now();
            writeActivity(now);
            hideWarning(true);
        }
        stayBtn.disabled = false;
    });

    signOutBtn?.addEventListener('click', async () => {
        if (expiring) return;
        expiring = true;
        signOutBtn.disabled = true;
        try { await originalFetch('/api/logout', { method: 'POST', headers: { 'Accept': 'application/json' }, cache: 'no-store' }); } catch (_) {}
        try { localStorage.removeItem(storageKey); localStorage.removeItem(syncKey); } catch (_) {}
        window.location.replace('/register');
    });

    // Do not let Escape or backdrop clicks dismiss the security warning.
    document.addEventListener('keydown', (event) => {
        if (warningVisible && event.key === 'Escape') {
            event.preventDefault();
            event.stopImmediatePropagation();
            stayBtn?.focus();
        }
    }, true);
    modal.addEventListener('click', (event) => {
        if (event.target === modal) stayBtn?.focus();
    });

    // Synchronize actual activity across tabs for the same authenticated user.
    window.addEventListener('storage', (event) => {
        if (event.key !== storageKey && event.key !== syncKey) return;
        const remote = safeStoredActivity();
        if (remote > lastActivity) {
            lastActivity = remote;
            lastBackendSync = Math.max(lastBackendSync, remote);
            if (warningVisible) hideWarning(false);
        }
    });

    // Detect server-enforced expiry on any existing AJAX request without
    // consuming or altering the response body used by the original caller.
    window.fetch = async (...args) => {
        const response = await originalFetch(...args);
        if (response.status === 401 && response.headers.get('X-Session-Expired') === '1') {
            forceSessionExpired();
        }
        return response;
    };

    // Opening/refreshing an authenticated page is genuine activity and the
    // Flask before_request hook already refreshed the server timestamp.
    writeActivity(Date.now());

    const checkInactivity = () => {
        if (expiring) return;
        const now = Date.now();
        const elapsed = now - lastActivity;
        const remaining = timeoutMs - elapsed;

        if (remaining <= 0) {
            if (countdown) countdown.textContent = '00:00';
            forceSessionExpired();
            return;
        }

        if (elapsed >= timeoutMs - warningMs) {
            showWarning();
            if (countdown) countdown.textContent = formatRemaining(remaining);
        } else if (warningVisible) {
            hideWarning(false);
        }
    };

    checkInactivity();
    window.setInterval(checkInactivity, 1000);
})();
