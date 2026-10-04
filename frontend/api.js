function csrfToken() {
  return document.cookie.split('; ').find((part) => part.startsWith('csrftoken='))?.split('=')[1] || '';
}

export async function api(path, options = {}) {
  const response = await fetch(`/api/v1/${path}`, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.method && options.method !== 'GET' ? { 'X-CSRFToken': csrfToken() } : {}),
      ...options.headers,
    },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
