let value = '';
let timer = null;

export function installWorkItemSearch(root, onSearch) {
  root.querySelector('.work-items-search')?.addEventListener('input', (event) => {
    value = event.target.value || '';
    clearTimeout(timer);
    timer = setTimeout(onSearch, 200);
  });
}

export function applyWorkItemSearch(query) {
  if (value.trim()) query.set('q', value.trim());
}
