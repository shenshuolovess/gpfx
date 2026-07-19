(() => {
  const table = {rows: [], columns: [], sortColumn: '交易日数', sortDirection: 'asc'};

  function header() {
    document.querySelector('#history-table thead').innerHTML = `<tr>${table.columns.map(column => {
      const active = table.sortColumn === column;
      const arrow = active ? (table.sortDirection === 'asc' ? '▲' : '▼') : '↕';
      return `<th><button class="sort-head ${active ? 'active' : ''}" data-history-sort="${escapeHtml(column)}"><span>${escapeHtml(column)}</span><i>${arrow}</i></button></th>`;
    }).join('')}</tr>`;
    document.querySelectorAll('[data-history-sort]').forEach(button => button.onclick = () => {
      const column = button.dataset.historySort;
      if (table.sortColumn === column) table.sortDirection = table.sortDirection === 'asc' ? 'desc' : 'asc';
      else { table.sortColumn = column; table.sortDirection = 'asc'; }
      header(); rows();
    });
  }

  function rows() {
    const query = document.querySelector('#history-search').value.trim().toLowerCase();
    let selected = table.rows.filter(row => !query || ['代码', '名称', '历史状态', '起始覆盖', '校验状态'].some(
      field => String(row[field] || '').toLowerCase().includes(query)
    ));
    selected = sortRows(selected, table.sortColumn, table.sortDirection);
    document.querySelector('#history-table tbody').innerHTML = selected.length
      ? selected.map(row => `<tr>${table.columns.map(column => `<td>${escapeHtml(row[column])}</td>`).join('')}</tr>`).join('')
      : `<tr><td colspan="${table.columns.length || 1}">没有匹配结果</td></tr>`;
    document.querySelector('#history-count').textContent = `${selected.length} / ${table.rows.length} 只`;
  }

  async function loadHistoryCoverage() {
    try {
      const data = await api('/api/previews/history-coverage');
      table.rows = data.rows; table.columns = data.columns;
      const summary = data.summary || {};
      document.querySelector('#history-summary').innerHTML = [
        ['完整', summary.complete ?? 0], ['可回测但不完整', summary.usable ?? 0],
        ['不足/缺失', summary.insufficient ?? 0], ['交易日中位数', summary.median_trading_days ?? 0],
        ['60日回测≥10截面', summary.long_ready ?? 0], ['数据源不支持指数', summary.unsupported_indexes ?? 0],
      ].map(([label, value]) => `<div class="count-card"><span>${label}</span><b>${value}</b></div>`).join('');
      document.querySelector('#history-note').textContent = data.file.path
        ? `${data.file.name} · 缺口天数可能包含停牌，不直接等同数据丢失 · 点击表头排序`
        : '尚未生成历史覆盖审计，请运行“历史覆盖审计”';
      header(); rows();
    } catch (error) { toast(error.message); }
  }

  document.querySelector('#history-search').addEventListener('input', rows);
  document.querySelector('#refresh-history').addEventListener('click', loadHistoryCoverage);
  loadHistoryCoverage();
})();
