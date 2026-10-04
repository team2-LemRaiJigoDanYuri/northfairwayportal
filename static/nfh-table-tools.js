(() => {
  'use strict';

  /*
   * Shared NFH-HOA record finder for Officer and Administrator portals.
   * It enhances existing server-rendered tables only; it never changes role
   * permissions or fetches records the signed-in user was not already given.
   */

  const norm = value => String(value ?? '').normalize('NFD').replace(/\p{Diacritic}/gu, '').toLowerCase().replace(/\s+/g, ' ').trim();
  const esc = value => String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const FILTER_HEADER_RULES = [
    { label: 'Status', test: h => (h.includes('status') || h === 'decision') && !h.includes('payment') && !h.includes('dues') },
    { label: 'Payment', test: h => h.includes('payment') },
    { label: 'Dues', test: h => h.includes('dues') },
    { label: 'Type', test: h => h === 'type' || h.includes('request type') || h.includes('service') },
    { label: 'Category', test: h => h.includes('category') },
    { label: 'Role', test: h => h === 'role' || h.includes('role / position') },
    { label: 'Reviewer', test: h => h.includes('reviewer') },
    { label: 'Channel', test: h => h.includes('channel') },
    { label: 'Submission', test: h => h.includes('submission') },
    { label: 'Active', test: h => h === 'active' },
  ];

  function placeholderRow(row) {
    if (!row) return false;
    if (row.classList.contains('record-filter-empty')) return true;
    return row.cells.length === 1 && row.cells[0].hasAttribute('colspan');
  }

  function getRows(table) {
    const body = table.tBodies[0];
    if (!body) return [];
    return Array.from(body.rows).filter(row => !placeholderRow(row));
  }

  function getHeaders(table) {
    const cells = Array.from(table.tHead?.rows?.[0]?.cells || []);
    return {
      cells,
      normalized: cells.map(cell => norm(cell.textContent)),
    };
  }

  function uniqueValues(rows, index) {
    if (index < 0) return [];
    const values = rows
      .map(row => (row.cells[index]?.textContent || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
    return [...new Set(values)].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
  }

  function resolveTitle(table) {
    const card = table.closest('.content-card, .review-queue-card, .dashboard-section, section');
    return card?.querySelector('.card-header h2, .release-title-row h2, .section-header h2, h2, h3')?.textContent?.trim() || 'records';
  }

  function buildSelect(label, values, index) {
    if (values.length < 2 || values.length > 40) return null;
    const field = document.createElement('label');
    field.className = 'record-filter-field';
    field.dataset.columnIndex = String(index);

    const caption = document.createElement('span');
    caption.textContent = label;

    const select = document.createElement('select');
    select.setAttribute('aria-label', `Filter by ${label}`);
    select.innerHTML = `<option value="">All ${esc(label)}</option>` +
      values.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');

    field.append(caption, select);
    return { field, select, index, label };
  }

  function selectFilterColumns(headers, rows) {
    const selected = [];
    const used = new Set();

    FILTER_HEADER_RULES.forEach(rule => {
      if (selected.length >= 4) return;
      const index = headers.findIndex((header, i) => !used.has(i) && rule.test(header));
      if (index < 0) return;
      const values = uniqueValues(rows, index);
      const tool = buildSelect(rule.label, values, index);
      if (tool) {
        selected.push(tool);
        used.add(index);
      }
    });

    return selected;
  }

  function createPanel(table, rowCount) {
    const panel = document.createElement('div');
    panel.className = 'record-filter-panel';
    panel.dataset.tableFilterFor = table.id || '';
    panel.setAttribute('aria-label', `Search and filter ${resolveTitle(table)}`);

    const head = document.createElement('div');
    head.className = 'record-filter-header';
    head.innerHTML = `
      <div class="record-filter-title">
        <span class="record-filter-icon" aria-hidden="true"><i class="fa-solid fa-magnifying-glass"></i></span>
        <div><strong>Find Records</strong><small>Search and narrow the list below</small></div>
      </div>
      <span class="record-filter-summary" aria-live="polite">${rowCount} record${rowCount === 1 ? '' : 's'}</span>`;

    const controls = document.createElement('div');
    controls.className = 'record-filter-controls';

    const searchWrap = document.createElement('label');
    searchWrap.className = 'record-filter-search';
    searchWrap.innerHTML = '<span>Search</span><div class="record-search-input"><i class="fa-solid fa-magnifying-glass" aria-hidden="true"></i></div>';
    const searchInput = document.createElement('input');
    searchInput.type = 'search';
    searchInput.autocomplete = 'off';
    searchInput.placeholder = 'Name, request ID, block, lot, type…';
    searchInput.setAttribute('aria-label', 'Search records');
    searchWrap.querySelector('.record-search-input').appendChild(searchInput);
    controls.appendChild(searchWrap);

    const footer = document.createElement('div');
    footer.className = 'record-filter-footer';

    panel.append(head, controls, footer);
    return { panel, controls, footer, searchInput, summary: head.querySelector('.record-filter-summary') };
  }

  function enhanceTable(table) {
    if (!table || table.dataset.recordFiltersReady === '1' || table.dataset.noRecordFilters === '1') return;
    if (!table.tHead || !table.tBodies.length) return;

    const responsive = table.closest('.table-responsive');
    if (!responsive?.parentNode) return;

    const rows = getRows(table);
    const { cells: headerCells, normalized: headers } = getHeaders(table);
    if (!headerCells.length) return;

    table.dataset.recordFiltersReady = '1';

    const ui = createPanel(table, rows.length);
    responsive.parentNode.insertBefore(ui.panel, responsive);

    const filterTools = selectFilterColumns(headers, rows);
    filterTools.forEach(tool => ui.controls.appendChild(tool.field));

    const sortField = document.createElement('label');
    sortField.className = 'record-filter-field';
    sortField.innerHTML = '<span>Sort by</span>';
    const sort = document.createElement('select');
    sort.setAttribute('aria-label', 'Sort records');
    sort.innerHTML = '<option value="">Default order</option>' + headers.map((header, index) => {
      if (!header || header.includes('action') || header === 'pdf' || header.includes('response')) return '';
      return `<option value="${index}">${esc(headerCells[index].textContent.trim())}</option>`;
    }).join('');
    sortField.appendChild(sort);
    ui.controls.appendChild(sortField);

    const directionField = document.createElement('label');
    directionField.className = 'record-filter-field record-sort-direction';
    directionField.innerHTML = '<span>Order</span>';
    const direction = document.createElement('select');
    direction.setAttribute('aria-label', 'Sort direction');
    direction.innerHTML = '<option value="asc">A–Z / Low–High</option><option value="desc">Z–A / High–Low</option>';
    directionField.appendChild(direction);
    ui.controls.appendChild(directionField);

    const reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'record-filter-clear';
    reset.innerHTML = '<i class="fa-solid fa-rotate-left" aria-hidden="true"></i><span>Reset</span>';
    ui.controls.appendChild(reset);

    const pageSizeField = document.createElement('label');
    pageSizeField.className = 'record-page-size-field';
    pageSizeField.innerHTML = '<span>Rows</span>';
    const pageSize = document.createElement('select');
    pageSize.className = 'record-filter-page-size';
    pageSize.setAttribute('aria-label', 'Rows per page');
    pageSize.innerHTML = '<option value="10">10</option><option value="25">25</option><option value="50">50</option><option value="0">All</option>';
    pageSizeField.appendChild(pageSize);

    const pager = document.createElement('div');
    pager.className = 'record-filter-pager';
    const previous = document.createElement('button');
    previous.type = 'button';
    previous.setAttribute('aria-label', 'Previous page');
    previous.innerHTML = '<i class="fa-solid fa-chevron-left" aria-hidden="true"></i><span>Previous</span>';
    const pageText = document.createElement('span');
    pageText.className = 'record-filter-page-text';
    const next = document.createElement('button');
    next.type = 'button';
    next.setAttribute('aria-label', 'Next page');
    next.innerHTML = '<span>Next</span><i class="fa-solid fa-chevron-right" aria-hidden="true"></i>';
    pager.append(previous, pageText, next);

    ui.footer.append(pageSizeField, pager);

    let originalOrder = rows.slice();
    let page = 1;
    let emptyRow = null;

    const findDateIndex = () => headers.findIndex(h => h.includes('submitted') || h.includes('updated') || h.includes('date') || h.includes('time') || h.includes('sent'));
    const dateIdx = findDateIndex();

    function removeEmptyRow() {
      if (emptyRow?.isConnected) emptyRow.remove();
      emptyRow = null;
    }

    function showEmptyRow() {
      removeEmptyRow();
      emptyRow = document.createElement('tr');
      emptyRow.className = 'record-filter-empty';
      const cell = document.createElement('td');
      cell.colSpan = Math.max(1, headerCells.length);
      cell.innerHTML = '<div class="record-empty-message"><i class="fa-regular fa-folder-open" aria-hidden="true"></i><span><strong>No matching records.</strong><small>Try changing the search or filters.</small></span></div>';
      emptyRow.appendChild(cell);
      table.tBodies[0].appendChild(emptyRow);
    }

    function compareValues(av, bv, index) {
      if (index === dateIdx) {
        const at = Date.parse(av.replace(' ', 'T'));
        const bt = Date.parse(bv.replace(' ', 'T'));
        if (Number.isFinite(at) && Number.isFinite(bt)) return at - bt;
      }
      const cleanA = av.replace(/[^0-9.-]/g, '');
      const cleanB = bv.replace(/[^0-9.-]/g, '');
      const an = Number(cleanA);
      const bn = Number(cleanB);
      if (/\d/.test(av) && /\d/.test(bv) && cleanA && cleanB && Number.isFinite(an) && Number.isFinite(bn)) return an - bn;
      return av.localeCompare(bv, undefined, { numeric: true, sensitivity: 'base' });
    }

    function apply() {
      removeEmptyRow();
      const terms = norm(ui.searchInput.value).split(' ').filter(Boolean);

      let filtered = originalOrder.filter(row => {
        const haystack = norm(`${row.textContent} ${row.dataset.searchExtra || ''}`);
        if (terms.length && !terms.every(term => haystack.includes(term))) return false;
        return filterTools.every(tool => !tool.select.value || norm(row.cells[tool.index]?.textContent) === norm(tool.select.value));
      });

      const sortIdx = sort.value === '' ? -1 : Number(sort.value);
      if (sortIdx >= 0) {
        filtered = filtered.slice().sort((a, b) => {
          const av = (a.cells[sortIdx]?.textContent || '').replace(/\s+/g, ' ').trim();
          const bv = (b.cells[sortIdx]?.textContent || '').replace(/\s+/g, ' ').trim();
          const value = compareValues(av, bv, sortIdx);
          return direction.value === 'desc' ? -value : value;
        });
      }

      originalOrder.forEach(row => table.tBodies[0].appendChild(row));
      if (sortIdx >= 0) filtered.forEach(row => table.tBodies[0].appendChild(row));

      const size = Number(pageSize.value);
      const totalPages = size ? Math.max(1, Math.ceil(filtered.length / size)) : 1;
      page = Math.max(1, Math.min(page, totalPages));
      const start = size ? (page - 1) * size : 0;
      const end = size ? Math.min(start + size, filtered.length) : filtered.length;
      const visible = new Set(filtered.slice(start, end));

      originalOrder.forEach(row => { row.hidden = !visible.has(row); });

      if (!filtered.length) showEmptyRow();

      const shownStart = filtered.length ? start + 1 : 0;
      const shownEnd = filtered.length ? end : 0;
      ui.summary.textContent = filtered.length === originalOrder.length
        ? `${filtered.length} record${filtered.length === 1 ? '' : 's'}`
        : `${filtered.length} of ${originalOrder.length} records`;

      pageText.textContent = size && filtered.length ? `${shownStart}–${shownEnd} of ${filtered.length}` : `${filtered.length} total`;
      previous.disabled = page <= 1;
      next.disabled = page >= totalPages;
      pager.hidden = !size || filtered.length <= size;
      directionField.hidden = sortIdx < 0;
    }

    ui.searchInput.addEventListener('input', () => { page = 1; apply(); });
    filterTools.forEach(tool => tool.select.addEventListener('change', () => { page = 1; apply(); }));
    sort.addEventListener('change', () => { page = 1; apply(); });
    direction.addEventListener('change', () => { page = 1; apply(); });
    pageSize.addEventListener('change', () => { page = 1; apply(); });
    reset.addEventListener('click', () => {
      ui.searchInput.value = '';
      filterTools.forEach(tool => { tool.select.value = ''; });
      sort.value = '';
      direction.value = 'asc';
      pageSize.value = '10';
      page = 1;
      apply();
      ui.searchInput.focus();
    });
    previous.addEventListener('click', () => {
      if (page > 1) {
        page -= 1;
        apply();
        ui.panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    });
    next.addEventListener('click', () => {
      page += 1;
      apply();
      ui.panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });

    if (!rows.length) {
      ui.searchInput.disabled = true;
      sort.disabled = true;
      direction.disabled = true;
      pageSize.disabled = true;
      reset.disabled = true;
      ui.summary.textContent = '0 records';
      pageText.textContent = '0 total';
      pager.hidden = true;
    } else {
      apply();
    }

    table._nfhRecordFilterRefresh = () => {
      const fresh = getRows(table);
      originalOrder = fresh.slice();
      ui.searchInput.disabled = !fresh.length;
      sort.disabled = !fresh.length;
      direction.disabled = !fresh.length;
      pageSize.disabled = !fresh.length;
      reset.disabled = !fresh.length;
      page = 1;
      apply();
    };
  }

  function init(root = document) {
    root.querySelectorAll('.data-table').forEach(enhanceTable);
  }

  function refresh() {
    document.querySelectorAll('.data-table').forEach(table => {
      if (typeof table._nfhRecordFilterRefresh === 'function') table._nfhRecordFilterRefresh();
      else enhanceTable(table);
    });
  }

  window.NFHRecordFilters = { init, refresh };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => init());
  else init();

  // Some role pages reveal tables through tabs. Re-run enhancement after a tab is opened.
  document.addEventListener('click', event => {
    if (event.target.closest('[data-tab], [data-tab-jump]')) setTimeout(() => init(), 0);
  });
  window.addEventListener('pageshow', () => init());
})();
