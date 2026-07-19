(() => {
  const table = {rows: [], columns: [], sortColumn: '机会评分', sortDirection: 'desc'};

  function renderHeader() {
    document.querySelector('#opportunity-table thead').innerHTML = `<tr>${table.columns.map(column => {
      const active = table.sortColumn === column;
      const arrow = active ? (table.sortDirection === 'asc' ? '▲' : '▼') : '↕';
      return `<th><button class="sort-head ${active ? 'active' : ''}" data-opportunity-sort="${escapeHtml(column)}"><span>${escapeHtml(column)}</span><i>${arrow}</i></button></th>`;
    }).join('')}</tr>`;
    document.querySelectorAll('[data-opportunity-sort]').forEach(button => button.onclick = () => {
      const column = button.dataset.opportunitySort;
      if (table.sortColumn === column) table.sortDirection = table.sortDirection === 'asc' ? 'desc' : 'asc';
      else { table.sortColumn = column; table.sortDirection = 'desc'; }
      renderHeader(); renderRows();
    });
  }

  function renderRows() {
    const query = document.querySelector('#opportunity-search').value.trim().toLowerCase();
    let rows = table.rows.filter(row => !query || ['代码', '名称', '分类', '所属行业', '机会等级'].some(
      field => String(row[field] || '').toLowerCase().includes(query)
    ));
    rows = sortRows(rows, table.sortColumn, table.sortDirection);
    document.querySelector('#opportunity-table tbody').innerHTML = rows.length
      ? rows.map(row => `<tr>${table.columns.map(column => `<td>${escapeHtml(row[column])}</td>`).join('')}</tr>`).join('')
      : `<tr><td colspan="${table.columns.length || 1}">没有匹配结果</td></tr>`;
    document.querySelector('#opportunity-count').textContent = `${rows.length} / ${table.rows.length} 只`;
  }

  async function loadOpportunityScores() {
    try {
      const data = await api('/api/previews/opportunity-scores');
      table.rows = data.rows; table.columns = data.columns;
      document.querySelector('#opportunity-note').textContent = data.file.path
        ? `${data.file.name} · 实验评分，当前回测尚未证明稳定排序能力 · 不是收益概率 · 点击表头排序`
        : '尚未生成机会评分，请运行“机会评分”或“综合评级”';
      renderHeader(); renderRows();
    } catch (error) { toast(error.message); }
  }

  document.querySelector('#opportunity-search').addEventListener('input', renderRows);
  document.querySelector('#refresh-opportunity').addEventListener('click', loadOpportunityScores);
  const defaultTimer = setInterval(() => {
    const card = document.querySelector('.task-card[data-task="opportunity_backtest"]');
    if (!card) return;
    const defaults = {max_stocks: 0, snapshots: 30, step: 5, horizons: '5'};
    Object.entries(defaults).forEach(([name, value]) => {
      const input = card.querySelector(`[data-opt="${name}"]`);
      if (input) input.value = value;
    });
    clearInterval(defaultTimer);
  }, 100);
  loadOpportunityScores();
})();
