"""Deployment configuration schema, not an authorization or quota grant."""
from codex_web.configuration import ConfigurationSpec, ConfigurationScope, ConfigurationValueKind

PREFIX = 'recovery.preservation.'


def install_preservation_configuration(service):
    for suffix, kind in [('resource_id',ConfigurationValueKind.STRING),
                         ('memory_bytes',ConfigurationValueKind.INTEGER),
                         ('disk_bytes',ConfigurationValueKind.INTEGER),
                         ('wall_seconds',ConfigurationValueKind.INTEGER)]:
        service.register_spec(ConfigurationSpec(key=PREFIX+suffix,value_kind=kind,default=None,
            allowed_scopes=[ConfigurationScope.DEPLOYMENT]))


def preservation_configuration(service):
    values = {name:service.resolve(PREFIX+name) for name in
              ('resource_id','memory_bytes','disk_bytes','wall_seconds')}
    if any(x.source!='published' or x.value is None for x in values.values()):
        raise ValueError('preservation_deployment_configuration_required')
    result = {name:x.value for name,x in values.items()}
    if (not isinstance(result['resource_id'],str) or not result['resource_id']
        or any(type(result[n]) is not int or result[n]<=0 for n in ('memory_bytes','disk_bytes','wall_seconds'))
        or not 10<=result['wall_seconds']<=300):
        raise ValueError('invalid_preservation_deployment_budget')
    return result, {name:(x.record_id,x.revision) for name,x in values.items()}
