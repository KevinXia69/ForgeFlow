ALTER TABLE control_attentions DROP CONSTRAINT IF EXISTS control_attentions_need_type_check;
ALTER TABLE control_attentions ADD CONSTRAINT control_attentions_need_type_check
    CHECK (need_type IN ('clarification', 'fact', 'approval', 'failure', 'rule',
                         'budget', 'result_unknown', 'system'));
