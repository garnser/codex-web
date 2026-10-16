-- Native private Goal database only. No application Goal/authority store.
CREATE TABLE goal_control_clock(thread_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, epoch INTEGER NOT NULL DEFAULT 0, held INTEGER NOT NULL DEFAULT 0 CHECK(held IN (0,1)));
INSERT INTO goal_control_clock(thread_id,revision) SELECT thread_id,1 FROM thread_goals;
CREATE TABLE goal_control_receipts(thread_id TEXT NOT NULL, operation_id TEXT NOT NULL, request TEXT NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(thread_id,operation_id));
CREATE TRIGGER goal_receipt_no_update BEFORE UPDATE ON goal_control_receipts BEGIN SELECT RAISE(ABORT,'immutable goal receipt'); END;
CREATE TRIGGER goal_receipt_no_delete BEFORE DELETE ON goal_control_receipts BEGIN SELECT RAISE(ABORT,'immutable goal receipt'); END;
CREATE TRIGGER goal_control_fence_insert BEFORE INSERT ON thread_goals WHEN EXISTS(SELECT 1 FROM goal_control_clock WHERE thread_id=NEW.thread_id AND held=1) BEGIN SELECT RAISE(ABORT,'goal writer fenced'); END;
CREATE TRIGGER goal_control_revision_insert AFTER INSERT ON thread_goals BEGIN INSERT INTO goal_control_clock(thread_id,revision) VALUES(NEW.thread_id,1) ON CONFLICT(thread_id) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER goal_control_fence_update BEFORE UPDATE ON thread_goals WHEN EXISTS(SELECT 1 FROM goal_control_clock WHERE (thread_id=OLD.thread_id OR thread_id=NEW.thread_id) AND held=1) BEGIN SELECT RAISE(ABORT,'goal writer fenced'); END;
CREATE TRIGGER goal_control_revision_update AFTER UPDATE ON thread_goals BEGIN INSERT INTO goal_control_clock(thread_id,revision) VALUES(NEW.thread_id,1) ON CONFLICT(thread_id) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER goal_control_fence_delete BEFORE DELETE ON thread_goals WHEN EXISTS(SELECT 1 FROM goal_control_clock WHERE thread_id=OLD.thread_id AND held=1) BEGIN SELECT RAISE(ABORT,'goal writer fenced'); END;
CREATE TRIGGER goal_control_revision_delete AFTER DELETE ON thread_goals BEGIN INSERT INTO goal_control_clock(thread_id,revision) VALUES(OLD.thread_id,1) ON CONFLICT(thread_id) DO UPDATE SET revision=revision+1; END;
