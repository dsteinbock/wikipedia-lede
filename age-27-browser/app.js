(() => {
  'use strict';

  const data = window.AGE27_DATA;
  const totalPeople = data.people.length;
  const musicianIds = new Set(data.musicians.map((row) => row.q));
  const firstYear = (value) => {
    const match = String(value).match(/^-?\d{1,6}/);
    return match ? Number(match[0]) : 0;
  };
  const deathSortValue = (value) => {
    const match = String(value).match(/^(-?\d{1,6})(?:-(\d{2}))?(?:-(\d{2}))?/);
    if (!match) return 0;
    const year = Number(match[1]);
    const month = Number(match[2] || 0);
    const day = Number(match[3] || 0);
    return year * 372 + month * 31 + day;
  };
  const fullYears = data.people.map((row) => firstYear(row.d));
  const FULL_YEAR_MIN = Math.min(...fullYears);
  const FULL_YEAR_MAX = Math.max(...fullYears);
  const state = { search: '', status: 'all', sort: 'death-asc', shown: 24, zoomStart: FULL_YEAR_MIN, zoomEnd: FULL_YEAR_MAX, cloudMode: 'drift', selectedOccupations: new Set() };
  let filtered = [];
  let timelineRows = [];
  let occupationInventory = [];
  let allOccupationNames = [];

  const $ = (selector) => document.querySelector(selector);
  const fmt = new Intl.NumberFormat('en-US');
  const elements = {
    search: $('#search'), status: $('#status-filter'), yearMin: $('#year-min'), yearMax: $('#year-max'),
    occupationButton: $('#occupation-filter-button'), occupationSummary: $('#occupation-filter-summary'),
    occupationPanel: $('#occupation-filter-panel'), occupationGroups: $('#occupation-groups'),
    sort: $('#sort-order'), count: $('#result-count'), context: $('#result-context'), list: $('#record-list'),
    sentinel: $('#scroll-sentinel'), empty: $('#empty-state'), bars: $('#occupation-bars'),
    timeline: $('#timeline'), timelineLegend: $('#timeline-legend'), tooltip: $('#timeline-tooltip'),
    zoomStart: $('#zoom-start'), zoomEnd: $('#zoom-end'), zoomSelection: $('#zoom-selection'),
    zoomReadout: $('#zoom-readout'), zoomCount: $('#zoom-count'),
    cloudModeNote: $('#cloud-mode-note'),
    dialog: $('#detail-dialog'), dialogContent: $('#dialog-content')
  };

  function sourceRows() { return data.people; }
  function primaryOccupation(row) { return row.o[0] || 'unclassified'; }

  function occupationColor(name) {
    let hash = 2166136261;
    for (const character of name) { hash ^= character.codePointAt(0); hash = Math.imul(hash, 16777619); }
    return `hsl(${Math.abs(hash) % 360} 76% 64%)`;
  }

  function stableUnit(value) {
    let hash = 2166136261;
    for (const character of value) { hash ^= character.codePointAt(0); hash = Math.imul(hash, 16777619); }
    return (hash >>> 0) / 4294967295;
  }

  const CATEGORY_ORDER = [
    'Music', 'Stage & screen', 'Writing & visual arts', 'Sports', 'Science & medicine',
    'Politics & royalty', 'Military & conflict', 'Religion & scholarship', 'Crime & resistance',
    'Trades & public life', 'Other', 'Unclassified'
  ];

  function occupationCategory(occupation) {
    const value = occupation.toLocaleLowerCase();
    if (value === 'unclassified') return 'Unclassified';
    if (/singer|music|composer|rapper|pian|guitar|drumm|violin|songwrit|lyric|vocal|saxoph|record producer|bandleader|harpsichord|organist|bassist|cellist|clarinet|percussion|conductor/.test(value)) return 'Music';
    if (/actor|film|television|theatre|theater|dancer|model|comedian|presenter|animator|cinematograph|drag queen|influencer|youtuber|tiktoker|vlogger|director/.test(value)) return 'Stage & screen';
    if (/writer|poet|journal|artist|paint|photograph|sculpt|illustrat|design|editor|playwright|author|novelist|engraver|printmaker|cartoon|graffiti|calligraph|literary|translator/.test(value)) return 'Writing & visual arts';
    if (/player|athlet|sport|racing|boxer|wrestl|swimm|ski|cycl|rower|jockey|gymnast|runner|football|cricket|tennis|basketball|hockey|rugby|martial|fencer|archer|surfer|judoka|biath|weightlift|golfer|mountain|climb|skater|coach|umpire/.test(value)) return 'Sports';
    if (/scient|physic|chemist|biolog|botan|zoolog|entomolog|ornitholog|physician|doctor|nurse|surgeon|pharmac|psycholog|engineer|mathematic|astronom|geolog|naturalist|computer|data scientist|inventor|architect|medical/.test(value)) return 'Science & medicine';
    if (/politic|monarch|king|queen|emperor|empress|ruler|sovereign|royal|aristocrat|diplomat|governor|prince|duke|courtier|regent|official|legislative/.test(value)) return 'Politics & royalty';
    if (/military|soldier|army|naval|warrior|commander|air force|fighter pilot|commando|partisan|revolution|resistance|airman|aviator|war chief/.test(value)) return 'Military & conflict';
    if (/priest|religious|theolog|monk|nun|bishop|cleric|imam|rabbi|minister|missionary|scholar|teacher|professor|academic|student|philosoph|historian|linguist|jurist|pedagogue/.test(value)) return 'Religion & scholarship';
    if (/killer|criminal|murder|robber|terror|pirate|thief|gangster|bandit|assassin|spy|agent|rapist|tortur|prostitut|sex worker|smuggl|rebel|militant/.test(value)) return 'Crime & resistance';
    if (/business|merchant|farmer|worker|labor|driver|pilot|sailor|police|firefighter|cook|butcher|carpenter|tailor|servant|entrepreneur|accountant|publisher|trader|miner|officer/.test(value)) return 'Trades & public life';
    return 'Other';
  }

  function categoryBands(rows, top, bottom) {
    const counts = new Map(CATEGORY_ORDER.map((category) => [category, 0]));
    rows.forEach((row) => {
      const category = occupationCategory(primaryOccupation(row));
      counts.set(category, counts.get(category) + 1);
    });
    const present = CATEGORY_ORDER.filter((category) => counts.get(category) > 0);
    const total = Math.max(1, rows.length);
    const available = bottom - top;
    const floor = Math.min(22, available / Math.max(1, present.length) * .45);
    const flexible = Math.max(0, available - floor * present.length);
    const bands = new Map();
    let cursor = top;
    present.forEach((category, index) => {
      const height = index === present.length - 1 ? bottom - cursor : floor + flexible * counts.get(category) / total;
      bands.set(category, { top: cursor, height, count: counts.get(category) });
      cursor += height;
    });
    return bands;
  }

  function buildOccupationInventory() {
    const counts = new Map();
    sourceRows().forEach((row) => {
      const occupations = row.o.length ? row.o : ['unclassified'];
      occupations.forEach((occupation) => counts.set(occupation, (counts.get(occupation) || 0) + 1));
    });
    occupationInventory = CATEGORY_ORDER.map((category) => ({
      category,
      occupations: [...counts.entries()]
        .filter(([occupation]) => occupationCategory(occupation) === category)
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .map(([name, count]) => ({ name, count }))
    })).filter((group) => group.occupations.length);
    allOccupationNames = occupationInventory.flatMap((group) => group.occupations.map((occupation) => occupation.name));
    state.selectedOccupations = new Set(allOccupationNames);
  }

  function occupationSelectionLabel() {
    const selected = state.selectedOccupations.size;
    if (selected === allOccupationNames.length) return 'all occupations';
    if (selected === 0) return 'no occupations';
    return `${fmt.format(selected)} occupations`;
  }

  function renderOccupationPanel() {
    elements.occupationGroups.innerHTML = occupationInventory.map((group) => `
      <section class="occupation-group" data-category="${escapeAttr(group.category)}">
        <label class="occupation-group-toggle">
          <input type="checkbox" data-group="${escapeAttr(group.category)}">
          <span><strong>${escapeHtml(group.category)}</strong><b>${fmt.format(group.occupations.reduce((sum, occupation) => sum + occupation.count, 0))}</b></span>
        </label>
        <div class="occupation-items">
          ${group.occupations.map((occupation) => `
            <label class="occupation-choice">
              <input type="checkbox" data-occupation-choice="${escapeAttr(occupation.name)}">
              <span><i></i>${escapeHtml(occupation.name)}<b>${fmt.format(occupation.count)}</b></span>
            </label>`).join('')}
        </div>
      </section>`).join('');
    syncOccupationControls();
  }

  function syncOccupationControls() {
    elements.occupationGroups.querySelectorAll('[data-occupation-choice]').forEach((input) => {
      input.checked = state.selectedOccupations.has(input.dataset.occupationChoice);
    });
    occupationInventory.forEach((group) => {
      const input = elements.occupationGroups.querySelector(`[data-group="${CSS.escape(group.category)}"]`);
      const selected = group.occupations.filter((occupation) => state.selectedOccupations.has(occupation.name)).length;
      input.checked = selected === group.occupations.length;
      input.indeterminate = selected > 0 && selected < group.occupations.length;
    });
    elements.occupationSummary.textContent = occupationSelectionLabel();
  }

  function setOccupationSelection(names) {
    state.selectedOccupations = new Set(names);
    state.shown = 24;
    syncOccupationControls();
    render();
  }

  function applyFilters() {
    const needle = state.search.trim().toLocaleLowerCase();
    filtered = sourceRows().filter((row) => {
      if (state.status !== 'all' && row.s !== state.status) return false;
      const year = firstYear(row.d);
      if (year < state.zoomStart || year > state.zoomEnd) return false;
      const occupations = row.o.length ? row.o : ['unclassified'];
      if (!occupations.some((occupation) => state.selectedOccupations.has(occupation))) return false;
      if (needle && !`${row.n} ${row.q} ${row.o.join(' ')} ${row.c.join(' ')} ${row.m.join(' ')}`.toLocaleLowerCase().includes(needle)) return false;
      return true;
    });
    sortRows();
  }

  function sortRows() {
    filtered.sort((a, b) => {
      if (state.sort === 'death-desc') return deathSortValue(b.d) - deathSortValue(a.d) || a.n.localeCompare(b.n);
      if (state.sort === 'name') return a.n.localeCompare(b.n);
      if (state.sort === 'certainty') return a.s.localeCompare(b.s) || deathSortValue(a.d) - deathSortValue(b.d) || a.n.localeCompare(b.n);
      return deathSortValue(a.d) - deathSortValue(b.d) || a.n.localeCompare(b.n);
    });
  }

  function render() {
    applyFilters();
    elements.count.textContent = fmt.format(filtered.length);
    elements.context.textContent = `${formatYear(state.zoomStart)}–${formatYear(state.zoomEnd)} · ${occupationSelectionLabel()}`;
    renderTimeline();
    renderOccupations();
    renderRecords();
  }

  function occupationCounts(rows) {
    const counts = new Map();
    rows.forEach((row) => row.o.forEach((occupation) => counts.set(occupation, (counts.get(occupation) || 0) + 1)));
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }

  function primaryOccupationCounts(rows) {
    const counts = new Map();
    rows.forEach((row) => {
      const occupation = primaryOccupation(row);
      counts.set(occupation, (counts.get(occupation) || 0) + 1);
    });
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }

  function selectOccupation(name) {
    setOccupationSelection([name]);
    $('.result-ribbon').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function renderOccupations() {
    const top = occupationCounts(filtered).slice(0, 10);
    const max = top[0]?.[1] || 1;
    elements.bars.innerHTML = top.length ? top.map(([name, count]) => `
      <button class="occupation-bar" type="button" data-occupation="${escapeAttr(name)}" aria-label="Filter to ${escapeAttr(name)}, ${count} records">
        <span>${escapeHtml(name)}</span><span class="occupation-track"><span style="width:${Math.max(3, count / max * 100)}%;background:${occupationColor(name)}"></span></span><span class="occupation-value">${fmt.format(count)}</span>
      </button>`).join('') : '<p>No occupations in this selection.</p>';
    elements.bars.querySelectorAll('[data-occupation]').forEach((button) => button.addEventListener('click', () => selectOccupation(button.dataset.occupation)));
  }

  function niceStep(span, targetTicks) {
    const rough = Math.max(1, span / targetTicks);
    const magnitude = 10 ** Math.floor(Math.log10(rough));
    const normalized = rough / magnitude;
    const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return nice * magnitude;
  }

  function formatYear(year) { return year < 0 ? `${Math.abs(year)} BCE` : String(year); }

  function setZoomWindow(start, end) {
    state.zoomStart = Math.max(FULL_YEAR_MIN, Math.min(start, FULL_YEAR_MAX - 1));
    state.zoomEnd = Math.min(FULL_YEAR_MAX, Math.max(end, state.zoomStart + 1));
    elements.zoomStart.value = state.zoomStart;
    elements.zoomEnd.value = state.zoomEnd;
    updateZoomControl();
  }

  function updateZoomControl() {
    const span = FULL_YEAR_MAX - FULL_YEAR_MIN;
    elements.zoomStart.value = state.zoomStart;
    elements.zoomEnd.value = state.zoomEnd;
    elements.yearMin.value = state.zoomStart;
    elements.yearMax.value = state.zoomEnd;
    const left = (state.zoomStart - FULL_YEAR_MIN) / span * 100;
    const right = (state.zoomEnd - FULL_YEAR_MIN) / span * 100;
    elements.zoomSelection.style.left = `${left}%`;
    elements.zoomSelection.style.width = `${right - left}%`;
    elements.zoomReadout.textContent = `${formatYear(state.zoomStart)} — ${formatYear(state.zoomEnd)}`;
    elements.zoomStart.setAttribute('aria-valuetext', formatYear(state.zoomStart));
    elements.zoomEnd.setAttribute('aria-valuetext', formatYear(state.zoomEnd));
  }

  function renderTimeline() {
    const svg = elements.timeline;
    const width = Math.max(620, svg.clientWidth || 1200);
    const height = 720;
    const margin = { top: 28, right: 18, bottom: 72, left: state.cloudMode === 'occupation' ? 126 : 18 };
    const baseline = height - margin.bottom;
    timelineRows = filtered.filter((row) => {
      const year = firstYear(row.d);
      return year >= state.zoomStart && year <= state.zoomEnd;
    }).sort((a, b) => firstYear(a.d) - firstYear(b.d));
    const minYear = state.zoomStart;
    const maxYear = state.zoomEnd;
    const span = Math.max(1, maxYear - minYear);
    const innerW = width - margin.left - margin.right;
    const xForYear = (year) => margin.left + (year - minYear) / span * innerW;
    const tickStep = niceStep(span, width < 760 ? 8 : 24);
    const tickStart = Math.ceil(minYear / tickStep) * tickStep;
    const ticks = [];
    for (let year = tickStart; year <= maxYear; year += tickStep) ticks.push(year);
    const minorStep = span <= 150 ? 1 : span <= 400 ? 2 : span <= 1200 ? 10 : 50;
    const minorTicks = [];
    for (let year = Math.ceil(minYear / minorStep) * minorStep; year <= maxYear; year += minorStep) minorTicks.push(year);

    const pointRadius = width < 760 ? 2.8 : 3.25;
    const bands = state.cloudMode === 'occupation' ? categoryBands(timelineRows, margin.top + 8, baseline - 12) : null;
    const points = timelineRows.map((row, index) => {
      const exactX = xForYear(firstYear(row.d));
      const verticalSpread = Math.pow(stableUnit(`${row.q}:cloud`), 1.65);
      const x = exactX;
      const occupation = primaryOccupation(row);
      const category = occupationCategory(occupation);
      const band = bands?.get(category);
      const y = band
        ? band.top + Math.min(5, band.height * .12) + stableUnit(`${row.q}:band`) * Math.max(1, band.height - Math.min(10, band.height * .24))
        : baseline - 12 - verticalSpread * (baseline - margin.top - 34);
      const color = occupationColor(occupation);
      return `<circle class="life-point ${row.s}" data-index="${index}" cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="${pointRadius}" fill="${row.s === 'possible' ? 'var(--paper)' : color}" stroke="${color}" tabindex="0" role="button" aria-label="${escapeAttr(row.n)}, died ${escapeAttr(row.d)}, ${escapeAttr(occupation)}"><title>${escapeHtml(row.n)} — ${escapeHtml(row.d)} — ${escapeHtml(occupation)}</title></circle>`;
    }).join('');

    const grid = ticks.map((year) => {
      const x = xForYear(year);
      return `<line x1="${x}" y1="${margin.top}" x2="${x}" y2="${baseline}" class="year-grid"/><line x1="${x}" y1="${baseline}" x2="${x}" y2="${baseline + 8}" class="year-tick"/><text x="${x}" y="${baseline + 28}" text-anchor="middle" class="year-label">${formatYear(year)}</text>`;
    }).join('');
    const minorGrid = minorTicks.filter((year) => year % tickStep !== 0).map((year) => {
      const x = xForYear(year);
      return `<line x1="${x}" y1="${baseline-7}" x2="${x}" y2="${baseline+4}" class="minor-year-tick"/>`;
    }).join('');
    const bandGuides = bands ? [...bands.entries()].map(([category, band], index) => {
      const center = band.top + band.height / 2;
      const line = index ? `<line x1="${margin.left-8}" y1="${band.top}" x2="${width-margin.right}" y2="${band.top}" class="category-guide"/>` : '';
      return `${line}<text x="8" y="${center-2}" class="category-label">${escapeHtml(category)}</text><text x="8" y="${center+11}" class="category-count">${fmt.format(band.count)}</text>`;
    }).join('') : '';
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.style.aspectRatio = `${width} / ${height}`;
    svg.innerHTML = `<g>${grid}${minorGrid}</g><g class="category-guides">${bandGuides}</g><line x1="${margin.left}" y1="${baseline}" x2="${width-margin.right}" y2="${baseline}" class="timeline-axis"/><text x="${margin.left}" y="${height-12}" class="axis-note">${fmt.format(timelineRows.length)} people · exact year horizontally · ${state.cloudMode === 'occupation' ? 'band thickness follows population' : 'vertical spread separates people'}</text><g class="point-cloud">${points}</g>`;

    const top = primaryOccupationCounts(filtered).slice(0, 8);
    elements.timelineLegend.innerHTML = top.map(([name, count]) => name === 'unclassified'
      ? `<span class="legend-item"><i style="background:${occupationColor(name)}"></i>${name} <b>${fmt.format(count)}</b></span>`
      : `<button type="button" data-occupation="${escapeAttr(name)}"><i style="background:${occupationColor(name)}"></i>${escapeHtml(name)} <span>${fmt.format(count)}</span></button>`
    ).join('') + `<span class="certainty-key"><i></i> hollow ring = possible · color = first listed occupation</span>`;
    elements.timelineLegend.querySelectorAll('button').forEach((button) => button.addEventListener('click', () => selectOccupation(button.dataset.occupation)));
    elements.zoomCount.textContent = `${fmt.format(timelineRows.length)} ${timelineRows.length === 1 ? 'point' : 'points'} in view`;
    elements.cloudModeNote.textContent = state.cloudMode === 'occupation'
      ? 'Broad career families sit together; populous ones get more breathing room.'
      : 'Vertical position is random, deterministic, and professionally meaningless.';
    updateZoomControl();
    $('#timeline-note').textContent = minorStep === 1 ? `Annual grid · labels every ${fmt.format(tickStep)} years · hover or click` : `${fmt.format(minorStep)}-year grid · choose an era for annual detail`;
  }

  function showPointTooltip(event, point) {
    const row = timelineRows[Number(point.dataset.index)];
    const occupation = primaryOccupation(row);
    const rect = elements.timeline.getBoundingClientRect();
    const pointRect = point.getBoundingClientRect();
    const x = event?.clientX ? event.clientX - rect.left : pointRect.left + pointRect.width / 2 - rect.left;
    const y = event?.clientY ? event.clientY - rect.top : pointRect.top - rect.top;
    elements.tooltip.innerHTML = `<span class="tooltip-dot" style="background:${occupationColor(occupation)}"></span><strong>${escapeHtml(row.n)}</strong><br>${escapeHtml(row.d)} · ${escapeHtml(occupation)}<br><em>${row.s} age 27</em>`;
    elements.tooltip.style.left = `${x}px`;
    elements.tooltip.style.top = `${y}px`;
    elements.tooltip.classList.add('visible');
  }

  function renderRecords() {
    const visible = filtered.slice(0, state.shown);
    const listValues = (values) => {
      const known = values.filter((value) => value && value.toLocaleLowerCase() !== 'unknown');
      return known.length ? known.map(escapeHtml).join(' · ') : 'Unknown';
    };
    elements.list.innerHTML = visible.map((row) => `
      <div class="record-row">
        <button class="record-card ${row.s}" type="button" data-detail-qid="${row.q}" aria-label="View details for ${escapeAttr(row.n)}">
          <span class="record-index"></span>
          <span class="record-name"><strong>${escapeHtml(row.n)}</strong><small>${row.q}${musicianIds.has(row.q) ? ' · musician' : ''}</small></span>
          <span class="record-lived"><small>Lived</small><br>${escapeHtml(row.b)} — ${escapeHtml(row.d)}</span>
          <span class="record-fact"><small>Cause of death</small><br>${listValues(row.c)}</span>
          <span class="record-fact"><small>Manner of death</small><br>${listValues(row.m)}</span>
          <span class="record-occupations">${row.o.slice(0, 2).map((occupation) => `<i class="occupation-tag" style="--occupation-color:${occupationColor(occupation)}">${escapeHtml(occupation)}</i>`).join('') || '<i class="occupation-tag">unclassified</i>'}</span>
          <span class="status-icon ${row.s}" role="img" aria-label="${row.s === 'confirmed' ? 'Confirmed age 27' : 'Possibly age 27'}" data-tooltip="${row.s === 'confirmed' ? 'Confirmed age 27' : 'Possibly age 27'}">${row.s === 'confirmed' ? '✓' : '≈'}</span>
        </button>
        <a class="record-wikipedia" href="${escapeAttr(row.u)}" target="_blank" rel="noopener" aria-label="Open ${escapeAttr(row.n)} on Wikipedia"><span aria-hidden="true">↗</span></a>
      </div>`).join('');
    elements.list.querySelectorAll('[data-detail-qid]').forEach((button) => button.addEventListener('click', () => openDetail(visible.find((row) => row.q === button.dataset.detailQid))));
    elements.empty.hidden = filtered.length !== 0;
    elements.sentinel.hidden = state.shown >= filtered.length || filtered.length === 0;
    elements.sentinel.querySelector('span').textContent = `${fmt.format(Math.min(state.shown, filtered.length))} of ${fmt.format(filtered.length)} names summoned`;
  }

  function openDetail(row) {
    if (!row) return;
    const isMusician = musicianIds.has(row.q);
    const hasKnownValues = (values) => values.some((value) => value && value.toLocaleLowerCase() !== 'unknown');
    const hasWikipediaFallback = (row.wc && hasKnownValues(row.c)) || (row.wm && hasKnownValues(row.m)) || (row.wo && hasKnownValues(row.o));
    const detailDate = (value) => value
      ? `${escapeHtml(value)}<sup title="Wikidata">1</sup>`
      : 'Unknown';
    const detailValues = (values, wikipediaDerived) => {
      const known = values.filter((value) => value && value.toLocaleLowerCase() !== 'unknown');
      if (!known.length) return 'Unknown';
      const sourceNumber = wikipediaDerived ? 2 : 1;
      const sourceTitle = wikipediaDerived ? 'English Wikipedia-derived fallback' : 'Wikidata';
      return `${known.map(escapeHtml).join(' · ')}<sup title="${sourceTitle}">${sourceNumber}</sup>`;
    };
    const wikidataUrl = `https://www.wikidata.org/wiki/${encodeURIComponent(row.q)}`;
    elements.dialogContent.innerHTML = `
      <div class="detail-hero"><p class="eyebrow">${row.q}${isMusician ? ' · musician collection' : ''}</p><h2>${escapeHtml(row.n)}</h2></div>
      <div class="detail-body">
        <div class="detail-dates"><div><span>Arrived</span><strong>${detailDate(row.b)}</strong></div><div><span>Departed early</span><strong>${detailDate(row.d)}</strong></div></div>
        <div class="detail-fact"><span>How sure are we?</span><strong class="status-tag ${row.s}">${row.s}</strong></div>
        <div class="detail-fact"><span>Possible age range</span><strong>${escapeHtml(row.r)}</strong></div>
        <div class="detail-fact"><span>Total runtime</span><strong>${fmt.format(row.lo)}–${fmt.format(row.hi)} days</strong></div>
        <div class="detail-fact"><span>Cause of death</span><strong>${detailValues(row.c, row.wc)}</strong></div>
        <div class="detail-fact"><span>Manner of death</span><strong>${detailValues(row.m, row.wm)}</strong></div>
        <div class="detail-fact"><span>Primary identity</span><strong>${detailValues(row.o, row.wo)}</strong></div>
        <p class="detail-source-note"><span><sup>1</sup> Structured data from Wikidata</span>${hasWikipediaFallback ? '<span><sup>2</sup> English Wikipedia-derived fallback</span>' : ''}</p>
        <div class="detail-links">
          <a class="detail-link wikidata" href="${escapeAttr(wikidataUrl)}" target="_blank" rel="noopener">Wikidata ↗</a>
          <a class="detail-link wikipedia" href="${escapeAttr(row.u)}" target="_blank" rel="noopener">Wikipedia ↗</a>
        </div>
      </div>`;
    elements.dialog.showModal();
  }

  function resetFilters() {
    Object.assign(state, { search: '', status: 'all', sort: 'death-asc', shown: 24, zoomStart: FULL_YEAR_MIN, zoomEnd: FULL_YEAR_MAX });
    state.selectedOccupations = new Set(allOccupationNames);
    elements.search.value = ''; elements.status.value = 'all'; elements.sort.value = 'death-asc';
    setZoomWindow(FULL_YEAR_MIN, FULL_YEAR_MAX);
    syncOccupationControls();
    render();
  }

  function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (character) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[character])); }
  function escapeAttr(value) { return escapeHtml(value); }

  elements.search.addEventListener('input', () => { state.search = elements.search.value; state.shown = 24; render(); });
  elements.status.addEventListener('change', () => { state.status = elements.status.value; state.shown = 24; render(); });
  elements.sort.addEventListener('change', () => {
    state.sort = elements.sort.value;
    state.shown = 24;
    sortRows();
    renderRecords();
  });
  $('#reset-filters').addEventListener('click', resetFilters); $('#empty-reset').addEventListener('click', resetFilters);
  $('#dialog-close').addEventListener('click', () => elements.dialog.close());
  elements.dialog.addEventListener('click', (event) => { if (event.target === elements.dialog) elements.dialog.close(); });

  elements.timeline.addEventListener('pointermove', (event) => { const point = event.target.closest?.('.life-point'); if (point) showPointTooltip(event, point); });
  elements.timeline.addEventListener('pointerleave', () => elements.tooltip.classList.remove('visible'));
  elements.timeline.addEventListener('focusin', (event) => { const point = event.target.closest?.('.life-point'); if (point) showPointTooltip(null, point); });
  elements.timeline.addEventListener('focusout', () => elements.tooltip.classList.remove('visible'));
  elements.timeline.addEventListener('click', (event) => { const point = event.target.closest?.('.life-point'); if (point) openDetail(timelineRows[Number(point.dataset.index)]); });
  elements.timeline.addEventListener('keydown', (event) => { if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('.life-point')) { event.preventDefault(); openDetail(timelineRows[Number(event.target.dataset.index)]); } });

  let zoomFrame = 0;
  let zoomSettleTimer = 0;
  function scheduleRangeRender() {
    if (!zoomFrame) {
      zoomFrame = requestAnimationFrame(() => {
        zoomFrame = 0;
        state.shown = 24;
        applyFilters();
        elements.count.textContent = fmt.format(filtered.length);
        elements.context.textContent = `${formatYear(state.zoomStart)}–${formatYear(state.zoomEnd)} · ${occupationSelectionLabel()}`;
        renderTimeline();
      });
    }
    clearTimeout(zoomSettleTimer);
    zoomSettleTimer = setTimeout(render, 90);
  }
  function handleZoomInput(changedHandle) {
    let start = Number(elements.zoomStart.value);
    let end = Number(elements.zoomEnd.value);
    if (start >= end) {
      if (changedHandle === 'start') start = end - 1;
      else end = start + 1;
    }
    state.zoomStart = Math.max(FULL_YEAR_MIN, start);
    state.zoomEnd = Math.min(FULL_YEAR_MAX, end);
    elements.zoomStart.value = state.zoomStart;
    elements.zoomEnd.value = state.zoomEnd;
    updateZoomControl();
    scheduleRangeRender();
  }
  elements.zoomStart.addEventListener('input', () => handleZoomInput('start'));
  elements.zoomEnd.addEventListener('input', () => handleZoomInput('end'));
  elements.zoomStart.addEventListener('change', () => { clearTimeout(zoomSettleTimer); render(); });
  elements.zoomEnd.addEventListener('change', () => { clearTimeout(zoomSettleTimer); render(); });
  [elements.zoomStart, elements.zoomEnd].forEach((handle) => {
    handle.addEventListener('pointerdown', () => handle.classList.add('active'));
    handle.addEventListener('pointerup', () => handle.classList.remove('active'));
  });
  document.querySelectorAll('input[name="cloud-mode"]').forEach((input) => input.addEventListener('change', () => {
    state.cloudMode = input.value;
    renderTimeline();
  }));

  let yearFieldTimer = 0;
  function handleYearFields(changedField) {
    let start = Number(elements.yearMin.value);
    let end = Number(elements.yearMax.value);
    if (!Number.isFinite(start) || !Number.isFinite(end)) return;
    start = Math.round(start); end = Math.round(end);
    if (start >= end) {
      if (changedField === 'min') start = end - 1;
      else end = start + 1;
    }
    setZoomWindow(start, end);
    clearTimeout(yearFieldTimer);
    yearFieldTimer = setTimeout(() => { state.shown = 24; render(); }, 180);
  }
  elements.yearMin.addEventListener('input', () => handleYearFields('min'));
  elements.yearMax.addEventListener('input', () => handleYearFields('max'));
  elements.yearMin.addEventListener('change', () => { handleYearFields('min'); render(); });
  elements.yearMax.addEventListener('change', () => { handleYearFields('max'); render(); });

  elements.occupationButton.addEventListener('click', () => {
    const open = elements.occupationButton.getAttribute('aria-expanded') === 'true';
    elements.occupationButton.setAttribute('aria-expanded', String(!open));
    elements.occupationPanel.hidden = open;
  });
  elements.occupationGroups.addEventListener('change', (event) => {
    const occupationInput = event.target.closest('[data-occupation-choice]');
    const groupInput = event.target.closest('[data-group]');
    if (occupationInput) {
      if (occupationInput.checked) state.selectedOccupations.add(occupationInput.dataset.occupationChoice);
      else state.selectedOccupations.delete(occupationInput.dataset.occupationChoice);
    } else if (groupInput) {
      const group = occupationInventory.find((item) => item.category === groupInput.dataset.group);
      group.occupations.forEach((occupation) => {
        if (groupInput.checked) state.selectedOccupations.add(occupation.name);
        else state.selectedOccupations.delete(occupation.name);
      });
    }
    state.shown = 24;
    syncOccupationControls();
    render();
  });
  $('#occupation-select-all').addEventListener('click', () => setOccupationSelection(allOccupationNames));
  $('#occupation-select-none').addEventListener('click', () => setOccupationSelection([]));
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.occupation-filter-shell') && !elements.occupationPanel.hidden) {
      elements.occupationPanel.hidden = true;
      elements.occupationButton.setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !elements.occupationPanel.hidden) {
      elements.occupationPanel.hidden = true;
      elements.occupationButton.setAttribute('aria-expanded', 'false');
      elements.occupationButton.focus();
    }
  });

  const lazyLoader = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting) && state.shown < filtered.length) {
      state.shown = Math.min(state.shown + 24, filtered.length);
      renderRecords();
    }
  }, { rootMargin: '500px 0px' });
  lazyLoader.observe(elements.sentinel);

  window.addEventListener('resize', debounce(renderTimeline, 120));
  function debounce(fn, wait) { let timeout; return () => { clearTimeout(timeout); timeout = setTimeout(fn, wait); }; }

  $('#hero-count').textContent = fmt.format(totalPeople);
  [elements.zoomStart, elements.zoomEnd].forEach((handle) => {
    handle.min = FULL_YEAR_MIN; handle.max = FULL_YEAR_MAX; handle.step = 1;
  });
  setZoomWindow(FULL_YEAR_MIN, FULL_YEAR_MAX);
  buildOccupationInventory();
  renderOccupationPanel();
  render();
})();
