const chartNumber=value=>{const number=Number(String(value??'').replace(/,/g,''));return Number.isFinite(number)?number:0};
const chartEscape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const shortChartDate=value=>String(value||'').slice(5).replace('-','/');

function attachChartTooltips(host){
  const targets=host.querySelectorAll('[data-chart-tooltip]');
  if(!targets.length)return;
  const tooltip=document.createElement('div');
  tooltip.className='chart-tooltip';
  tooltip.hidden=true;
  host.appendChild(tooltip);

  const hide=()=>{tooltip.hidden=true;tooltip.classList.remove('below')};
  const show=target=>{
    const title=target.dataset.tooltipTitle||'';
    const details=(target.dataset.tooltipDetails||'').split('|').filter(Boolean);
    tooltip.innerHTML=`<strong>${chartEscape(title)}</strong>${details.map(detail=>`<span>${chartEscape(detail)}</span>`).join('')}`;
    tooltip.hidden=false;
    tooltip.classList.remove('below');

    const hostRect=host.getBoundingClientRect(),svg=target.ownerSVGElement,svgRect=svg.getBoundingClientRect();
    const viewBox=svg.viewBox.baseVal;
    const anchorX=chartNumber(target.dataset.anchorX),anchorY=chartNumber(target.dataset.anchorY);
    let left=svgRect.left-hostRect.left+(anchorX-viewBox.x)/viewBox.width*svgRect.width;
    let top=svgRect.top-hostRect.top+(anchorY-viewBox.y)/viewBox.height*svgRect.height-10;
    const half=tooltip.offsetWidth/2,edge=7;
    left=Math.max(half+edge,Math.min(host.clientWidth-half-edge,left));
    if(top-tooltip.offsetHeight<edge){
      top=svgRect.top-hostRect.top+(anchorY-viewBox.y)/viewBox.height*svgRect.height+10;
      tooltip.classList.add('below');
    }
    tooltip.style.left=`${left}px`;
    tooltip.style.top=`${top}px`;
  };

  targets.forEach(target=>{
    target.addEventListener('pointerenter',()=>show(target));
    target.addEventListener('pointerleave',hide);
    target.addEventListener('mousemove',()=>show(target));
    target.addEventListener('mouseleave',hide);
  });
  host.addEventListener('pointerleave',hide);
}

function renderDashboardLineChart(hostId,rows,series,ariaLabel){
  const host=document.getElementById(hostId);
  if(!host)return;
  const ordered=[...rows].sort((a,b)=>String(a['日期']).localeCompare(String(b['日期']))).slice(-16);
  if(!ordered.length){host.innerHTML='<div class="chart-empty">历史数据不足，运行任务后会逐日形成趋势。</div>';return}
  const width=720,height=230,pad={left:42,right:18,top:18,bottom:34};
  const plotWidth=width-pad.left-pad.right,plotHeight=height-pad.top-pad.bottom;
  const values=series.flatMap(item=>ordered.map(row=>chartNumber(item.value(row))));
  const maximum=Math.max(1,...values),roundedMax=Math.ceil(maximum/5)*5||1;
  const x=index=>ordered.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(ordered.length-1);
  const y=value=>pad.top+plotHeight-chartNumber(value)/roundedMax*plotHeight;
  const grid=Array.from({length:5},(_,index)=>{const value=roundedMax*(4-index)/4,cy=pad.top+plotHeight*index/4;return `<line class="chart-grid-line" x1="${pad.left}" y1="${cy}" x2="${width-pad.right}" y2="${cy}"/><text class="chart-axis-label" x="${pad.left-9}" y="${cy+4}" text-anchor="end">${Math.round(value)}</text>`}).join('');
  const labelIndexes=ordered.length<=6?ordered.map((_,index)=>index):[0,Math.floor((ordered.length-1)/2),ordered.length-1];
  const xLabels=labelIndexes.map(index=>`<text class="chart-axis-label" x="${x(index)}" y="${height-9}" text-anchor="middle">${chartEscape(shortChartDate(ordered[index]['日期']))}</text>`).join('');
  const paths=series.map((item,seriesIndex)=>{
    const points=ordered.map((row,index)=>({x:x(index),y:y(item.value(row)),value:chartNumber(item.value(row)),date:row['日期']}));
    const path=points.map((point,index)=>`${index?'L':'M'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ');
    const dots=points.map((point,index)=>`<circle class="chart-point chart-series-${seriesIndex}" cx="${point.x}" cy="${point.y}" r="${index===points.length-1?4.5:3}"><title>${chartEscape(point.date)} · ${chartEscape(item.name)} ${point.value}只</title></circle>`).join('');
    return `<path class="chart-line chart-series-${seriesIndex}" d="${path}"/>${dots}`;
  }).join('');
  const step=ordered.length>1?plotWidth/(ordered.length-1):plotWidth;
  const hoverZones=ordered.map((row,index)=>{
    const left=ordered.length===1?pad.left:x(index)-step/2;
    const zoneWidth=ordered.length===1?plotWidth:(index===0||index===ordered.length-1?step/2:step);
    const pointYs=series.map(item=>y(item.value(row)));
    const anchorY=Math.max(pad.top+8,Math.min(...pointYs));
    const details=series.map(item=>`${item.name}：${chartNumber(item.value(row))}只`).join('|');
    return `<rect class="chart-hover-zone" x="${left}" y="${pad.top}" width="${zoneWidth}" height="${plotHeight}" data-chart-tooltip data-tooltip-title="${chartEscape(row['日期'])}" data-tooltip-details="${chartEscape(details)}" data-anchor-x="${x(index)}" data-anchor-y="${anchorY}"/><line class="chart-hover-guide" x1="${x(index)}" y1="${pad.top}" x2="${x(index)}" y2="${pad.top+plotHeight}"/>`;
  }).join('');
  const legend=series.map((item,index)=>`<span><i class="chart-legend-swatch chart-series-bg-${index}"></i>${chartEscape(item.name)}<b>${chartNumber(item.value(ordered.at(-1)))}</b></span>`).join('');
  host.innerHTML=`<div class="chart-legend">${legend}</div><svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${chartEscape(ariaLabel)}"><title>${chartEscape(ariaLabel)}</title>${grid}${xLabels}${paths}${hoverZones}</svg>`;
  attachChartTooltips(host);
}

function renderClassificationDonut(latest,names){
  const host=document.getElementById('classification-donut-chart');
  if(!host)return;
  if(!latest||!names.length){host.innerHTML='<div class="chart-empty">尚未生成分类数量数据。</div>';return}
  const values=names.map(name=>({name,value:chartNumber(latest[`${name}数量`])})).filter(item=>item.value>0);
  const total=values.reduce((sum,item)=>sum+item.value,0);
  if(!total){host.innerHTML='<div class="chart-empty">最新分类数量为空。</div>';return}
  const radius=64,circumference=2*Math.PI*radius;
  let offset=0;
  const segments=values.map(item=>{
    const length=item.value/total*circumference,index=names.indexOf(item.name);
    const percentage=(item.value/total*100).toFixed(1);
    const middleAngle=-Math.PI/2+(offset+length/2)/circumference*Math.PI*2;
    const anchorX=90+radius*Math.cos(middleAngle),anchorY=90+radius*Math.sin(middleAngle);
    const segment=`<circle class="donut-segment chart-series-${index%9}" cx="90" cy="90" r="${radius}" stroke-dasharray="${length} ${circumference-length}" stroke-dashoffset="${-offset}" data-chart-tooltip data-tooltip-title="${chartEscape(item.name)}" data-tooltip-details="股票数量：${item.value}只|占比：${percentage}%" data-anchor-x="${anchorX.toFixed(2)}" data-anchor-y="${anchorY.toFixed(2)}"><title>${chartEscape(item.name)} ${item.value}只 · ${percentage}%</title></circle>`;
    offset+=length;
    return segment;
  }).join('');
  const legend=values.map(item=>{const index=names.indexOf(item.name);return `<button type="button" onclick="openStockList('classification','${chartEscape(item.name)}')"><i class="chart-legend-swatch chart-series-bg-${index%9}"></i><span>${chartEscape(item.name)}</span><b>${item.value}</b><small>${(item.value/total*100).toFixed(1)}%</small></button>`}).join('');
  host.innerHTML=`<div class="donut-layout"><svg class="donut-svg" viewBox="0 0 180 180" role="img" aria-label="最新九种分类结构，共${total}只"><title>最新九种分类结构</title><circle class="donut-track" cx="90" cy="90" r="${radius}"/>${segments}<text class="donut-total" x="90" y="86" text-anchor="middle">${total}</text><text class="donut-caption" x="90" y="104" text-anchor="middle">只股票</text></svg><div class="donut-legend">${legend}</div></div>`;
  attachChartTooltips(host);
}

function renderClassificationCharts(data){
  const rows=data?.rows||[];
  const names=(data?.columns||[]).slice(1).map(column=>column.replace(/数量$/,''));
  renderDashboardLineChart('classification-trend-chart',rows,[
    {name:'主升与赶顶',value:row=>chartNumber(row['上升数量'])+chartNumber(row['震荡上行数量'])+chartNumber(row['赶顶数量'])},
    {name:'弱势区间',value:row=>chartNumber(row['下降数量'])+chartNumber(row['震荡下行数量'])},
    {name:'过渡与模糊',value:row=>chartNumber(row['过渡状态数量'])+chartNumber(row['边界模糊数量'])},
  ],'分类趋势宽度折线图');
  renderClassificationDonut(data?.latest||{},names);
  const latestDate=data?.latest?.['日期']||'—';
  const breadthDate=document.getElementById('breadth-chart-date'),donutDate=document.getElementById('donut-chart-date');
  if(breadthDate)breadthDate.textContent=`${rows.length}个交易日`;
  if(donutDate)donutDate.textContent=latestDate;
}

function renderTargetTrendChart(data){
  renderDashboardLineChart('target-trend-chart',data?.rows||[],[
    {name:'强势',value:row=>row['强势数量']},
    {name:'近期新高',value:row=>row['近期新高数量']},
    {name:'历史新高',value:row=>row['历史新高数量']},
  ],'计算标的数量趋势折线图');
}
