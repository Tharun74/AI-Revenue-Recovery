const puppeteer = require('puppeteer');

(async () => {
  const browser = await puppeteer.launch({ headless: 'new' });
  const page = await browser.newPage();
  
  // Catch React errors
  page.on('console', msg => {
    if (msg.type() === 'error') {
      console.log('BROWSER ERROR:', msg.text());
    }
  });
  page.on('pageerror', err => {
    console.log('BROWSER EXCEPTION:', err.toString());
  });

  console.log('Navigating to http://localhost:3000 ...');
  await page.goto('http://localhost:3000', { waitUntil: 'networkidle0' });
  
  console.log('Checking health indicators...');
  // The header chips start out bg-slate-500 (null), and turn bg-emerald-400 (true) when health fetch succeeds.
  // We'll wait for the emerald bg class on the API chip indicator.
  try {
    await page.waitForSelector('.bg-emerald-400', { timeout: 10000 });
    console.log('Health check passed (Indicators turned green)');
  } catch(e) {
    console.log('Health check failed or timed out. Checking for red indicators...');
    const hasRed = await page.$('.bg-rose-400');
    if (hasRed) console.log('Found red indicator instead!');
  }
  
  console.log('Clicking "Run Agent" tab (if not already on it)...');
  const runTabButton = await page.evaluateHandle(() => {
    return Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Run Agent'));
  });
  if (runTabButton) await runTabButton.click();
  
  console.log('Clicking "Run Agent" action button...');
  // Find the button with Play icon and "Run Agent" or "Run Agent (Dry Run)" text
  const actionButton = await page.evaluateHandle(() => {
    const btns = Array.from(document.querySelectorAll('button'));
    return btns.find(b => b.textContent.trim().startsWith('Run Agent') && b.className.includes('bg-[var(--accent)]'));
  });
  
  if (actionButton) {
    await actionButton.click();
    console.log('Waiting for run to complete (watching for "Dashboard" content)...');
    // Once it finishes, it switches to Dashboard tab and shows "At Risk" metric card
    try {
      await page.waitForFunction(() => {
        return document.body.innerText.includes('At Risk') && document.body.innerText.includes('Stop Compliance');
      }, { timeout: 30000 });
      console.log('Agent Run completed successfully. UI updated with metrics.');
    } catch(e) {
      console.log('Timeout waiting for run to complete.');
    }
  } else {
    console.log('Action button not found.');
  }

  await browser.close();
})();
