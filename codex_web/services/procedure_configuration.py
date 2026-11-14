"""Finite capacity for complete required procedure guidance, not authority."""
import inspect
from codex_web.configuration import ConfigurationScope, ConfigurationSpec, ConfigurationValueKind
from codex_web.services.configuration import ConfigurationService, ConfigurationSpecRegistry
from codex_web.storage.configuration_registry import ConfigurationRegistryStore, _CANONICAL_CONFIGURATION_READS
from codex_web.skills import SKILL_MAX_CONTEXT_CHARS


PROCEDURE_CONTEXT_CHARACTERS = ConfigurationSpec(
    key="procedure_discovery.max_context_characters",
    value_kind=ConfigurationValueKind.INTEGER,
    default=12000,
    minimum=1000,
    maximum=SKILL_MAX_CONTEXT_CHARS,
    allowed_scopes=[ConfigurationScope.DEPLOYMENT],
    category="Agents & Execution",
    description=(
        "Maximum complete procedure guidance characters, including required Skill "
        "instructions, full contracts, supporting context and trust wrapper. Resolved "
        "once per discovery; never truncates required guidance or grants authority."
    ),
    startup_only=False,
    hot_reloadable=True,
    grants_authority=False,
)


def install_procedure_configuration(service: ConfigurationService) -> ConfigurationSpec:
    return service.register_spec(PROCEDURE_CONTEXT_CHARACTERS)


class ProcedureContextCapacityResolver:
    """Identifiable canonical capacity reader; no retained result or authority.

    Resolution is recomputed on every call. Only an explicit request-local raw
    read observation can replace repository I/O during guarded initial sealing.
    """

    def __init__(self, configuration: ConfigurationService):
        self.configuration = configuration

    def __call__(self):
        return self.configuration.resolve(PROCEDURE_CONTEXT_CHARACTERS.key)

    def can_observe_reads(self):
        service = self.configuration
        return (type(self) is ProcedureContextCapacityResolver
            and all(_original(self, name, method) for name, method in _CANONICAL_CAPACITY_READS.items())
            and type(service) is ConfigurationService
            and all(_original(service, name, method) for name, method in _CANONICAL_CAPACITY_CONFIGURATION_READS.items())
            and type(service.specs) is ConfigurationSpecRegistry
            and _original(service.specs, 'get', _CANONICAL_CAPACITY_SPEC_GET)
            and service.specs.get(PROCEDURE_CONTEXT_CHARACTERS.key) == PROCEDURE_CONTEXT_CHARACTERS
            and type(service.store) is ConfigurationRegistryStore
            and _CANONICAL_CONFIGURATION_READS['can_observe_reads'](service.store))

    def observe_reads(self, callback):
        if not _CANONICAL_CAPACITY_CAN_OBSERVE(self):
            return callback(None)
        return self.configuration.store.observe_reads(callback)


def _original(owner, name, method):
    current = getattr(owner, name, None)
    if name in vars(owner) or getattr(type(owner), name, None) is not method:
        return False
    return (getattr(current, '__self__', None) is owner and getattr(current, '__func__', None) is method
        or isinstance(inspect.getattr_static(type(owner), name), staticmethod) and current is method)


_CANONICAL_CAPACITY_CONFIGURATION_READS = {name: getattr(ConfigurationService, name) for name in (
    'resolve', '_resolve_records', '_record_matches_context', '_resolution_chain')}
_CANONICAL_CAPACITY_SPEC_GET = ConfigurationSpecRegistry.get
_CANONICAL_CAPACITY_READS = {name: getattr(ProcedureContextCapacityResolver, name) for name in (
    '__call__', 'can_observe_reads', 'observe_reads')}
_CANONICAL_CAPACITY_CAN_OBSERVE = ProcedureContextCapacityResolver.can_observe_reads
