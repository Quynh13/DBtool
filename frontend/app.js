import { api } from './api.js';
import { showConfiguration, showReview } from './config.js';

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
  const route = `#/tables/${encodeURIComponent(schema)}/${encodeURIComponent(name)}`;
  if (data.can_insert) link('Add row', `${route}/add`, main);
  const rows = data.rows.map((row) => {
    const actions = node('span');
    if (row.row_token && data.can_update) link('Edit', `${route}/rows/${encodeURIComponent(row.row_token)}/edit`, actions);
    if (row.row_token && data.can_delete) {
      actions.append(' ');
      link('Delete', `${route}/rows/${encodeURIComponent(row.row_token)}/delete`, actions);
    }
    return [...row.values, actions];
  });
  table([...data.columns.map((column) => column.label), 'Action'], rows, main);
  if (data.page > 1) link('Previous', `${route}?page=${data.page - 1}`, main);
  if (data.page < data.total_pages) link('Next', `${route}?page=${data.page + 1}`, main);
}

function fieldControl(field, parent, schema, name) {
  const wrapper = node('div', null, parent);
  const label = node('label', `${field.label}${field.required ? ' *' : ''}`, wrapper);
  const type = field.data_type;
  let control;
  if (field.lookup || type === 'boolean') {
    if (field.lookup) {
      const search = node('input', null, wrapper);
      search.type = 'search';
      search.placeholder = `Search ${field.label}`;
      search.setAttribute('aria-label', search.placeholder);
      control = node('select', null, wrapper);
      control.add(new Option('Select…', ''));
      if (field.value != null && field.value !== '') {
        control.add(new Option(String(field.value), String(field.value)));
        control.value = String(field.value);
      }
      let timer;
      search.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          try {
            const path = `tables/${encodeURIComponent(schema)}/${encodeURIComponent(name)}/lookup/${encodeURIComponent(field.name)}/?q=${encodeURIComponent(search.value)}`;
            const { results } = await api(path);
            const current = control.value;
            control.replaceChildren(new Option('Select…', ''));
            results.forEach(({ value, label }) => control.add(new Option(label, value)));
            if (current && ![...control.options].some((option) => option.value === current)) {
              control.add(new Option(current, current));
            }
            control.value = current;
          } catch (exception) {
            error(exception.message);
          }
        }, 200);
      });
    } else {
      control = node('select', null, wrapper);
      [['', 'Select…'], ['true', 'True'], ['false', 'False']].forEach(([value, text]) => control.add(new Option(text, value)));
      if (field.value != null) control.value = field.value === true ? 'true' : field.value === false ? 'false' : String(field.value);
    }
  } else if (type === 'json' || type === 'jsonb') {
    control = node('textarea', null, wrapper);
    control.rows = 6;
    control.value = field.value == null ? '' : String(field.value);
  } else {
    control = node('input', null, wrapper);
    control.type = {
      date: 'date',
      'timestamp without time zone': 'datetime-local',
      'timestamp with time zone': 'datetime-local',
      'time without time zone': 'time',
      'time with time zone': 'time',
      integer: 'number', smallint: 'number', bigint: 'number',
      numeric: 'number', decimal: 'number', real: 'number', 'double precision': 'number',
    }[type] || 'text';
    if (control.type === 'number') control.step = ['integer', 'smallint', 'bigint'].includes(type) ? '1' : 'any';
    control.value = field.value == null ? '' : String(field.value);
  }
  label.append(control);
  control.name = field.name;
  control.required = field.required;
  if (field.help_text) node('small', field.help_text, wrapper);
  return control;
}

function formErrors(data) {
  for (const [field, items] of Object.entries(data.errors || {})) {
    error(`${field === '__all__' ? 'Form' : field}: ${items.map((item) => item.message).join('; ')}`);
  }
}

async function showRowForm(schema, name, mode, token) {
  const route = `#/tables/${encodeURIComponent(schema)}/${encodeURIComponent(name)}`;
  const path = `tables/${encodeURIComponent(schema)}/${encodeURIComponent(name)}/${mode === 'add' ? 'add/' : `rows/${encodeURIComponent(token)}/edit/`}`;
  const data = await api(path);
  link('← Back to table', route, main);
  node('h2', mode === 'add' ? 'Add row' : 'Edit row', main);
  const form = node('form', null, main);
  const controls = data.fields.map((field) => fieldControl(field, form, schema, name));
  node('button', 'Save', form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(controls.map((control) => [control.name, control.value]));
    if (mode === 'edit') values.state_token = data.state_token;
    try {
      await api(path, { method: 'POST', body: JSON.stringify(values) });
      location.hash = route;
    } catch (exception) {
      error(exception.message);
      formErrors(exception.data || {});
    }
  });
}

async function showDelete(schema, name, token) {
  const path = `tables/${encodeURIComponent(schema)}/${encodeURIComponent(name)}/rows/${encodeURIComponent(token)}/delete/`;
  const data = await api(path);
  const route = `#/tables/${encodeURIComponent(schema)}/${encodeURIComponent(name)}`;
  link('← Back to table', route, main);
  node('h2', 'Delete row?', main);
  table(['Column', 'Value'], Object.entries(data.values), main);
  const button = node('button', 'Confirm delete', main);
  button.addEventListener('click', async () => {
    try {
      await api(path, { method: 'POST', body: JSON.stringify({ state_token: data.state_token }) });
      location.hash = route;
    } catch (exception) {
      error(exception.message);
    }
  });
}

async function show() {
  const session = await api('session/');
  nav.replaceChildren();
  if (!session.authenticated) return loginScreen();
  link('Database Editor', '#/', nav);
  if (session.is_superuser) link('Database Configuration', '#/configuration', nav);
  const signOut = node('button', 'Sign out', nav);
  signOut.addEventListener('click', async () => {
    await api('session/', { method: 'DELETE' });
    await show();
  });
  main.replaceChildren();
  try {
    if (location.hash === '#/configuration') return await showConfiguration(main);
    const review = location.hash.match(/^#\/configuration\/([^/?]+)\/([^/?]+)/);
    if (review) return await showReview(main, decodeURIComponent(review[1]), decodeURIComponent(review[2]));
    const match = location.hash.match(/^#\/tables\/([^/?]+)\/([^/?]+)(?:\/rows\/([^/?]+)\/(edit|delete)|\/(add))?/);
    if (match) {
      const schema = decodeURIComponent(match[1]);
      const name = decodeURIComponent(match[2]);
      if (match[5]) await showRowForm(schema, name, 'add');
      else if (match[4] === 'edit') await showRowForm(schema, name, 'edit', decodeURIComponent(match[3]));
      else if (match[4] === 'delete') await showDelete(schema, name, decodeURIComponent(match[3]));
      else await showTable(schema, name);
    }
    else await showTables();
  } catch (exception) {
    error(exception.message);
  }
}

window.addEventListener('hashchange', show);
show().catch((exception) => error(exception.message));
