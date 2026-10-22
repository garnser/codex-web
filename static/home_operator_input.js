import { request } from './api_client.js';

export function assertHomeOperatorReplacement(payload) {
  if (payload.defer_start && payload.agent_profile_id) {
    throw new Error('Operator conversation was replaced. Refresh Home to inspect the original request before sending again.');
  }
}

// Selection supplies explicit request pins; server-side admission remains the
// authority boundary. No input is sent, retried or redirected by this reader.
export async function prepareHomeOperatorInput(projectId, threadId, payload, read = request) {
  if (projectId !== 'home') return payload;
  const state = await read('/api/home/operator');
  if (state.binding?.thread_id !== threadId || !state.binding.agent_profile_id) return payload;
  if (!state.can_configure || state.status !== 'configured' || state.thread?.id !== threadId
      || !state.agent_profile || state.agent_profile.id !== state.binding.agent_profile_id
      || state.agent_profile.revision !== state.binding.agent_profile_revision
      || payload.execution_profile_id !== state.thread.execution_profile_id) {
    throw new Error('Operator profile or conversation changed. Refresh Home and select the current profile before sending.');
  }
  return { ...payload, defer_start: true, agent_profile_id: state.agent_profile.id,
    agent_profile_revision: state.agent_profile.revision };
}
