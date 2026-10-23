// Canonical execution-context DTOs for chat fixtures; no application imports.
function executionDefinition(projectId) {
 return {record_id:`fixture-execution-catalog-${projectId}`,definition_id:'execution.profiles.default',
  kind:'execution-profile-catalog',revision:1,checksum:'a'.repeat(64),definition_schema_version:'1.0'};
}
function executionProfiles(projectId) {
 return {definition:executionDefinition(projectId),default_profile_id:'repository-write',items:[{
  id:'repository-write',name:'Repository write',workspaceMode:'repository',repositoryAccess:'mutable',
  requiredWorkerCapabilities:['git','command_execution']}]};
}
function contextualReadiness(request) {
 const url=new URL(request.url()),query=url.searchParams;
 const projectId=decodeURIComponent(url.pathname.match(/\/projects\/([^/]+)/)[1]);
 return query.has('execution_profile_id') ? {execution_context:{profile_id:query.get('execution_profile_id'),
  sandbox:query.get('sandbox'),definition:executionDefinition(projectId)}} : {};
}
module.exports={executionDefinition,executionProfiles,contextualReadiness};
