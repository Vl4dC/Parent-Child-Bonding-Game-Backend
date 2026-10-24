-- Additive index for private, owner-scoped exact-snapshot attempt history.
create index records_quiz_snapshot_page
  on api_private.records(owner_id,(data->>'question_set_id'),id)
  where kind='quizzes';
