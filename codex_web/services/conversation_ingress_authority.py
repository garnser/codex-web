"""Capture only the canonical execution authority selected for one input."""

def capture_conversation_ingress_authority(event, actor, *, connections, routing, bindings,
        settings_service, profile_for_binding, execution_profiles, resources):
    from codex_web.models import BotBindingCreate, BotRouteTest, ThreadRunSettings
    from codex_web.services.conversation_channels import capture_repository_selection, ConversationChannelService
    project = connections._project(event.project_id)
    snapshot = connections.ingress_snapshot(event, actor)
    # Resolve the delivery route without consulting runner history or queues.
    # Only the selected role contributes execution authority to this original.
    selection = routing.preview(BotRouteTest(
        provider="slack", project_id=event.project_id, connection_id=event.connection_id,
        external_conversation_id=event.message.conversation.conversation_id,
        external_thread_id=event.message.conversation.thread_id, message_id=event.message.message_id,
        text=ConversationChannelService._message_text(event) or " "), inspect_execution=False)
    selected_id = selection.get("bindingId")
    selected = bindings.by_id(selected_id) if selected_id else None
    if selected_id and selected is None:
        raise ValueError("selected routing binding no longer exists")
    if selected is not None and selected.project_id != event.project_id:
        raise ValueError("selected routing binding Project mismatch")
    rows = [selected] if selected else []
    snapshot["route_selection"] = {key: selection.get(key) for key in (
        "bindingId", "threadId", "projectId", "routeSource", "wouldCreateThread",
        "wouldCloneBinding", "ambiguous", "steer", "routedText")}
    snapshot["routing"] = []
    if selected is not None or selection.get("ambiguous"):
        snapshot["new_binding"] = None
    else:
        default_profile, default_definition = execution_profiles.resolve(None,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id, project_id=event.project_id)
        default_binding = BotBindingCreate(provider="slack", project_id=event.project_id,
            external_conversation_id=event.message.conversation.conversation_id)
        from codex_web.services.resources import RepositoryTargetSelectionError
        try:
            default_settings = capture_repository_selection(ThreadRunSettings(), project,
                default_profile, resources, actor)
        except RepositoryTargetSelectionError as exc:
            snapshot["new_binding_error"] = exc.code
            # An unusable auto-binding default does not disable an existing binding
            # with its own proven scope (for example an orchestration-only profile).
            snapshot["new_binding"] = None
        else:
            snapshot["new_binding"] = {
                "binding": {"sandbox": default_binding.sandbox, "approval_policy": default_binding.approval_policy},
                "settings": default_settings.model_dump(mode="json"), "profile": None,
                "execution_profile": default_profile.model_dump(mode="json"),
                "definition": default_definition.model_dump(mode="json"),
            }
    for binding in sorted(rows, key=lambda item: item.id):
        settings = settings_service.get(binding.thread_id)
        profile_context = profile_for_binding(binding, actor=actor)
        profile = profile_context[0] if profile_context else None
        execution_profile, definition = execution_profiles.resolve(
            getattr(profile, "execution_profile_id", None) or settings.execution_profile_id,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id, project_id=event.project_id)
        snapshot["routing"].append({
            "binding": binding.model_dump(mode="json", exclude={"updated_at", "created_at", "external_name", "thread_name"}),
            "settings": capture_repository_selection(settings, project, execution_profile,
                resources, actor).model_dump(mode="json"),
            "profile": profile.model_dump(mode="json") if profile else None,
            "execution_profile": execution_profile.model_dump(mode="json"),
            "definition": definition.model_dump(mode="json"),
        })
    return snapshot
