const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
                                   deviceScaleFactor: 2 });
  const pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('PAGEERROR ' + e.message));
  let fail = 0;
  const chk = (n, ok, x='') => { console.log((ok ? '  PASS  ' : '  FAIL  ') + n + (ok ? '' : '   ' + String(x).slice(0,200))); if (!ok) fail++; };
  const overflow = () => pg.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

  await pg.goto('http://127.0.0.1:8000/index.html', { waitUntil: 'domcontentloaded' });
  await pg.waitForFunction(() => document.querySelectorAll('#store-picker option').length > 0, null, { timeout: 10000 });
  await pg.waitForTimeout(1500);

  chk('menu button visible on a phone', await pg.isVisible('.menu-btn'));
  chk('sidebar hidden until asked', !(await pg.evaluate(() => {
    const r = document.querySelector('.sidebar').getBoundingClientRect();
    return r.right > 5 && r.left < window.innerWidth - 5;
  })));
  chk('no sideways scrolling of the page', (await overflow()) <= 1, await overflow());

  await pg.click('.menu-btn');
  await pg.waitForTimeout(400);
  chk('menu opens', await pg.evaluate(() => document.body.classList.contains('menu-open')));
  await pg.click('.sidebar >> text=כמה להכין');
  await pg.waitForTimeout(1500);
  chk('menu closes after choosing a screen', !(await pg.evaluate(() => document.body.classList.contains('menu-open'))));
  // tomorrow may be a closed day (Friday, a holiday); the table check needs a
  // day that actually has a table, or it would pass without testing anything
  for (const v of await pg.$$eval('#prod-date option', o => o.map(x => x.value))) {
    if ((await pg.locator('#production-body').innerText()).includes('כמות מוצעת')) break;
    await pg.evaluate(d => loadProduction(d), v);
    await pg.waitForTimeout(700);
  }
  chk('production screen shown', (await pg.locator('#production-body').innerText()).includes('כמות מוצעת'));
  chk('the table is really there to test', await pg.locator('#production-body table').count() > 0);
  chk('wide table scrolls inside its box, page does not', (await overflow()) <= 1, await overflow());
  await pg.screenshot({ path: 'shot_mobile_production.png', fullPage: true });

  for (const [id, sel] of [['overview', '#overview-body'], ['trends', '#trends-body'], ['products', '#products-body'],
                           ['report', '#report-body'], ['suppliers', '#suppliers-body'], ['settings', '#settings-body']]) {
    await pg.evaluate(i => show(i), id);
    await pg.waitForTimeout(1400);
    const o = await overflow();
    chk('no horizontal overflow on ' + id, o <= 1, o);
    const tiny = await pg.evaluate(s => {
      const el = document.querySelector(s);
      return el ? [...el.querySelectorAll('*')].some(n => {
        const st = getComputedStyle(n);
        return n.textContent.trim() && parseFloat(st.fontSize) < 10;
      }) : false;
    }, sel);
    chk('no text under 10px on ' + id, !tiny);
  }
  await pg.evaluate(() => show('overview'));
  await pg.waitForTimeout(1200);
  await pg.screenshot({ path: 'shot_mobile_overview.png', fullPage: true });
  chk('no JS errors', errors.length === 0, errors.join(' | '));
  console.log(fail ? 'FAILURES: ' + fail : '>>> MOBILE CHECKS PASS');
  await b.close();
  process.exit(fail ? 1 : 0);
})();
