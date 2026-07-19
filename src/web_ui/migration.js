(() => {
  let comparison = null;
  const html = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[ch]);
  const pct = value => value === null || value === undefined || Number.isNaN(Number(value))
    ? '—' : `${Number(value) >= 0 ? '+' : ''}${(Number(value) * 100).toFixed(2)}%`;
  const ratioText = value => value === null || value === undefined || Number.isNaN(Number(value))
    ? '—' : `${(Number(value) * 100).toFixed(1)}%`;

  function stockBoard(rows, board) {
    const stocks = rows.filter(row => row['榜单'] === board)
      .sort((a, b) => Number(a['排名']) - Number(b['排名']));
    return `<section class="migration-board"><h4>${board}</h4>${stocks.length
      ? stocks.map(row => `<div class="migration-stock"><b>${row['排名']}</b><span>${html(row['名称'] || row['代码'])}<small>${html(row['代码'])} · ${row['样本数']}样本</small></span><b class="${Number(row['平均同池超额']) >= 0 ? 'migration-positive' : 'migration-negative'}">${pct(row['平均同池超额'])}</b><span>跑赢 ${ratioText(row['跑赢同池率'])}</span></div>`).join('')
      : '<div class="empty">暂无可用样本</div>'}</section>`;
  }

  function render() {
    if (!comparison?.available) return;
    const horizon = `${document.querySelector('#rule-horizon')?.value || comparison.horizons[0]}日`;
    const period = document.querySelector('#rule-period')?.value || '总体';
    const migrations = (comparison.migrations || []).filter(
      row => row['周期'] === horizon && row['样本区间'] === period
    );
    const stocks = (comparison.migration_stocks || []).filter(
      row => row['周期'] === horizon && row['样本区间'] === period
    );
    document.querySelector('#rule-thresholds').innerHTML = (comparison.threshold_changes || [])
      .map(row => `<span class="threshold-chip"><b>${html(row['阈值名称'])}</b>${row['基线阈值']} → ${row['候选阈值']} · ${html(row['调整方向'])}</span>`).join('');
    document.querySelector('#rule-migrations').innerHTML = migrations.length
      ? migrations.map(row => {
          const related = stocks.filter(stock => stock['候选规则'] === row['候选规则'] && stock['基线分类'] === row['基线分类'] && stock['候选分类'] === row['候选分类']);
          const positive = Number(row['平均同池超额']) >= 0;
          return `<details class="migration-item"><summary><div class="migration-name"><b>${html(row['基线分类'])} → ${html(row['候选分类'])}</b><small>${html(row['候选规则'])} · ${html(row['统计结论'])}</small></div><div class="migration-metric"><span>样本 / 股票</span><b>${row['样本数']} / ${row['不同股票数']}</b></div><div class="migration-metric"><span>覆盖截面</span><b>${row['覆盖截面数']}</b></div><div class="migration-metric"><span>平均同池超额</span><b class="${positive ? 'migration-positive' : 'migration-negative'}">${pct(row['平均同池超额'])}</b></div><div class="migration-metric"><span>跑赢同池率</span><b>${ratioText(row['跑赢同池率'])}</b></div><div class="migration-metric"><span>可信度</span><b>${html(row['可信度'])}</b></div></summary><div class="migration-body"><div class="migration-trigger">主要触发：${html(row['主要触发阈值'] || '—')} · 95% CI ${pct(row['同池超额95%CI下限'])} ～ ${pct(row['同池超额95%CI上限'])} · ${html(row['数据质量提示'])}</div><div class="migration-boards">${stockBoard(related, '正向榜')}${stockBoard(related, '负向榜')}</div></div></details>`;
        }).join('')
      : '<div class="empty">当前区间和周期没有分类迁移样本</div>';
  }

  async function load() {
    try {
      const response = await fetch('/api/previews/rule-comparison');
      if (!response.ok) return;
      comparison = await response.json();
      render();
    } catch (_) { /* 主页面会统一提示接口错误。 */ }
  }

  document.querySelector('#rule-horizon')?.addEventListener('change', render);
  document.querySelector('#rule-period')?.addEventListener('change', render);
  document.querySelector('#refresh-rules')?.addEventListener('click', () => setTimeout(load, 100));
  const note = document.querySelector('#rule-note');
  if (note) new MutationObserver(render).observe(note, {childList: true, characterData: true, subtree: true});
  load();
})();
