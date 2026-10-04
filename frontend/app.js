import { api } from './api.js';

const main = document.getElementById('main');
const nav = document.getElementById('nav');

function node(tag, value, parent) {
  const element = document.createElement(tag);
  if (value != null) element.textContent = String(value);
  if (parent) parent.append(element);
  return element;
}

function link(label, href, parent) {
  const element = node('a', label, parent);
  element.href = href;
  return element;
}

function table(headers, rows, parent) {
  const element = node('table', null, parent);
  const heading = node('tr', null, node('thead', null, element));
  headers.forEach((value) => node('th', value, heading));
  const body = node('tbody', null, element);
  rows.forEach((values) => {
    const row = node('tr', null, body);
    values.forEach((value) => {
      const cell = node('td', null, row);
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value == null ? '' : String(value);
    });
  });
}

function error(message) {
  node('p', message, main).setAttribute('role', 'alert');
}

function loginScreen() {
  main.replaceChildren();
  node('h2', 'Sign in', main);
  const form = node('form', null, main);
  const username = node('input', null, form);
  username.name = 'username';
  username.autocomplete = 'username';
  username.required = true;
  username.placeholder = 'Username';
  const password = node('input', null, form);
  password.name = 'password';
  password.type = 'password';
  password.autocomplete = 'current-password';
  password.required = true;
  password.placeholder = 'Password';
  node('button', 'Sign in', form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api('session/', { method: 'POST', body: JSON.stringify({ username: username.value, password: password.value }) });
      await show();
    } catch (exception) {
      error(exception.message);
    }
  });
}

async function showTables() {
  const { tables } = await api('tables/');
  node('h2', 'Released tables', main);
  if (!tables.length) return node('p', 'No released tables.', main);
  table(['Schema', 'Table', 'Status', 'Action'], tables.map((item) => {
    const action = link(item.action, `#/tables/${encodeURIComponent(item.schema)}/${encodeURIComponent(item.name)}`);
    return [item.schema, item.label, item.safety, action];
  }), main);
}

async function showTable(schema, name) {
  const page = new URLSearchParams(location.hash.split('?')[1] || '').get('page');
  const path = `tables/${encodeURIComponent(schema)}/${encodeURIComponent(name)}/`;
  const data = await api(page ? `${path}?page=${encodeURIComponent(page)}` : path);
  link('← Back to tables', '#/', main);
  node('h2', `${data.label} (${schema}.${name})`, main);
  node('p', `${data.status} · ${data.total_rows} tool-owned row(s)`, main);
  const rows = data.rows.map((row) => [...row.values, row.row_token ? 'Managed' : 'Read only']);
  table([...data.columns.map((column) => column.label), 'Action'], rows, main);
  const route = `#/tables/${encodeURIComponent(schema)}/${encodeURIComponent(name)}`;
  if (data.page > 1) link('Previous', `${route}?page=${data.page - 1}`, main);
  if (data.page < data.total_pages) link('Next', `${route}?page=${data.page + 1}`, main);
}

async function show() {
  const session = await api('session/');
  nav.replaceChildren();
  if (!session.authenticated) return loginScreen();
  link('Database Editor', '#/', nav);
  const signOut = node('button', 'Sign out', nav);
  signOut.addEventListener('click', async () => {
    await api('session/', { method: 'DELETE' });
    await show();
  });
  main.replaceChildren();
  try {
    const match = location.hash.match(/^#\/tables\/([^/?]+)\/([^/?]+)/);
    if (match) await showTable(decodeURIComponent(match[1]), decodeURIComponent(match[2]));
    else await showTables();
  } catch (exception) {
    error(exception.message);
  }
}

window.addEventListener('hashchange', show);
show().catch((exception) => error(exception.message));
