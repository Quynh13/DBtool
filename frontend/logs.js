import { api } from './api.js';

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

function grid(parent, headers, rows) {
  const table = node('table', null, parent);
  const heading = node('tr', null, node('thead', null, table));
  headers.forEach((value) => node('th', value, heading));
  const body = node('tbody', null, table);
  rows.forEach((values) => {
    const row = node('tr', null, body);
    values.forEach((value) => {
      const cell = node('td', null, row);
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value == null ? '' : String(value);
    });
  });
}

export async function showLogs(main) {
  const page = new URLSearchParams(location.hash.split('?')[1] || '').get('page') || '1';
  const data = await api(`operations/?page=${encodeURIComponent(page)}`);
  main.replaceChildren();
  node('h2', 'Operation Logs', main);
  grid(main, ['Time', 'Action', 'Table', 'Status', 'Actor', 'Detail'], data.operations.map((item) => [
    item.created_at, item.action, `${item.schema}.${item.table}`, item.status, item.actor,
    link('View', `#/operations/${item.id}`),
  ]));
  if (data.page > 1) link('Previous', `#/operations?page=${data.page - 1}`, main);
  if (data.page < data.total_pages) link('Next', `#/operations?page=${data.page + 1}`, main);
  if (data.configuration_events.length) {
    node('h2', 'Recent configuration events', main);
    grid(main, ['Time', 'Table', 'Action', 'Actor', 'Release change'], data.configuration_events.map((item) => [
      item.created_at, `${item.schema}.${item.table}`, item.action, item.actor,
      `${item.was_released} → ${item.is_released}`,
    ]));
  }
}

export async function showOperation(main, id) {
  const data = await api(`operations/${encodeURIComponent(id)}/`);
  main.replaceChildren();
  link('← Operation Logs', '#/operations', main);
  node('h2', `${data.action} ${data.schema}.${data.table}`, main);
  node('p', `Status: ${data.status}; actor: ${data.actor || 'unknown'}; time: ${data.created_at}`, main);
  for (const [label, value] of [
    ['Record key', data.record_key], ['Before', data.before_data], ['After', data.after_data],
  ]) {
    node('h3', label, main);
    node('pre', JSON.stringify(value, null, 2), main);
  }
  if (data.error_message) node('p', data.error_message, main);
  if (data.can_rollback) {
    const button = node('button', 'Rollback', main);
    button.addEventListener('click', async () => {
      if (!confirm(`Rollback ${data.action} on ${data.schema}.${data.table}?`)) return;
      try {
        await api(`operations/${encodeURIComponent(id)}/rollback/`, { method: 'POST' });
        await showOperation(main, id);
      } catch (exception) {
        node('p', exception.message, main).setAttribute('role', 'alert');
      }
    });
  }
}
