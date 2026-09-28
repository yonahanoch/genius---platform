// What a brand-new store owner actually sees before uploading any data.
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1360, height: 900 } });
  const pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('PAGEERROR ' + e.message));
  // the CDN (Chart.js) is unreachable from this sandbox; that is the harness, not the app
  pg.on('console', m => {
    const t = m.text();
    if (m.type() === 'error' && !/ERR_TUNNEL_CONNECTION_FAILED|ERR_NAME_NOT_RESOLVED|jsdelivr|cdnjs|404 \(NOT FOUND\)/.test(t))
      errors.push('console ' + t.slice(0, 160));
  });
  let fail = 0;
  const chk = (n, ok, x = '') => {
    console.log((ok ? '  PASS  ' : '  FAIL  ') + n + (ok ? '' : '   ' + String(x).slice(0, 300)));
    if (!ok) fail++;
  };
  const txt = async sel => (await pg.locator(sel).innerText()).trim();
  const go = async id => { await pg.evaluate(i => show(i), id); await pg.waitForTimeout(1200); };

  await pg.goto('http://127.0.0.1:8000/index.html', { waitUntil: 'domcontentloaded' });
  await pg.waitForFunction(() => document.querySelectorAll('#store-picker option').length > 0, null, { timeout: 10000 });

  // register a real new store through the real endpoint
  const reg = await pg.evaluate(async () => {
    const r = await fetch(gxApi() + '/onboard', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'מכולת חדשה', phone: '052-9998877', business_type: 'makolet' })
    });
    return r.json();
  });
  chk('a new store can register', !!reg.store_id && !!reg.store_token, JSON.stringify(reg).slice(0, 200));

  // sign in as that store, exactly as the site does
  await pg.evaluate(t => {
    localStorage.setItem('gx_mine', JSON.stringify({ [t.sid]: { name: 'מכולת חדשה', token: t.tok } }));
  }, { tok: reg.store_token, sid: reg.store_id });
  await pg.reload({ waitUntil: 'domcontentloaded' });
  await pg.waitForTimeout(2500);
  await pg.evaluate(sid => {
    const p = document.getElementById('store-picker');
    if (![...p.options].some(o => o.value === sid)) { const o = document.createElement('option'); o.value = sid; o.textContent = 'מכולת חדשה'; p.appendChild(o); }
    p.value = sid; p.dispatchEvent(new Event('change'));
  }, reg.store_id);
  await pg.waitForTimeout(2000);

  console.log('=== overview of an empty store ===');
  const ov = await txt('#overview-body');
  console.log('---- rendered ----\n' + ov + '\n------------------');
  chk('does NOT silently show ₪0 as if it were a real number',
    !/₪0\b/.test(ov) || /עדיין אין|העלה קובץ|טרם הועלו/.test(ov), ov.slice(0, 300));
  chk('tells the owner the next step is uploading a sales file',
    /העלה קובץ|חיבור נתונים|קובץ מכירות/.test(ov), ov.slice(0, 300));

  console.log('=== the analysis screens ===');
  for (const [id, sel] of [['trends', '#trends-body'], ['forecast', '#forecast-body'],
                           ['production', '#production-body'], ['report', '#report-body']]) {
    await go(id);
    const t = await txt(sel);
    chk(id + ': explains there is no data yet, in Hebrew',
      /אין עדיין נתוני מכירות|עדיין אין נתונים/.test(t), t.slice(0, 200));
    chk(id + ': no raw English error leaks to the owner',
      !/no sales data|no saved sales/i.test(t), t.slice(0, 200));
    chk(id + ': offers a way to get there, not just a sentence',
      /onclick|button/i.test(await pg.locator(sel).innerHTML()), (await pg.locator(sel).innerHTML()).slice(0, 200));
  }

  await pg.screenshot({ path: 'shot_firstrun.png', fullPage: true });
  chk('no console errors anywhere in the first run', errors.length === 0, errors.join(' | '));
  console.log('\n' + (fail ? fail + ' FAILED' : 'all first-run checks passed'));
  await b.close();
  process.exit(fail ? 1 : 0);
})();
