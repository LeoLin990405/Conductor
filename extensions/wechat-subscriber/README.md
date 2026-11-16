# Conductor WeChat Subscriber

Chrome/Edge MV3 extension for one-click WeChat official-account subscription.

1. Open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, select this folder.
2. Open any `mp.weixin.qq.com` article.
3. Click the extension, confirm the Conductor base URL, click **一键订阅当前公众号**.
4. Conductor resolves the account from the article HTML and starts incremental sync;
   no WeChat follow-list access is performed.

Host permissions are limited to `http://127.0.0.1:8000/*` (local Conductor) and
`https://mp.weixin.qq.com/*` (article pages). If Conductor runs elsewhere, edit the
base URL in the popup and add that origin to `host_permissions`.
