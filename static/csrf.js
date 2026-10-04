(function () {
  'use strict';
  function token() {
    return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
  }
  function sameOrigin(url) {
    try { return new URL(url, window.location.href).origin === window.location.origin; }
    catch (_) { return true; }
  }
  const originalFetch = window.fetch;
  if (originalFetch && !window.__nfhCsrfFetchWrapped) {
    window.__nfhCsrfFetchWrapped = true;
    window.fetch = function (input, init) {
      const options = Object.assign({}, init || {});
      const method = String(options.method || (input && input.method) || 'GET').toUpperCase();
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (!['GET','HEAD','OPTIONS','TRACE'].includes(method) && sameOrigin(url)) {
        const headers = new Headers(options.headers || (input && input.headers) || {});
        const csrf = token();
        if (csrf) headers.set('X-CSRF-Token', csrf);
        options.headers = headers;
      }
      return originalFetch.call(this, input, options);
    };
  }
  function injectIntoForms(root) {
    const csrf = token();
    if (!csrf) return;
    (root || document).querySelectorAll('form').forEach(form => {
      const method = String(form.getAttribute('method') || 'GET').toUpperCase();
      if (!['POST','PUT','PATCH','DELETE'].includes(method)) return;
      if (form.querySelector('input[name="_csrf_token"]')) return;
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = '_csrf_token';
      input.value = csrf;
      form.appendChild(input);
    });
  }
  function init() {
    injectIntoForms(document);
    const observer = new MutationObserver(mutations => {
      for (const mutation of mutations) {
        mutation.addedNodes.forEach(node => {
          if (node.nodeType === 1) injectIntoForms(node);
        });
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once:true});
  else init();
})();
