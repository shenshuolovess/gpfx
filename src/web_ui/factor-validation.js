(() => {
  const state = {summary: [], monthly: [], summaryColumns: [], monthlyColumns: []};

  function renderHeader(selector, columns) {
    document.querySelector(`${selector} thead`).innerHTML = `<tr>${columns.map(
      column => `<th>${escapeHtml(column)}</th>`
    ).join('')}</tr>`;
  }

  function renderRows(selector, columns, rows, emptyText) {
    document.querySelector(`${selector} tbody`).innerHTML = rows.length
      ? rows.map(row => `<tr>${columns.map(column => `<td>${escapeHtml(row[column])}</td>`).join('')}</tr>`).join('')
      : `<tr><td colspan="${columns.length || 1}">${escapeHtml(emptyText)}</td></tr>`;
  }

  function renderMonthly() {
    const horizon = document.querySelector('#factor-horizon').value;
    let rows = state.monthly.filter(row => horizon === '全部' || row['周期'] === horizon);
    rows = [...rows].sort((a, b) => String(b['验证月份']).localeCompare(String(a['验证月份'])));
    renderHeader('#factor-monthly-table', state.monthlyColumns);
    renderRows('#factor-monthly-table', state.monthlyColumns, rows, '暂无逐月滚动记录');
  }

  async function loadFactorValidation() {
    try {
      const data = await api('/api/previews/opportunity-factors');
      state.summary = data.summary_rows; state.monthly = data.monthly_rows;
      state.summaryColumns = data.summary_columns; state.monthlyColumns = data.monthly_columns;
      const stats = data.statistics || {};
      document.querySelector('#factor-months').textContent = stats.months ? `${stats.months}个月` : '';
      document.querySelector('#factor-summary-cards').innerHTML = [
        ['初步通过', `${stats.passed_rows || 0}/${stats.total_rows || 0}`],
        ['选择回撤反弹', stats.rebound_selections || 0],
        ['选择趋势延续', stats.trend_selections || 0],
        ['主动不启用', stats.abstentions || 0],
      ].map(([label, value]) => `<div class="count-card"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`).join('');
      document.querySelector('#factor-note').textContent = data.summary_file.path
        ? `${data.summary_file.name} · ${data.warning}`
        : '请先运行“机会因子滚动验证”';
      renderHeader('#factor-summary-table', state.summaryColumns);
      renderRows('#factor-summary-table', state.summaryColumns, state.summary, '暂无滚动汇总');
      renderMonthly();
    } catch (error) { toast(error.message); }
  }

  document.querySelector('#factor-horizon').addEventListener('change', renderMonthly);
  document.querySelector('#refresh-factors').addEventListener('click', loadFactorValidation);
  loadFactorValidation();
})();
