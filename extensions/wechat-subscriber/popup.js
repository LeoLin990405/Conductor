const baseInput = document.getElementById('base');
const button = document.getElementById('subscribe');
const status = document.getElementById('status');

chrome.storage.local.get({ base: 'http://127.0.0.1:8000' }, ({ base }) => {
  baseInput.value = base;
});

async function activeArticleUrl() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url || '';
  if (!/https:\/\/mp\.weixin\.qq\.com\/s(\?|\/)/.test(url)) {
    throw new Error('请在微信公众号文章页使用这个扩展');
  }
  return url;
}

button.addEventListener('click', async () => {
  const base = baseInput.value.trim().replace(/\/+$/, '') || 'http://127.0.0.1:8000';
  await chrome.storage.local.set({ base });
  button.disabled = true;
  status.textContent = '正在解析文章…';
  try {
    const url = await activeArticleUrl();
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const [{ result: account }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => (document.querySelector('#js_name')?.textContent || '').trim(),
    });
    const response = await fetch(`${base}/api/v1/connections/wechat/subscriptions/from-article`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, account: account || '' }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    status.textContent = `已订阅公众号「${data.account || ''}」，共 ${data.count || 0} 篇文章。`;
  } catch (error) {
    status.textContent = `失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
});
