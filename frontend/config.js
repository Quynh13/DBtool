import { api } from './api.js';

function node(tag, text, parent) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = String(text);
  if (parent) parent.append(element);
  return element;
}

function link(text, href, parent) {
  const element = node('a', text, parent);
  element.href = href;
  return element;
}

function input(parent, name, label, value, type = 'text') {
  const wrapper = node('div', null, parent);
  const caption = node('label', label, wrapper);
  const element = node(type === 'textarea' ? 'textarea' : 'input', null, caption);
  if (type !== 'textarea') element.type = type;
  element.name = name;
  if (type === 'checkbox') element.checked = Boolean(value);
  else element.value = value ?? '';
  return element;
}

function select(parent, name, label, options, selected, multiple = false) {
  const wrapper = node('div', null, parent);
  const caption = node('label', label, wrapper);
  const element = node('select', null, caption);
  element.name = name;
  element.multiple = multiple;
  if (multiple) element.size = Math.min(Math.max(options.length, 2), 8);
  const chosen = new Set(Array.isArray(selected) ? selected : [selected]);
  options.forEach((value) => element.add(new Option(value, value, false, chosen.has(value))));
  return element;
}

function errors(parent, messages) {
  for (const [field, items] of Object.entries(messages || {})) {
    node('p', `${field}: ${items.map((item) => item.message).join('; ')}`, parent).setAttribute('role', 'alert');
  }
}

function replaceOptions(element, options, selected) {
  const chosen = new Set(Array.isArray(selected) ? selected : [selected]);
  element.replaceChildren(...options.map((value) => new Option(value, value, false, chosen.has(value))));
}

export async function showConfiguration(main) {
  const { tables } = await api('configuration/');
  main.replaceChildren();
  node('h2', 'Database Configuration', main);
  if (!tables.length) return node('p', 'No physical tables found.', main);
  const grid = node('table', null, main);
  const heading = node('tr', null, node('thead', null, grid));
  ['Table', 'Configuration', 'Release', 'Action'].forEach((title) => node('th', title, heading));
  const body = node('tbody', null, grid);
  for (const item of tables) {
    const row = node('tr', null, body);
    node('td', `${item.schema}.${item.name}`, row);
    node('td', item.configuration_status, row);
    node('td', item.release_status, row);
    const actions = node('td', null, row);
    const route = `#/configuration/${encodeURIComponent(item.schema)}/${encodeURIComponent(item.name)}`;
    link(item.action, route, actions);
    if (item.can_release || item.can_unrelease) {
      const action = item.can_release ? 'release' : 'unrelease';
      const button = node('button', item.can_release ? 'Release' : 'Unrelease', actions);
      button.addEventListener('click', async () => {
        try {
          await api(`configuration/${encodeURIComponent(item.schema)}/${encodeURIComponent(item.name)}/${action}/`, { method: 'POST' });
          await showConfiguration(main);
        } catch (exception) {
          errors(main, { Action: [{ message: exception.message }] });
        }
      });
    }
  }
}

function mappingEditor(parent, prefix, values, sourceColumns, destinationColumns) {
  const rows = node('div', null, parent);
  const add = node('button', 'Add output mapping', parent);
  add.type = 'button';
  function addRow(source = '', destination = '') {
    const row = node('div', null, rows);
    const sourceSelect = select(row, '', 'Source', sourceColumns, source);
    const destinationSelect = select(row, '', 'Destination', destinationColumns, destination);
    const remove = node('button', 'Remove', row);
    remove.type = 'button';
    remove.addEventListener('click', () => row.remove());
    return { row, sourceSelect, destinationSelect };
  }
  let initial = values;
  if (typeof initial === 'string') {
    try { initial = JSON.parse(initial); } catch { initial = []; }
  }
  if (Array.isArray(initial)) initial.forEach(({ source, destination }) => addRow(source, destination));
  add.addEventListener('click', () => addRow());
  const hidden = node('input', null, parent);
  hidden.type = 'hidden';
  hidden.name = `${prefix}-output_mappings`;
  return {
    refresh(columns) {
      for (const row of rows.children) replaceOptions(row.querySelector('select'), columns, row.querySelector('select').value);
    },
    collect() {
      hidden.value = JSON.stringify([...rows.children].map((row) => ({
        source: row.querySelectorAll('select')[0].value,
        destination: row.querySelectorAll('select')[1].value,
      })));
    },
  };
}

export async function showReview(main, schema, table, provided) {
  const path = `configuration/${encodeURIComponent(schema)}/${encodeURIComponent(table)}/`;
  const data = provided || await api(path);
  main.replaceChildren();
  link('← Database Configuration', '#/configuration', main);
  node('h2', `${schema}.${table}`, main);
  node('p', `Configuration: ${data.configuration_status} · Release: ${data.release_status}`, main);
  node('p', `Physical type: ${data.table_type}; primary key: ${data.primary_key.join(', ') || 'None'}`, main);
  data.release_blockers.forEach((reason) => node('p', reason, main));
  if (data.can_release || data.can_unrelease) {
    const action = data.can_release ? 'release' : 'unrelease';
    const button = node('button', data.can_release ? 'Release' : 'Unrelease', main);
    button.addEventListener('click', async () => {
      try {
        await api(`${path}${action}/`, { method: 'POST' });
        await showReview(main, schema, table);
      } catch (exception) {
        errors(main, { Action: [{ message: exception.message }] });
      }
    });
  }
  const form = node('form', null, main);
  const tableSection = node('fieldset', null, form);
  node('legend', 'Table configuration', tableSection);
  input(tableSection, 'table-display_label', 'Display label', data.table_form.values.display_label);
  input(tableSection, 'table-mutation_enabled', 'Enable mutations', data.table_form.values.mutation_enabled, 'checkbox');
  errors(tableSection, data.table_form.errors);
  const mappings = [];
  for (const column of data.columns) {
    const values = column.values;
    const prefix = column.prefix;
    const section = node('fieldset', null, form);
    node('legend', `${column.name} (${column.data_type})`, section);
    node('p', `Nullable: ${column.nullable}; default: ${column.default ?? 'none'}; PK: ${column.primary_key}; identity: ${column.identity}; generated: ${column.generated}`, section);
    for (const [name, label] of [
      ['reviewed', 'Reviewed'], ['show_in_form', 'Show in form'],
      ['editable', 'Editable'], ['required_on_insert', 'Required on insert'],
      ['allow_clear_on_update', 'Allow clear on update'],
    ]) input(section, `${prefix}-${name}`, label, values[name], 'checkbox');
    input(section, `${prefix}-display_label`, 'Display label', values.display_label);
    const mode = select(section, `${prefix}-input_mode`, 'Input mode', ['text', 'lookup', 'fixed'], values.input_mode);
    const fixed = node('div', null, section);
    const fixedTypes = {
      date: 'date',
      'timestamp without time zone': 'datetime-local',
      'timestamp with time zone': 'datetime-local',
      'time without time zone': 'time',
      'time with time zone': 'time',
      smallint: 'number', integer: 'number', bigint: 'number',
      numeric: 'number', decimal: 'number', real: 'number', 'double precision': 'number',
    };
    let fixedControl;
    if (column.data_type === 'boolean') {
      fixedControl = select(fixed, `${prefix}-fixed_insert_value`, 'Fixed insert value', ['', 'true', 'false'], values.fixed_insert_value);
    } else {
      fixedControl = input(fixed, `${prefix}-fixed_insert_value`, 'Fixed insert value', values.fixed_insert_value, fixedTypes[column.data_type] || 'text');
      if (fixedControl.type === 'number') fixedControl.step = ['smallint', 'integer', 'bigint'].includes(column.data_type) ? '1' : 'any';
    }
    input(section, `${prefix}-help_text`, 'Help text', values.help_text, 'textarea');
    const lookup = node('div', null, section);
    node('h3', 'Lookup', lookup);
    const schemas = Object.keys(data.catalog).sort();
    const schemaSelect = select(lookup, `${prefix}-target_schema`, 'Target schema', schemas, values.target_schema);
    const tableSelect = select(lookup, `${prefix}-target_table`, 'Target table', Object.keys(data.catalog[schemaSelect.value] || {}).sort(), values.target_table);
    const sourceColumns = () => data.catalog[schemaSelect.value]?.[tableSelect.value] || [];
    const valueSelect = select(lookup, `${prefix}-target_value_column`, 'Target value column', sourceColumns(), values.target_value_column);
    const displaySelect = select(lookup, `${prefix}-display_columns`, 'Display columns', sourceColumns(), values.display_columns, true);
    const searchSelect = select(lookup, `${prefix}-search_columns`, 'Search columns', sourceColumns(), values.search_columns, true);
    input(lookup, `${prefix}-allow_null`, 'Allow null', values.allow_null, 'checkbox');
    input(lookup, `${prefix}-allow_arbitrary_value`, 'Allow arbitrary value', values.allow_arbitrary_value, 'checkbox');
    const mapping = mappingEditor(lookup, prefix, values.output_mappings, sourceColumns(), data.columns.map((item) => item.name));
    mappings.push(mapping);
    function refreshTables() {
      replaceOptions(tableSelect, Object.keys(data.catalog[schemaSelect.value] || {}).sort(), tableSelect.value);
      refreshColumns();
    }
    function refreshColumns() {
      const columns = sourceColumns();
      replaceOptions(valueSelect, columns, valueSelect.value);
      replaceOptions(displaySelect, columns, [...displaySelect.selectedOptions].map((option) => option.value));
      replaceOptions(searchSelect, columns, [...searchSelect.selectedOptions].map((option) => option.value));
      mapping.refresh(columns);
    }
    schemaSelect.addEventListener('change', refreshTables);
    tableSelect.addEventListener('change', refreshColumns);
    function toggle() {
      lookup.hidden = mode.value !== 'lookup';
      fixed.hidden = mode.value !== 'fixed';
      if (mode.value === 'fixed') {
        section.querySelector(`[name="${prefix}-show_in_form"]`).checked = false;
        section.querySelector(`[name="${prefix}-editable"]`).checked = false;
      }
    }
    mode.addEventListener('change', toggle);
    toggle();
    errors(section, column.errors);
  }
  node('button', 'Save configuration', form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    mappings.forEach((mapping) => mapping.collect());
    try {
      await api(path, { method: 'POST', body: new FormData(form) });
      await showReview(main, schema, table);
    } catch (exception) {
      if (exception.data?.columns) await showReview(main, schema, table, exception.data);
      else errors(main, { Save: [{ message: exception.message }] });
    }
  });
}
