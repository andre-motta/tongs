(async () => {
  // Automated DOM interactions in the native application, using synthetic data.
  // The adapter evaluates this expression and awaits its JSON-compatible result.
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const until = async (predicate, description) => {
    const deadline = performance.now() + 12000;
    while (performance.now() < deadline) {
      const result = predicate();
      if (result) return result;
      await pause(30);
    }
    throw new Error(`UI probe timed out: ${description}`);
  };
  const require = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const click = async (selector, description) => {
    const node = await until(() => document.querySelector(selector), description);
    require(!node.disabled, `${description} is disabled`);
    node.click();
    return node;
  };
  require(new URLSearchParams(location.search).get('demo') !== '1', 'Browser mock mode is forbidden for native proof');
  await until(() => window.tongs && document.querySelectorAll('.review-card').length === 2, 'native bridge and inbox');
  require((await window.tongs.invoke('health')).fixture === true, 'Expected synthetic Python backend');
  const cards = [...document.querySelectorAll('.review-card')];
  const largeCard = cards.find((node) => node.textContent.includes('20,000'));
  require(largeCard, 'Large fixture review missing');
  largeCard.focus();
  const keyboardFocusable = document.activeElement === largeCard;
  const started = performance.now();
  largeCard.click();
  await until(() => document.querySelector('.diff-stat')?.textContent.includes('20,000') && document.querySelector('.diff-canvas'), '20,000-line rendered diff');
  const largeRenderMs = performance.now() - started;
  await click('[aria-label="Side-by-side diff view"]', 'split diff control');
  await until(() => document.querySelector('.split-row'), 'split rows');
  const scroller = document.querySelector('.diff-scroll');
  scroller.scrollTop = scroller.scrollHeight;
  scroller.dispatchEvent(new Event('scroll', { bubbles: true }));
  await until(() => document.querySelector('.diff-window')?.textContent.includes('render_review_item(19999,'), 'last large-diff row');
  const renderedRows = document.querySelectorAll('.diff-row').length;
  require(renderedRows > 0 && renderedRows < 160, 'Large diff DOM is not bounded');
  const canvasHeight = document.querySelector('.diff-canvas').getBoundingClientRect().height;
  require(canvasHeight >= 500000, 'Large diff scroll range is missing');
  const normal = [...document.querySelectorAll('.review-card')].find((node) => !node.textContent.includes('20,000'));
  normal.click();
  await until(() => document.querySelector('.diff-stat')?.textContent.startsWith('80 lines') && document.querySelector('.diff-scroll').scrollTop === 0, 'normal diff reset');
  await click('[aria-label="Toggle light and dark theme"]', 'theme toggle');
  await until(() => document.querySelector('.app-shell')?.dataset.theme === 'light', 'light theme');
  await click('[aria-label="Toggle light and dark theme"]', 'theme restore');
  await until(() => document.querySelector('.app-shell')?.dataset.theme === 'dark', 'dark theme');
  const pluginsButton = [...document.querySelectorAll('.nav-item')].find((node) => node.textContent.includes('Plugins'));
  require(pluginsButton, 'Plugins navigation missing');
  pluginsButton.click();
  const input = await until(() => document.querySelector('[aria-label="Plugin message"]'), 'independently installed module mount');
  const pluginRoundTrip = async () => {
    const message = document.querySelector('[aria-label="Plugin message"]');
    message.value = 'Native common UI probe';
    const call = [...document.querySelectorAll('.plugin-mount button')].find((node) => node.textContent === 'Call Python plugin');
    require(call, 'Plugin action missing');
    call.click();
    const output = await until(() => {
      const text = document.querySelector('.plugin-mount pre')?.textContent;
      return text?.includes('Python plugin received: Native common UI probe') ? text : null;
    }, 'plugin Python response');
    const parsed = JSON.parse(output);
    require(parsed.calls > 0, 'Plugin call counter missing');
    return parsed;
  };
  await pluginRoundTrip();
  const inboxButton = [...document.querySelectorAll('.nav-item')].find((node) => node.textContent.includes('Review inbox'));
  inboxButton.click();
  await until(() => !document.querySelector('[aria-label="Plugin message"]'), 'module unmount');
  require(!input.isConnected, 'Previous module DOM was retained');
  pluginsButton.click();
  await until(() => document.querySelector('[aria-label="Plugin message"]'), 'module remount');
  const response = await pluginRoundTrip();
  const helpButton = [...document.querySelectorAll('.module-heading button')].find((node) => node.textContent.includes('Help'));
  require(helpButton, 'Plugin help action missing');
  helpButton.click();
  await until(() => document.querySelector('.help-drawer pre')?.textContent.includes('bundled'), 'bundled help in UI');
  require(!document.querySelector('.module-error'), 'Plugin module error visible');
  return {
    provenance: 'Automated DOM interactions inside actual native renderer; synthetic Python backend and independently installed sample plugin',
    bridge: 'native', large_render_ms: Math.round(largeRenderMs * 10) / 10,
    large_lines: 20000, rendered_rows_at_end: renderedRows, canvas_height: canvasHeight,
    reached_last_row: true, normal_scroll_reset: true, split_diff: true,
    keyboard_focusable: keyboardFocusable, light_and_dark: true,
    plugin_mounted: true, plugin_unmounted_and_remounted: true,
    plugin_response: response, bundled_help_visible: true,
    viewport: { width: innerWidth, height: innerHeight },
  };
})()
