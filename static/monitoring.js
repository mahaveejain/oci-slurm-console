/* The Mac loopback helper opens an isolated Chrome session. Credentials never pass through the controller. */
function normalizeGrafanaUrl(value) {
  const text = value.trim();
  if (!text || /[\s\\]/.test(text) || text.startsWith('//')) throw new Error('Enter a Grafana IP address or an HTTP(S) URL.');
  const explicit = /^[a-z][a-z\d+.-]*:\/\//i.test(text);
  if (!explicit && /^(javascript|data|file):/i.test(text)) throw new Error('Use an HTTP or HTTPS address.');
  const url = new URL(explicit ? text : `http://${text}`);
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash) {
    throw new Error('Use an HTTP(S) address without credentials, query parameters, or a fragment.');
  }
  if (!explicit && !url.port) url.port = '3000';
  const path = url.pathname.replace(/\/+$/, '');
  url.pathname = path.endsWith('/login') ? path : `${path}/login`;
  return url;
}

const grafanaForm = document.querySelector('#grafana-form');
if (grafanaForm) {
  const address = document.querySelector('#grafana-url');
  const username = document.querySelector('#grafana-username');
  const password = document.querySelector('#grafana-password');
  const message = document.querySelector('#grafana-message');
  const transport = document.querySelector('#grafana-transport');
  const openButton = document.querySelector('#open-grafana');
  const directLink = document.querySelector('#grafana-direct-link');
  const storageKey = 'slurm-console.grafana-url';
  const credentialsKey = 'slurm-console.grafana-credentials';
  const credentialsMessage = document.querySelector('#grafana-credentials-message');
  const passwordToggle = document.querySelector('#grafana-toggle-password');
  function hidePassword() {
    password.type = 'password';
    passwordToggle.textContent = 'Show';
    passwordToggle.setAttribute('aria-pressed', 'false');
    passwordToggle.setAttribute('aria-label', 'Show password');
  }
  function restoreCredentials() {
    username.value = '';
    password.value = '';
    hidePassword();
    credentialsMessage.textContent = '';
    try {
      const saved = JSON.parse(localStorage.getItem(credentialsKey));
      if (saved && saved.url === normalizeGrafanaUrl(address.value).href &&
          typeof saved.username === 'string' && typeof saved.password === 'string') {
        username.value = saved.username;
        password.value = saved.password;
        credentialsMessage.textContent = 'Saved credentials loaded for this Grafana address.';
      }
    } catch (_) { /* Invalid records and unavailable storage leave fields empty. */ }
  }
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved) address.value = normalizeGrafanaUrl(saved).href;
  } catch (_) { /* The default remains usable when browser storage is disabled. */ }
  restoreCredentials();
  passwordToggle.addEventListener('click', () => {
    const reveal = password.type === 'password';
    password.type = reveal ? 'text' : 'password';
    passwordToggle.textContent = reveal ? 'Hide' : 'Show';
    passwordToggle.setAttribute('aria-pressed', String(reveal));
    passwordToggle.setAttribute('aria-label', reveal ? 'Hide password' : 'Show password');
  });
  document.querySelector('#grafana-save-credentials').addEventListener('click', () => {
    try {
      const url = normalizeGrafanaUrl(address.value).href;
      if (!username.value.trim() || !password.value) {
        credentialsMessage.textContent = 'Enter both username and password before saving.';
        return;
      }
      // Save the URL first so a credential record is never restored for a different destination.
      localStorage.setItem(storageKey, url);
      localStorage.setItem(credentialsKey, JSON.stringify({url, username: username.value.trim(), password: password.value}));
      address.value = url;
      hidePassword();
      credentialsMessage.textContent = 'Credentials saved in this browser until you clear them. Saving does not sign in.';
    } catch (_) {
      credentialsMessage.textContent = 'Could not save credentials. Check the Grafana address and browser storage permissions.';
    }
  });
  document.querySelector('#grafana-clear-credentials').addEventListener('click', () => {
    username.value = '';
    password.value = '';
    hidePassword();
    try {
      localStorage.removeItem(credentialsKey);
      credentialsMessage.textContent = 'Saved credentials cleared from this browser. The Grafana address is retained.';
    } catch (_) {
      credentialsMessage.textContent = 'Fields cleared, but browser storage could not be cleared. Clear this site’s data in browser settings.';
    }
  });
  function updateTransport() {
    directLink.hidden = true;
    try {
      transport.textContent = normalizeGrafanaUrl(address.value).protocol === 'http:'
        ? 'This address uses HTTP. Any sign-in credentials will be sent unencrypted. Use HTTPS for protected sign-in.' : '';
    } catch (_) { transport.textContent = ''; }
  }
  address.addEventListener('input', () => {
    restoreCredentials();
    message.textContent = '';
    updateTransport();
  });
  updateTransport();
  grafanaForm.addEventListener('submit', async event => {
    event.preventDefault();
    let url;
    try { url = normalizeGrafanaUrl(address.value); }
    catch (error) { message.textContent = error.message; return; }
    const user = username.value.trim();
    if (Boolean(user) !== Boolean(password.value)) {
      message.textContent = 'Provide both username and password, or leave both empty to open Grafana.';
      return;
    }
    address.value = url.href;
    try { localStorage.setItem(storageKey, url.href); } catch (_) { /* Optional storage. */ }
    directLink.href = url.href;
    directLink.textContent = 'Open login page';
    directLink.hidden = false;
    openButton.disabled = true;
    message.textContent = 'Opening a separate Chrome Incognito session on this Mac…';
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 90000);
    const helper = 'http://127.0.0.1:8765';
    try {
      const statusResponse = await fetch(`${helper}/status`, {
        cache: 'no-store', credentials: 'omit', signal: abort.signal,
      });
      if (!statusResponse.ok) throw new Error('The Mac helper does not allow this console address. Restart it with the correct --origin.');
      const status = await statusResponse.json();
      if (status.service !== 'slurm-grafana-browser-helper' || !status.token) {
        throw new Error('The local port is not running the Grafana browser helper.');
      }
      const response = await fetch(`${helper}/open`, {
        method: 'POST', credentials: 'omit', referrerPolicy: 'no-referrer',
        headers: {'Content-Type': 'application/json', 'Authorization': `Bearer ${status.token}`},
        body: JSON.stringify({url: url.href, username: user, password: password.value}),
        signal: abort.signal,
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'The Mac helper could not open Grafana.');
      message.textContent = result.signed_in
        ? 'Signed in to Grafana in a separate Chrome Incognito window on this Mac.'
        : 'Grafana login opened in a separate Chrome Incognito window. Enter credentials there or save them here for automatic sign-in.';
    } catch (error) {
      message.textContent = error instanceof TypeError
        ? 'The Mac browser helper is unavailable. On this Mac, run scripts/start-grafana-browser-helper.sh and try again. If Chrome asks this console for local network access, allow it. Open login page is also available.'
        : error.name === 'AbortError'
          ? 'The Mac browser helper timed out. Check its window and Grafana connectivity before retrying.'
          : error.message;
    } finally {
      clearTimeout(timeout);
      hidePassword();
      openButton.disabled = false;
    }
  });
}
