"""Durable custody compatibility through real models and private canonical storage."""
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
from codex_web import execution_workers as models
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.sqlite_state import SQLiteStateStore

# Exact ExecutionAssignment class from 5936d097677feea2ba66a125511ac12ef3d14661.
# Its unchanged dependent types come from the model module; no Git/history needed in CI.
OLD_ASSIGNMENT_SOURCE = r'''
class ExecutionAssignment(ResourceContextModel):
    resource_context: ReadOnlyResourceContext | None = None
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"assignment-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    subject: ExecutionSubject | None = None
    work_item_ref: str | None = None
    execution_id: str
    project_id: str | None = None
    resource_ids: tuple[str, ...]
    base_revision: str | None = None
    execution_contract_version: str
    required_capabilities: tuple[WorkerCapability, ...]
    sandbox: SandboxMode
    approval_policy: ApprovalPolicy
    network: NetworkPolicy
    limits: WorkerResourceLimits
    secret_refs: tuple[str, ...] = ()
    deadline_at: float | None = None
    expected_artifact_types: tuple[str, ...] = ()
    expected_evidence_types: tuple[str, ...] = ()
    execution_workspace_id: str | None = None
    runtime_binding: ExecutionRuntimeBinding | None = None
    repository_target: RepositoryExecutionTarget | None = None
    repository_scope: RepositoryExecutionScope | None = None
    execution_profile_id: str | None = None
    execution_profile_definition: DefinitionReference | None = None
    agent_profile: AgentProfileExecutionBinding | None = None
    skill_refs: tuple[DefinitionReference, ...] = ()
    skill_assignment_sources: dict[str, str] = Field(default_factory=dict)
    procedure_discoveries: tuple[ProcedureDiscovery, ...] = ()
    procedure_checkpoint: ProcedureCheckpoint | None = None
    # Retained terminal runs, never evicted or reused as the current writer.
    procedure_history: tuple[ProcedureCheckpoint, ...] = Field(default=(), max_length=63)
    status: AssignmentStatus = AssignmentStatus.PENDING
    fence: int = Field(default=0, ge=0)
    lease: AssignmentLease | None = None
    assigned_worker_id: str | None = None
    created_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    failure: FailureRecord | None = None
    artifact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def procedure_runs(self) -> "ExecutionAssignment":
        runs = (*self.procedure_history, self.procedure_checkpoint)
        ids = [p.id or "legacy" for p in runs if p is not None]
        if len(set(ids)) != len(ids) or any(p.status != "completed" for p in self.procedure_history):
            raise ValueError("procedure history requires distinct completed run identities")
        if self.procedure_history and self.procedure_checkpoint is None:
            raise ValueError("procedure history requires a current run")
        return self

    @model_validator(mode="after")
    def normalize_subject(self) -> "ExecutionAssignment":
        self.subject, self.work_item_ref = normalize_execution_subject(
            self.subject,
            self.work_item_ref,
        )
        return self
'''

class NativeCredentialCustodyCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = dict(vars(models))
        exec(compile(OLD_ASSIGNMENT_SOURCE, "<5936-ExecutionAssignment>", "exec"), namespace)
        cls.old_reader = namespace["ExecutionAssignment"]

    def legacy(self):
        return self.old_reader(organization_id="local", workspace_id="private", execution_id="private-execution",
            subject={"kind":"thread_bootstrap", "ref":"private-thread"},
            resource_ids=(), execution_contract_version="thread-bootstrap/1.0", required_capabilities=(),
            sandbox="read-only", approval_policy="never", network=models.NetworkPolicy(),
            limits=models.WorkerResourceLimits(), created_by="private-requester",
            created_at=100, updated_at=100).model_dump(mode="json")

    def marked(self):
        assignment = models.ExecutionAssignment.model_validate(self.legacy())
        assignment.native_credential_custody = models.NativeCredentialCustody(
            runtime_secret_ref="secret-runtime", admitted_at=101, requester_identity_id="private-requester",
            worker_identity_id="private-worker", fence=1,
            procedures=({"definition_id":"private-procedure", "kind":"agent.skill", "revision":1,
                "record_id":"private-record", "checksum":"a"*64, "definition_schema_version":"1.2"},),
            provider_bindings={"private-binding":"b"*64}, procedure_secret_refs=("secret-provider",))
        return assignment

    def test_legacy_113_decode_remains_unadmitted_and_preserves_assignment_bytes(self):
        original = self.legacy()
        payload = {"schema_version":"1.13", "assignments":[original]}
        migrated = ExecutionWorkerStore(None)._decode(payload)
        assignment = migrated.assignments[0]
        self.assertEqual(migrated.schema_version, "1.14")
        self.assertIsNone(assignment.native_credential_custody)
        self.assertEqual(assignment.model_dump(mode="json"), original)
        self.assertNotIn("native_credential_custody", assignment.model_dump(mode="json"))
        self.assertEqual(payload, {"schema_version":"1.13", "assignments":[original]})

    def test_marked_114_roundtrip_retains_exact_reference_provenance(self):
        marked = self.marked()
        raw = marked.model_dump(mode="json")
        state = ExecutionWorkerStore(None)._decode({"schema_version":"1.14", "assignments":[raw]})
        self.assertEqual(state.assignments[0].model_dump(mode="json"), raw)
        self.assertEqual(state.assignments[0].native_credential_custody, marked.native_credential_custody)

    def test_real_keyed_storage_matches_aggregate_migration_and_marked_roundtrip(self):
        for version, raw in (("1.13", self.legacy()), ("1.14", self.marked().model_dump(mode="json"))):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                sqlite = SQLiteStateStore(Path(directory)/"private.sqlite3")
                try:
                    store = ExecutionWorkerStore(sqlite)
                    sqlite.put(store.namespace, {"schema_version":version, "assignments":[raw]})
                    aggregate = store._decode(sqlite.get(store.namespace)).assignments[0]
                    keyed = store.assignment(raw["id"])
                    self.assertIsNone(sqlite.get(store.namespace))
                    self.assertEqual(keyed.model_dump(mode="json"), aggregate.model_dump(mode="json"))
                    stored = sqlite.record_get(store.assignment_namespace, raw["id"])
                    self.assertEqual(stored, raw)
                    page, cursor = store.assignment_page(limit=1)
                    self.assertEqual(len(page), 1)
                    self.assertEqual(page[0].model_dump(mode="json"), raw)
                finally:
                    close = getattr(sqlite, "close", None)
                    if close is not None:
                        close()

    def test_actual_5936_reader_accepts_legacy_and_refuses_new_custody(self):
        original = self.legacy()
        self.assertEqual(self.old_reader.model_validate(original).model_dump(mode="json"), original)
        with self.assertRaises(ValidationError) as refused:
            self.old_reader.model_validate(self.marked().model_dump(mode="json"))
        self.assertTrue(any(error["loc"] == ("native_credential_custody",)
            and error["type"] == "extra_forbidden" for error in refused.exception.errors()))

    def test_unknown_nested_custody_schema_is_not_silently_accepted(self):
        raw = self.marked().model_dump(mode="json")
        raw["native_credential_custody"]["schema_version"] = "future"
        with self.assertRaises(ValidationError) as refused:
            models.ExecutionAssignment.model_validate(raw)
        self.assertTrue(any(error["loc"] == ("native_credential_custody", "schema_version")
            for error in refused.exception.errors()))


if __name__ == "__main__":
    unittest.main()
