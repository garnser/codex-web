export function timeoutText(value) {
  if (value === null || value === undefined || value === '') return 'Not set';
  const seconds = Number(value);
  if (seconds === 0 || seconds === -1) return 'Disabled';
  return Number.isFinite(seconds) && seconds > 0 ? `${seconds}s` : 'Invalid timeout';
}
