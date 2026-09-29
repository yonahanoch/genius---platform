// The palette switcher: every theme must actually change the page, persist,
// and keep text readable against its own background.
const { chromium } = require('playwright');

function lum(c) {                       // relative luminance of "rgb(r, g, b)"
  const m = c.match(/\d+/g).slice(0, 3).map(Number).map(v => {
    v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2];
}
const contrast = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};

(async () => {
  const b = await chromium.launch();
  const pg = await (await b.newContext({ viewport: { width: 1360, height: 900 } })).newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('PAGEERROR ' + e.message));
  pg.on('console', m => {
    const t = m.text();
    if (m.type() === 'error' && !/ERR_TUNNEL|ERR_NAME_NOT_RESOLVED|jsdelivr|cdnjs|NOT FOUND|UNAUTHORIZED/.test(t))
      errors.push('console ' + t.slice(0, 150));
  });
  let fail = 0;
  const chk = (n, ok, x = '') => { console.log((ok ? '  PASS  ' : '  FAIL  ') + n + (ok ? '' : '   ' + String(x).slice(0, 220))); if (!ok) fail++; };

  await pg.goto('http://127.0.0.1:8000/index.html', { waitUntil: 'domcontentloaded' });
  await pg.waitForFunction(() => document.querySelectorAll('#store-picker option').length > 0, null, { timeout: 10000 });
  await pg.waitForTimeout(1200);

  chk('the palette button is on every screen', await pg.locator('#gx-theme-btn').isVisible());

  const seen = new Set();
  for (const id of ['night', 'brass', 'ink', 'ivory']) {
    await pg.evaluate(t => gxSetTheme(t), id);
    await pg.waitForTimeout(600);
    const m = await pg.evaluate(() => {
      const cs = getComputedStyle(document.body);
      const card = document.querySelector('.net-card, .mc, .ai-row');
      return {
        bg: cs.backgroundColor, fg: cs.color,
        sub: getComputedStyle(document.documentElement).getPropertyValue('--text2').trim(),
        themeColor: document.getElementById('gx-theme-color').getAttribute('content'),
        cardBg: card ? getComputedStyle(card).backgroundColor : null,
      };
    });
    chk(id + ': background is distinct from the other themes', !seen.has(m.bg), m.bg);
    seen.add(m.bg);
    const c = contrast(m.bg, m.fg);
    chk(id + ': body text contrast >= 7:1 (AAA)', c >= 7, 'got ' + c.toFixed(1) + ' on ' + m.bg);
    chk(id + ': the phone chrome colour follows the theme', m.themeColor.toLowerCase() !== '#1d9e75' || id === 'night', m.themeColor);
  }

  // secondary text is the one most likely to end up unreadable
  for (const id of ['night', 'brass', 'ink', 'ivory']) {
    await pg.evaluate(t => gxSetTheme(t), id);
    await pg.waitForTimeout(300);
    const { bg, sub } = await pg.evaluate(() => {
      const probe = document.createElement('span');
      probe.style.color = 'var(--text2)'; document.body.appendChild(probe);
      const c = getComputedStyle(probe).color; probe.remove();
      return { bg: getComputedStyle(document.body).backgroundColor, sub: c };
    });
    const c = contrast(bg, sub);
    chk(id + ': secondary text contrast >= 4.5:1 (AA)', c >= 4.5, 'got ' + c.toFixed(1));
  }

  // a full sweep: every visible text element, against the background it really
  // sits on. This is what catches a component that assumed a dark theme.
  for (const id of ['night', 'brass', 'ink', 'ivory']) {
    await pg.evaluate(t => gxSetTheme(t), id);
    await pg.waitForTimeout(500);
    for (const screen of ['overview', 'trends', 'forecast', 'suppliers', 'settings']) {
      await pg.evaluate(sc => show(sc), screen);
      await pg.waitForTimeout(500);
      const bad = await pg.evaluate(() => {
        const solid = el => {
          for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
            const c = getComputedStyle(n).backgroundColor;
            const a = c.match(/[\d.]+/g);
            if (a && (a.length < 4 || parseFloat(a[3]) > 0.85)) return c;
          }
          return getComputedStyle(document.body).backgroundColor;
        };
        const L = c => {
          const m = c.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => {
            v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
          });
          return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2];
        };
        const ratio = (a, b) => { const [x, y] = [L(a), L(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
        const out = [];
        for (const el of document.querySelectorAll('.sc.on *, .sidebar *, .role-bar *, .topbar *')) {
          if (!el.offsetParent && el.tagName !== 'BODY') continue;
          const txt = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join('');
          if (!txt) continue;
          const cs = getComputedStyle(el);
          if (cs.visibility === 'hidden' || cs.opacity === '0') continue;
          const size = parseFloat(cs.fontSize);
          const big = size >= 24 || (size >= 18.66 && parseInt(cs.fontWeight, 10) >= 700);
          const need = big ? 3 : 4.5;
          const r = ratio(solid(el), cs.color);
          if (r < need) out.push(txt.slice(0, 22) + ' ' + r.toFixed(1) + ':1 (' + cs.color + ' on ' + solid(el) + ')');
        }
        return out;
      });
      chk(id + '/' + screen + ': every visible text passes WCAG AA', bad.length === 0, bad.slice(0, 4).join(' | '));
    }
  }

  // it has to survive a reload, and not flash the wrong palette first
  await pg.evaluate(() => gxSetTheme('ivory'));
  await pg.reload({ waitUntil: 'domcontentloaded' });
  const early = await pg.evaluate(() => document.documentElement.getAttribute('data-theme'));
  chk('the chosen theme is applied before the page paints', early === 'ivory', early);
  await pg.waitForTimeout(2000);
  chk('and it is still the one in use after loading', await pg.evaluate(() => gxTheme()) === 'ivory');

  // the default: a first-time visitor sees ivory, and a deliberate "night" sticks
  {
    const fresh = await (await b.newContext({ viewport: { width: 1360, height: 900 } })).newPage();
    await fresh.goto('http://127.0.0.1:8000/index.html', { waitUntil: 'domcontentloaded' });
    const d = await fresh.evaluate(() => ({
      attr: document.documentElement.getAttribute('data-theme'),
      saved: localStorage.getItem('gx_theme'),
      bg: getComputedStyle(document.body).backgroundColor,
      chrome: document.getElementById('gx-theme-color').getAttribute('content'),
    }));
    chk('a first-time visitor gets ivory', d.attr === 'ivory' && d.saved === null, JSON.stringify(d));
    chk('the default background is the ivory paper colour', d.bg === 'rgb(247, 244, 239)', d.bg);
    chk('the phone chrome matches the ivory default', d.chrome.toLowerCase() === '#f7f4ef', d.chrome);
    await fresh.waitForTimeout(1500);
    chk('gxTheme() reports ivory when nothing is saved', await fresh.evaluate(() => gxTheme()) === 'ivory');
    await fresh.evaluate(() => gxSetTheme('night'));
    await fresh.reload({ waitUntil: 'domcontentloaded' });
    const n = await fresh.evaluate(() => ({
      attr: document.documentElement.getAttribute('data-theme'),
      bg: getComputedStyle(document.body).backgroundColor,
      chrome: document.getElementById('gx-theme-color').getAttribute('content'),
    }));
    chk('choosing night is remembered and applied before paint', n.attr === 'night' && n.bg === 'rgb(15, 17, 23)', JSON.stringify(n));
    chk('the phone chrome follows a saved night choice', n.chrome.toLowerCase() === '#0f1117', n.chrome);
    await fresh.evaluate(() => localStorage.setItem('gx_theme', 'garbage'));
    await fresh.reload({ waitUntil: 'domcontentloaded' });
    chk('a corrupted saved value falls back to ivory', await fresh.evaluate(() => document.documentElement.getAttribute('data-theme')) === 'ivory');
    await fresh.context().close();
  }

  // the picker itself
  await pg.evaluate(() => gxThemeMenu());
  await pg.waitForTimeout(400);
  chk('the picker lists all four themes', await pg.locator('.theme-opt').count() === 4);
  chk('the current theme is marked', await pg.locator('.theme-opt[aria-pressed="true"]').count() === 1);
  await pg.evaluate(() => gxCloseTheme());
  chk('the picker closes', await pg.locator('.theme-sheet').count() === 0);

  // charts must repaint in the new palette rather than keep the old ink
  await pg.evaluate(() => gxSetTheme('night'));
  await pg.evaluate(() => show('trends'));
  await pg.waitForTimeout(2500);
  await pg.evaluate(() => gxSetTheme('ivory'));
  await pg.waitForTimeout(2500);
  chk('switching theme on a chart screen throws nothing', errors.length === 0, errors.join(' | '));
  await pg.screenshot({ path: 'shot_theme_ivory.png', fullPage: true });
  await pg.evaluate(() => gxSetTheme('brass'));
  await pg.waitForTimeout(1500);
  await pg.screenshot({ path: 'shot_theme_brass.png', fullPage: true });

  chk('no errors anywhere', errors.length === 0, errors.join(' | '));
  console.log('\n' + (fail ? fail + ' FAILED' : 'all theme checks passed'));
  await b.close();
  process.exit(fail ? 1 : 0);
})();
