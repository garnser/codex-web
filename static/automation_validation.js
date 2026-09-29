// These paths mirror the canonical Automation schema; the API still validates.
export function automationEditorErrors(form) {
  const kind = form.elements.trigger_type.value;
  const required = kind === 'recurring_schedule' ? ['cron', 'timezone']
    : kind === 'one_shot_schedule' ? ['due_at']
      : kind === 'provider_event' ? ['event_type', 'provider_id']
        : kind === 'canonical_event' ? ['event_type'] : [];
  return required.filter(name => !form.elements[name].value.trim()).map(name => ({
    field: name,
    message: `Enter ${name.replaceAll('_', ' ')} for the selected trigger.`,
  }));
}

export const automationErrorFields = {
  'definition.name': 'name',
  'definition.description': 'description',
  'definition.instructions': 'instructions',
  'definition.target.id': 'target_id',
  'definition.target.kind': 'target_kind',
  'definition.trigger.type': 'trigger_type',
  'definition.trigger.cron': 'cron',
  'definition.trigger.timezone': 'timezone',
  'definition.trigger.due_at': 'due_at',
  'definition.trigger.event_type': 'event_type',
  'definition.trigger.provider_id': 'provider_id',
  'definition.budget.max_input_tokens': 'max_input_tokens',
  'definition.budget.max_output_tokens': 'max_output_tokens',
  'definition.budget.max_cost_usd': 'max_cost_usd',
  'definition.budget.max_duration_seconds': 'max_duration_seconds',
  'definition.budget.max_concurrency': 'max_concurrency',
  'definition.retry.max_attempts': 'retry_max_attempts',
  'definition.retry.backoff_seconds': 'retry_backoff_seconds',
};
