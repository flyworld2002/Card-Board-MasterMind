window.__cbm = { state: 'run', done: 0, total: 0, lines: [], sent: 0, err: '' };
window.__cbmNext = async () => {
  const S = window.__cbm;
  for (let t = 0; t < 30 && S.state === 'run'; t++) await new Promise(r => setTimeout(r, 500));
  if (S.state === 'run') return 'CBMRH|wait|' + S.done + '/' + S.total;
  if (S.state === 'error') return 'CBMRH|error|' + S.err;
  if (S.sent >= S.lines.length) return 'CBMRH|end|' + S.lines.length;
  const part = S.lines.slice(S.sent, S.sent + 40);
  S.sent += part.length;
  return ['CBMRH|part|' + S.sent + '/' + S.lines.length].concat(part);
};
(async () => {
  const S = window.__cbm;
  const getJson = (u, o) => fetch(u, o).then(r => r.ok ? r.json() : Promise.reject(new Error('http ' + r.status)));
  const post = (u, b) => getJson(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
  try {
    const pids = [];
    for (let from = 0; ; from += 50) {
      const j = await post('https://mp-search-api.tcgplayer.com/v1/search/request?q=&isList=true', {
        algorithm: 'sales_exp_fields_synonym', from: from, size: 50,
        filters: { term: { productLineName: ['pokemon'], setName: ['__SLUG__'], productTypeName: ['Cards'] }, range: {}, match: {} },
        listingSearch: { context: { cart: {} }, filters: { term: { sellerStatus: 'Live', channelId: 0 }, range: { quantity: { gte: 1 } }, exclude: { channelExclusion: 0 } } },
        context: { cart: {}, shippingCountry: 'US', userProfile: {} },
        settings: { useFuzzySearch: false, didYouMean: {} },
        sort: { field: 'product-name', order: 'asc' }
      });
      const res = j.results[0];
      for (const p of res.results) if (p.customAttributes && p.customAttributes.number) pids.push(Math.round(p.productId));
      if (from + 50 >= res.totalResults || !res.results.length) break;
    }
    S.total = pids.length;
    let next = 0;
    const one = async (pid) => {
      let h;
      try { h = await getJson('https://infinite-api.tcgplayer.com/price/history/' + pid + '/detailed?range=month'); }
      catch (e) { S.lines.push(pid + '|x|x'); return; }
      const rows = (h.result || []).filter(x => x.variant === 'Reverse Holofoil' && x.condition === 'Near Mint' && (!x.language || x.language === 'English'));
      if (!rows.length) return;
      let price = '-';
      for (const b of (rows[0].buckets || [])) { const m = parseFloat(b.marketPrice); if (m > 0) { price = m.toFixed(2); break; } }
      let count = '-';
      try {
        const l = await post('https://mp-search-api.tcgplayer.com/v1/product/' + pid + '/listings', {
          filters: { term: { sellerStatus: 'Live', channelId: 0, language: ['English'], condition: ['Near Mint'], printing: ['Reverse Holofoil'] }, range: { quantity: { gte: 1 } }, exclude: { channelExclusion: 0 } },
          from: 0, size: 1, context: { shippingCountry: 'US', cart: {} }, sort: { field: 'price+shipping', order: 'asc' }
        });
        count = String(Math.round(l.results[0].totalResults));
      } catch (e) { count = 'x'; }
      S.lines.push(pid + '|' + count + '|' + price);
    };
    const worker = async () => { while (next < pids.length) { const pid = pids[next++]; await one(pid); S.done++; } };
    await Promise.all([worker(), worker(), worker(), worker()]);
    S.state = 'done';
  } catch (e) { S.err = String(e).slice(0, 150); S.state = 'error'; }
})();
'CBMRH|started'
