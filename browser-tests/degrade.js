// Every screen must survive a server that answers with a different shape —
// an older deploy, a newer one, or a field that simply is not there.
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const pg = await (await b.newContext({ viewport: { width: 1360, height: 900 } })).newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('PAGEERROR ' + e.message));
  pg.on('console', m => {
    const t = m.text();
    if (m.type() === 'error' && !/ERR_TUNNEL|ERR_NAME_NOT_RESOLVED|jsdelivr|cdnjs|NOT FOUND|UNAUTHORIZED|FORBIDDEN/.test(t))
      errors.push('console ' + t.slice(0, 160));
  });
  let fail = 0;
  const chk = (n, ok, x = '') => { console.log((ok ? '  PASS  ' : '  FAIL  ') + n + (ok ? '' : '   ' + String(x).slice(0, 250))); if (!ok) fail++; };

  await pg.goto('http://127.0.0.1:8000/index.html', { waitUntil: 'domcontentloaded' });
  await pg.waitForFunction(() => document.querySelectorAll('#store-picker option').length > 0, null, { timeout: 10000 });
  await pg.waitForTimeout(1200);

  // every API answer becomes {} — the emptiest shape a server can send
  await pg.evaluate(() => {
    const real = window.fetch;
    window.fetch = async (u, o) => {
      if (String(u).includes(gxApi())) return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
      return real(u, o);
    };
  });

  for (const id of ['overview', 'products', 'trends', 'forecast', 'production', 'report',
                    'ads', 'suppliers', 'settings', 'network', 'dashboard', 'alerts']) {
    const before = errors.length;
    try { await pg.evaluate(i => show(i), id); } catch (e) { errors.push('show(' + id + ') threw ' + e.message); }
    await pg.waitForTimeout(700);
    chk(id + ': renders without throwing on an empty server answer',
        errors.length === before, errors.slice(before).join(' | '));
  }
  chk('no errors anywhere', errors.length === 0, errors.join(' | '));
  console.log('\n' + (fail ? fail + ' FAILED' : 'all degradation checks passed'));
  await b.close();
  process.exit(fail ? 1 : 0);
})();
