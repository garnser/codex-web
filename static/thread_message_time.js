export function coerceMessageDate(value) {
  if (!value) return null;
  if (typeof value === "number") {
    return new Date(value > 1_000_000_000_000 ? value : value * 1000);
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function messageTimestamp(...candidates) {
  for (const candidate of candidates) {
    const date = coerceMessageDate(candidate);
    if (date) return date;
  }
  return new Date();
}

export function formatMessageTimestamp(value) {
  const date = coerceMessageDate(value) || new Date();
  return date.toLocaleString();
}

export function itemTimestamp(item = {}, turn = {}) {
  return messageTimestamp(
    item.createdAt,
    item.created_at,
    item.timestamp,
    item.completedAt,
    item.completed_at,
    item.updatedAt,
    item.updated_at,
    turn.createdAt,
    turn.created_at,
    turn.startedAt,
    turn.started_at,
    turn.updatedAt,
    turn.updated_at,
  );
}
