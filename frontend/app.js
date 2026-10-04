async function loadTables() {
  const status = document.getElementById('status');
  try {
    const response = await fetch('/api/v1/tables/', { credentials: 'same-origin' });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const { tables } = await response.json();
    const body = document.querySelector('#tables tbody');
    for (const table of tables) {
      const row = body.insertRow();
      for (const value of [table.schema, table.label, table.safety]) {
        row.insertCell().textContent = value;
      }
      const link = document.createElement('a');
      link.href = table.action_url;
      link.textContent = table.action;
      row.insertCell().append(link);
    }
    document.getElementById('tables').hidden = tables.length === 0;
    status.textContent = tables.length ? `${tables.length} released table(s)` : 'No released tables.';
  } catch (error) {
    status.textContent = `Unable to load tables: ${error.message}`;
  }
}

loadTables();
