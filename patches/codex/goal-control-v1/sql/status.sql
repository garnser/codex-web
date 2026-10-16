UPDATE thread_goals SET status=?1,updated_at_ms=?2
WHERE thread_id=?3 AND goal_id=?4 AND status IN ('active','paused','blocked')
AND (?1!='active' OR token_budget IS NULL OR tokens_used<token_budget)
RETURNING json_object('threadId',thread_id,'goalId',goal_id,'objective',objective,'status',status,'tokenBudget',token_budget,'tokensUsed',tokens_used,'timeUsedSeconds',time_used_seconds,'createdAtMs',created_at_ms,'updatedAtMs',updated_at_ms)
